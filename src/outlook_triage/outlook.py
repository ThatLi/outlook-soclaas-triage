from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo


OL_FOLDER_INBOX = 6
SUPPORTED_MESSAGE_PREFIXES = ("IPM.Note", "IPM.Schedule.Meeting")


class OutlookError(RuntimeError):
    pass


class OutlookUnavailable(OutlookError):
    pass


class OutlookSecurityError(OutlookError):
    pass


@dataclass(frozen=True)
class SyncResult:
    messages: list[dict]
    high_water_received_at: str


def message_key(store_id: str, source_id: str) -> str:
    return f"{store_id}:{source_id}"


def _safe_get(obj: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, name)
        return default if value is None else value
    except Exception:
        return default


class OutlookClient:
    def __init__(
        self,
        profile: str | None = None,
        *,
        timezone: ZoneInfo | None = None,
        dispatch: Callable[[str], Any] | None = None,
        co_initialize: Callable[[], None] | None = None,
        co_uninitialize: Callable[[], None] | None = None,
    ):
        self.profile = profile
        self.timezone = timezone or ZoneInfo("Asia/Singapore")
        self._closed = False
        if dispatch is None:
            try:
                import pythoncom
                import win32com.client
            except ImportError as exc:
                raise OutlookUnavailable(
                    "Classic Outlook access requires Windows and pywin32. Install this project in a Windows virtual environment."
                ) from exc
            dispatch = win32com.client.Dispatch
            co_initialize = pythoncom.CoInitialize
            co_uninitialize = pythoncom.CoUninitialize
        self._co_uninitialize = co_uninitialize or (lambda: None)
        try:
            (co_initialize or (lambda: None))()
            self.application = dispatch("Outlook.Application")
            self.namespace = self.application.GetNamespace("MAPI")
            if profile:
                self.namespace.Logon(profile, "", False, False)
            self.inbox = self.namespace.GetDefaultFolder(OL_FOLDER_INBOX)
            self.store_id = str(self.inbox.StoreID)
        except Exception as exc:
            self._co_uninitialize()
            raise self._translate_error("connect to classic Outlook", exc) from exc

    def __enter__(self) -> "OutlookClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.inbox = None
        self.namespace = None
        self.application = None
        self._co_uninitialize()

    @staticmethod
    def _translate_error(operation: str, exc: Exception) -> OutlookError:
        detail = str(exc)
        lowered = detail.lower()
        if any(marker in lowered for marker in ("access denied", "mapi_e_not_supported", "0x80070005", "0x80040102")):
            return OutlookSecurityError(
                f"Outlook blocked programmatic access while attempting to {operation}. "
                "Review your organization's Outlook Programmatic Access policy; this application will not bypass it."
            )
        return OutlookUnavailable(
            f"Unable to {operation}. Confirm that classic Outlook is installed, configured, and signed in. ({type(exc).__name__})"
        )

    def _received_datetime(self, item: Any) -> datetime:
        value = _safe_get(item, "ReceivedTime")
        if not isinstance(value, datetime):
            raise OutlookError("Outlook item has no usable ReceivedTime")
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def _sender(self, item: Any) -> tuple[str, str]:
        name = str(_safe_get(item, "SenderName", "") or "")
        address = ""
        try:
            if str(_safe_get(item, "SenderEmailType", "")).upper() == "EX":
                sender = _safe_get(item, "Sender")
                exchange_user = sender.GetExchangeUser() if sender else None
                address = str(_safe_get(exchange_user, "PrimarySmtpAddress", "") or "")
                if not address and sender:
                    distribution_list = sender.GetExchangeDistributionList()
                    address = str(_safe_get(distribution_list, "PrimarySmtpAddress", "") or "")
            if not address:
                address = str(_safe_get(item, "SenderEmailAddress", "") or "")
        except Exception:
            address = str(_safe_get(item, "SenderEmailAddress", "") or "")
        return name, address

    def _normalize(self, item: Any, *, include_body: bool) -> dict | None:
        message_class = str(_safe_get(item, "MessageClass", "") or "")
        if not message_class.startswith(SUPPORTED_MESSAGE_PREFIXES):
            return None
        source_id = str(_safe_get(item, "EntryID", "") or "")
        if not source_id:
            return None
        received = self._received_datetime(item)
        name, address = self._sender(item)
        importance_value = int(_safe_get(item, "Importance", 1) or 1)
        importance = {0: "low", 2: "high"}.get(importance_value, "normal")
        body = str(_safe_get(item, "Body", "") or "") if include_body else ""
        attachments = _safe_get(item, "Attachments")
        attachment_count = int(_safe_get(attachments, "Count", 0) or 0)
        return {
            "messageKey": message_key(self.store_id, source_id),
            "sourceId": source_id,
            "storeId": self.store_id,
            "conversationId": str(_safe_get(item, "ConversationID", "") or ""),
            "sender": {"emailAddress": {"name": name, "address": address}},
            "subject": str(_safe_get(item, "Subject", "") or "(no subject)"),
            "receivedDateTime": received.isoformat(timespec="seconds"),
            "importance": importance,
            "bodyPreview": body[:255],
            "body": {"contentType": "text", "content": body},
            "hasAttachments": attachment_count > 0,
        }

    def _iter_sorted(self):
        try:
            items = self.inbox.Items
            items.Sort("[ReceivedTime]", True)
            item = items.GetFirst()
            while item is not None:
                yield item
                item = items.GetNext()
        except Exception as exc:
            raise self._translate_error("enumerate Inbox messages", exc) from exc

    def newest_messages(self, limit: int = 10) -> list[dict]:
        messages: list[dict] = []
        for item in self._iter_sorted():
            normalized = self._normalize(item, include_body=False)
            if normalized:
                messages.append(normalized)
                if len(messages) >= limit:
                    break
        return messages

    def get_message(self, source_id: str, store_id: str | None = None) -> dict:
        try:
            item = self.namespace.GetItemFromID(source_id, store_id or self.store_id)
            normalized = self._normalize(item, include_body=True)
        except Exception as exc:
            raise self._translate_error("read the selected Outlook message", exc) from exc
        if not normalized:
            raise OutlookError("The selected Outlook item is not a supported mail or meeting message")
        return normalized

    def sync(
        self,
        last_received_at: str | None,
        *,
        bootstrap_days: int = 7,
        overlap_hours: int = 8,
    ) -> SyncResult:
        now = datetime.now(self.timezone)
        previous = datetime.fromisoformat(last_received_at).astimezone(self.timezone) if last_received_at else None
        cutoff = previous - timedelta(hours=overlap_hours) if previous else now - timedelta(days=bootstrap_days)
        high_water = previous
        messages: list[dict] = []
        for item in self._iter_sorted():
            try:
                received = self._received_datetime(item)
            except OutlookError:
                continue
            if received < cutoff:
                break
            normalized = self._normalize(item, include_body=True)
            if not normalized:
                continue
            messages.append(normalized)
            if high_water is None or received > high_water:
                high_water = received
        return SyncResult(messages, (high_water or now).isoformat(timespec="seconds"))

