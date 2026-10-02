from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from outlook_triage.outlook import OutlookClient, OutlookSecurityError, message_key


SGT = ZoneInfo("Asia/Singapore")


class Item:
    def __init__(self, entry_id, received, *, body="Body", message_class="IPM.Note", sender_type="SMTP"):
        self.EntryID = entry_id
        self.ReceivedTime = received
        self.MessageClass = message_class
        self.Subject = f"Subject {entry_id}"
        self.SenderName = "Alice"
        self.SenderEmailType = sender_type
        self.SenderEmailAddress = "alice@example.com"
        self.ConversationID = "conversation"
        self.Importance = 1
        self.Attachments = SimpleNamespace(Count=0)
        self._body = body
        self.body_reads = 0
        if sender_type == "EX":
            self.Sender = SimpleNamespace(
                GetExchangeUser=lambda: SimpleNamespace(PrimarySmtpAddress="alice@company.test"),
                GetExchangeDistributionList=lambda: None,
            )

    @property
    def Body(self):
        self.body_reads += 1
        return self._body


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

