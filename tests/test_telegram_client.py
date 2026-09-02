import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tgxm.telegram_client import (
    TelegramConfigurationError,
    TelegramCredentials,
    list_telegram_dialogs,
    envelope_from_telethon_event,
)


def test_credentials_are_loaded_from_named_environment_variables(monkeypatch):
    monkeypatch.setenv("TEST_TG_API_ID", "123")
    monkeypatch.setenv("TEST_TG_API_HASH", "secret-hash")
    monkeypatch.setenv("TEST_TG_SESSION", "state/test")

    credentials = TelegramCredentials.from_environment(
        api_id_env="TEST_TG_API_ID",
        api_hash_env="TEST_TG_API_HASH",
        session_env="TEST_TG_SESSION",
    )

    assert credentials.api_id == 123
    assert credentials.api_hash == "secret-hash"
    assert credentials.session == "state/test"


def test_credentials_fail_closed_when_environment_is_incomplete(monkeypatch):
    monkeypatch.delenv("MISSING_TG_API_ID", raising=False)
    monkeypatch.delenv("MISSING_TG_API_HASH", raising=False)
    monkeypatch.delenv("MISSING_TG_SESSION", raising=False)

    with pytest.raises(TelegramConfigurationError, match="missing"):
        TelegramCredentials.from_environment(
            api_id_env="MISSING_TG_API_ID",
            api_hash_env="MISSING_TG_API_HASH",
            session_env="MISSING_TG_SESSION",
        )


def test_envelope_preserves_transport_evidence_and_normalizes_utc():
    local_time = datetime(2026, 8, 27, 10, 30, tzinfo=UTC) + timedelta(0)
    message = SimpleNamespace(
        id=77,
        date=local_time,
        edit_date=None,
        message="GOLD SELL 4601 SL 4618 TP 4595",
        reply_to_msg_id=40,
        fwd_from=None,
    )
    event = SimpleNamespace(chat_id=-100123, message=message)

    envelope = envelope_from_telethon_event(
        event,
        event_kind="new",
        observed_at_utc=datetime(2026, 8, 27, 10, 31, tzinfo=UTC),
    )

    assert envelope.peer_id == -100123
    assert envelope.message_id == 77
    assert envelope.reply_to_message_id == 40
    assert envelope.message_time_utc.tzinfo is UTC
    assert envelope.event_kind == "new"


def test_credentials_repr_is_redacted(monkeypatch):
    monkeypatch.setenv("REPR_TG_API_ID", "123")
    monkeypatch.setenv("REPR_TG_API_HASH", "must-not-appear")
    monkeypatch.setenv("REPR_TG_SESSION", "private-session")
    credentials = TelegramCredentials.from_environment(
        api_id_env="REPR_TG_API_ID",
        api_hash_env="REPR_TG_API_HASH",
        session_env="REPR_TG_SESSION",
    )

    rendered = repr(credentials)
    assert "must-not-appear" not in rendered
    assert "private-session" not in rendered
    assert "redacted" in rendered


def test_naive_telegram_timestamp_is_rejected():
    message = SimpleNamespace(
        id=77,
        date=datetime(2026, 8, 27, 10, 30),
        edit_date=None,
        message="signal",
        reply_to_msg_id=None,
        fwd_from=None,
    )
    event = SimpleNamespace(chat_id=-100123, message=message)
    with pytest.raises(ValueError, match="timezone-aware"):
        envelope_from_telethon_event(event, event_kind="new")


def test_dialog_listing_excludes_private_direct_contacts(monkeypatch):
    dialogs = [
        SimpleNamespace(id=-1001, title="Signals", is_channel=True, is_group=False),
        SimpleNamespace(id=-2002, title="Group", is_channel=False, is_group=True),
        SimpleNamespace(id=3003, title="Private Contact", is_channel=False, is_group=False),
    ]

    class FakeClient:
        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_dialogs(self):
            return dialogs

        async def disconnect(self):
            return None

    monkeypatch.setattr("tgxm.telegram_client._telethon_client", lambda _: FakeClient())
    result = asyncio.run(
        list_telegram_dialogs(TelegramCredentials(123, "hash", "session"))
    )
    assert {item.title for item in result} == {"Signals", "Group"}
