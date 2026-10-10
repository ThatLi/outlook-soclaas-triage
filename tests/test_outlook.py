from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from outlook_triage.outlook import (
    OutlookClient,
    OutlookSecurityError,
    OutlookUnavailable,
    message_key,
)


SGT = ZoneInfo("Asia/Singapore")


class Item:
    def __init__(
        self,
        entry_id,
        received,
        *,
        body="Body",
        message_class="IPM.Note",
        sender_type="SMTP",
        unread=True,
        flag_status=0,
    ):
        self.EntryID = entry_id
        self.ReceivedTime = received
        self.MessageClass = message_class
        self.UnRead = unread
        self.FlagStatus = flag_status
        self.Subject = f"Subject {entry_id}"
        self.SenderName = "Alice"
        self.SenderEmailType = sender_type
        self.SenderEmailAddress = "alice@example.com"
        self.ConversationID = "conversation"
        self.Importance = 1
        self.Attachments = SimpleNamespace(Count=0)
        self._body = body
        self.body_reads = 0
        self.save_calls = 0
        if sender_type == "EX":
            self.Sender = SimpleNamespace(
                GetExchangeUser=lambda: SimpleNamespace(PrimarySmtpAddress="alice@company.test"),
                GetExchangeDistributionList=lambda: None,
            )

    @property
    def Body(self):
        self.body_reads += 1
        return self._body

    def Save(self):
        self.save_calls += 1


class Items:
    def __init__(self, values):
        self.values = list(values)
        self.index = 0
        self.sorted = None

    def Sort(self, field, descending):
        self.sorted = (field, descending)

    def GetFirst(self):
        self.index = 0
        return self.values[0] if self.values else None

    def GetNext(self):
        self.index += 1
        return self.values[self.index] if self.index < len(self.values) else None


class Namespace:
    def __init__(self, items):
        self.inbox = SimpleNamespace(StoreID="store-1", Items=Items(items))
        self.by_id = {item.EntryID: item for item in items}
        self.logon_args = None

    def Logon(self, *args):
        self.logon_args = args

    def GetDefaultFolder(self, folder_id):
        assert folder_id == 6
        return self.inbox

    def GetItemFromID(self, entry_id, store_id):
        assert store_id == "store-1"
        return self.by_id[entry_id]


def make_client(items, *, profile=None):
    namespace = Namespace(items)
    application = SimpleNamespace(GetNamespace=lambda name: namespace)
    lifecycle = []
    client = OutlookClient(
        profile,
        timezone=SGT,
        dispatch=lambda prog_id: application,
        co_initialize=lambda: lifecycle.append("init"),
        co_uninitialize=lambda: lifecycle.append("uninit"),
    )
    return client, namespace, lifecycle


def test_check_mail_does_not_read_body_and_closes_com():
    item = Item("entry-1", datetime(2026, 10, 2, 8, 0, tzinfo=SGT))
    client, namespace, lifecycle = make_client([item], profile="Work")
    with client:
        messages = client.newest_messages(10)
    assert messages[0]["sourceId"] == "entry-1"
    assert item.body_reads == 0
    assert namespace.logon_args[0] == "Work"
    assert lifecycle == ["init", "uninit"]


def test_sync_uses_overlap_cutoff_and_high_water():
    items = [
        Item("new", datetime(2026, 10, 2, 10, 0, tzinfo=SGT)),
        Item("overlap", datetime(2026, 10, 2, 8, 30, tzinfo=SGT)),
        Item("old", datetime(2026, 10, 2, 7, 0, tzinfo=SGT)),
    ]
    client, namespace, _ = make_client(items)
    with client:
        result = client.sync("2026-10-02T09:00:00+08:00", overlap_hours=1)
    assert [message["sourceId"] for message in result.messages] == ["new", "overlap"]
    assert result.high_water_received_at == "2026-10-02T10:00:00+08:00"
    assert namespace.inbox.Items.sorted == ("[ReceivedTime]", True)


def test_sync_includes_unread_or_actively_flagged_messages_only():
    items = [
        Item("unread", datetime(2026, 10, 2, 11, 0, tzinfo=SGT), unread=True),
        Item("flagged", datetime(2026, 10, 2, 10, 0, tzinfo=SGT), unread=False, flag_status=2),
        Item("read", datetime(2026, 10, 2, 9, 0, tzinfo=SGT), unread=False),
        Item("completed", datetime(2026, 10, 2, 8, 0, tzinfo=SGT), unread=False, flag_status=1),
    ]
    client, _, _ = make_client(items)
    with client:
        result = client.sync("2026-10-02T07:00:00+08:00", overlap_hours=0)
    assert [message["sourceId"] for message in result.messages] == ["unread", "flagged"]
    assert items[0].body_reads == 1
    assert items[1].body_reads == 1
    assert items[2].body_reads == 0
    assert items[3].body_reads == 0


def test_sync_advances_high_water_for_excluded_message():
    read = Item("read", datetime(2026, 10, 2, 10, 0, tzinfo=SGT), unread=False)
    client, _, _ = make_client([read])
    with client:
        result = client.sync("2026-10-02T09:00:00+08:00", overlap_hours=0)
    assert result.messages == []
    assert result.high_water_received_at == "2026-10-02T10:00:00+08:00"
    assert read.body_reads == 0


def test_get_message_ignores_sync_eligibility_for_explicit_fetches():
    read = Item("read", datetime(2026, 10, 2, 8, 0, tzinfo=SGT), unread=False)
    client, _, _ = make_client([read])
    with client:
        message = client.get_message("read")
    assert message["sourceId"] == "read"
    assert read.body_reads == 1


def test_mark_read_changes_and_saves_unread_item_idempotently():
    item = Item("entry-1", datetime(2026, 10, 2, 8, 0, tzinfo=SGT), unread=True)
    client, _, _ = make_client([item])
    with client:
        assert client.mark_read("entry-1", "store-1") is True
        assert item.UnRead is False
        assert item.save_calls == 1
        assert client.mark_read("entry-1", "store-1") is False
        assert item.save_calls == 1


def test_body_preview_is_bounded_and_does_not_change_read_state():
    item = Item("entry-1", datetime(2026, 10, 2, 8, 0, tzinfo=SGT), body="abcdef", unread=True)
    client, _, _ = make_client([item])
    with client:
        assert client.get_body_preview("entry-1", "store-1", limit=3) == "abc"
    assert item.UnRead is True
    assert item.save_calls == 0


def test_exchange_sender_and_refetch():
    item = Item("entry-1", datetime(2026, 10, 2, 8, 0, tzinfo=SGT), sender_type="EX")
    client, _, _ = make_client([item])
    with client:
        message = client.get_message("entry-1", "store-1")
    assert message["sender"]["emailAddress"]["address"] == "alice@company.test"
    assert message["messageKey"] == message_key("store-1", "entry-1")
    assert item.body_reads == 1


def test_unsupported_outlook_item_is_ignored():
    item = Item("calendar", datetime(2026, 10, 2, 8, 0, tzinfo=SGT), message_class="IPM.Appointment")
    client, _, _ = make_client([item])
    with client:
        assert client.newest_messages() == []


def test_access_denied_is_reported_as_security_error():
    def denied(_):
        raise RuntimeError("0x80070005 Access denied")

    with pytest.raises(OutlookSecurityError, match="will not bypass"):
        OutlookClient(dispatch=denied, co_initialize=lambda: None, co_uninitialize=lambda: None)


@pytest.mark.parametrize("failure_point", ["dispatch", "namespace", "profile", "inbox"])
def test_constructor_failures_release_com_and_report_outlook_unavailable(failure_point):
    lifecycle = []

    class FailingNamespace:
        def Logon(self, *args):
            if failure_point == "profile":
                raise RuntimeError("profile is invalid")

        def GetDefaultFolder(self, folder_id):
            if failure_point == "inbox":
                raise RuntimeError("Inbox is unavailable")
            return SimpleNamespace(StoreID="store-1", Items=Items([]))

    class Application:
        def GetNamespace(self, name):
            if failure_point == "namespace":
                raise RuntimeError("MAPI is unavailable")
            return FailingNamespace()

    def dispatch(prog_id):
        if failure_point == "dispatch":
            raise RuntimeError("classic Outlook is unavailable")
        return Application()

    with pytest.raises(OutlookUnavailable, match="classic Outlook"):
        OutlookClient(
            "Work",
            dispatch=dispatch,
            co_initialize=lambda: lifecycle.append("init"),
            co_uninitialize=lambda: lifecycle.append("uninit"),
        )
    assert lifecycle == ["init", "uninit"]


def test_com_busy_during_enumeration_is_translated_and_com_is_released():
    client, namespace, lifecycle = make_client([])

    class BusyItems:
        def Sort(self, field, descending):
            raise RuntimeError("Call was rejected by callee")

    namespace.inbox.Items = BusyItems()
    with pytest.raises(OutlookUnavailable, match="enumerate Inbox messages"):
        with client:
            client.newest_messages()
    assert lifecycle == ["init", "uninit"]


@pytest.mark.parametrize(
    "failure, expected_error",
    [
        (KeyError("message disappeared"), OutlookUnavailable),
        (RuntimeError("0x80070005 Access denied"), OutlookSecurityError),
    ],
)
def test_get_item_failure_is_translated(failure, expected_error):
    client, namespace, lifecycle = make_client([])

    def fail_get_item(entry_id, store_id):
        raise failure

    namespace.GetItemFromID = fail_get_item
    with pytest.raises(expected_error, match="selected Outlook message"):
        with client:
            client.get_message("missing-entry")
    assert lifecycle == ["init", "uninit"]


def test_exchange_sender_property_denial_falls_back_to_sender_email_address():
    item = Item("entry-1", datetime(2026, 10, 2, 8, 0, tzinfo=SGT), sender_type="EX")

    class DeniedSender:
        def GetExchangeUser(self):
            raise RuntimeError("property access denied")

    item.Sender = DeniedSender()
    client, _, _ = make_client([item])
    with client:
        message = client.get_message("entry-1")
    assert message["sender"]["emailAddress"]["address"] == "alice@example.com"


def test_close_is_idempotent_and_context_failure_releases_com():
    client, _, lifecycle = make_client([])
    with pytest.raises(RuntimeError, match="operation failed"):
        with client:
            raise RuntimeError("operation failed")
    client.close()
    client.close()
    assert lifecycle == ["init", "uninit"]


def test_malformed_items_are_skipped_during_inbox_enumeration():
    missing_received = Item("missing-received", None)
    invalid_importance = Item("bad-importance", datetime(2026, 10, 2, 9, 0, tzinfo=SGT))
    invalid_importance.Importance = "not-an-integer"
    valid = Item("valid", datetime(2026, 10, 2, 8, 0, tzinfo=SGT))
    client, _, _ = make_client([missing_received, invalid_importance, valid])
    with client:
        newest = client.newest_messages()
    assert [message["sourceId"] for message in newest] == ["valid"]


def test_malformed_items_are_skipped_during_sync():
    malformed = Item("bad", datetime(2026, 10, 2, 9, 0, tzinfo=SGT))
    malformed.Importance = "not-an-integer"
    valid = Item("valid", datetime(2026, 10, 2, 8, 0, tzinfo=SGT))
    client, _, _ = make_client([malformed, valid])
    with client:
        result = client.sync("2026-10-02T07:00:00+08:00", overlap_hours=0)
    assert [message["sourceId"] for message in result.messages] == ["valid"]
    assert result.high_water_received_at == "2026-10-02T09:00:00+08:00"

