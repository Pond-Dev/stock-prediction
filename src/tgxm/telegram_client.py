"""Read-only Telegram ingestion through an optional Telethon dependency.

This module deliberately has no broker dependency.  It turns Telegram updates
into small immutable envelopes and hands them to the application pipeline.  It
never replies to, edits, or deletes messages in a signal channel.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Awaitable, Callable, Iterable


class TelegramDependencyError(RuntimeError):
    """Raised when Telegram functionality is requested without Telethon."""


class TelegramConfigurationError(ValueError):
    """Raised when required Telegram environment variables are unavailable."""


class TelegramAuthenticationError(RuntimeError):
    """Raised when the configured Telegram session is not authorized."""


@dataclass(frozen=True, slots=True)
class TelegramCredentials:
    api_id: int = field(repr=False)
    api_hash: str = field(repr=False)
    session: str = field(repr=False)

    def __repr__(self) -> str:
        return "TelegramCredentials(<redacted>)"

    @classmethod
    def from_environment(
        cls,
        *,
        api_id_env: str,
        api_hash_env: str,
        session_env: str,
    ) -> "TelegramCredentials":
        values = {
            api_id_env: os.getenv(api_id_env),
            api_hash_env: os.getenv(api_hash_env),
            session_env: os.getenv(session_env),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise TelegramConfigurationError(
                "missing Telegram environment variables: " + ", ".join(missing)
            )
        try:
            api_id = int(values[api_id_env] or "")
        except ValueError as exc:
            raise TelegramConfigurationError(
                f"{api_id_env} must contain an integer Telegram API ID"
            ) from exc
        if api_id <= 0:
            raise TelegramConfigurationError(f"{api_id_env} must be positive")
        return cls(
            api_id=api_id,
            api_hash=values[api_hash_env] or "",
            session=values[session_env] or "",
        )


@dataclass(frozen=True, slots=True)
class TelegramMessageEnvelope:
    """Transport-neutral evidence captured from one Telegram update."""

    peer_id: int
    message_id: int
    text: str
    message_time_utc: datetime
    observed_at_utc: datetime
    event_kind: str = "new"
    edit_time_utc: datetime | None = None
    reply_to_message_id: int | None = None
    forward_origin: str | None = None

    def __post_init__(self) -> None:
        if self.event_kind not in {"new", "edit"}:
            raise ValueError("event_kind must be 'new' or 'edit'")
        if self.message_time_utc.tzinfo is None:
            raise ValueError("message_time_utc must be timezone-aware")
        if self.observed_at_utc.tzinfo is None:
            raise ValueError("observed_at_utc must be timezone-aware")
        if self.edit_time_utc is not None and self.edit_time_utc.tzinfo is None:
            raise ValueError("edit_time_utc must be timezone-aware")


EnvelopeHandler = Callable[
    [TelegramMessageEnvelope], None | Awaitable[None]
]


@dataclass(frozen=True, slots=True)
class TelegramDialog:
    peer_id: int
    title: str
    is_channel: bool
    is_group: bool


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("Telegram timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _peer_id_from_event(event: object) -> int:
    value = getattr(event, "chat_id", None)
    if value is None:
        message = getattr(event, "message", None)
        value = getattr(message, "chat_id", None)
    if value is None:
        raise ValueError("Telegram update did not include a stable numeric peer ID")
    return int(value)


def _forward_origin(message: object) -> str | None:
    forward = getattr(message, "fwd_from", None)
    if forward is None:
        return None
    origin = getattr(forward, "from_id", None)
    original_message_id = getattr(forward, "channel_post", None)
    if origin is None and original_message_id is None:
        return "forwarded"
    # This is evidence used for correlation, not a credential.  repr keeps
    # Telethon peer variants distinct without coupling the core to its types.
    return f"{origin!r}:{original_message_id!r}"


def envelope_from_telethon_event(
    event: object,
    *,
    event_kind: str,
    observed_at_utc: datetime | None = None,
) -> TelegramMessageEnvelope:
    """Convert a Telethon event using duck typing, which is easy to fixture."""

    message = getattr(event, "message", None)
    if message is None:
        raise ValueError("Telegram update did not include a message")
    message_id = getattr(message, "id", None)
    message_time = _to_utc(getattr(message, "date", None))
    if message_id is None or message_time is None:
        raise ValueError("Telegram message is missing ID or timestamp")
    text = getattr(message, "message", None)
    if text is None:
        text = getattr(message, "raw_text", "")
    reply = getattr(message, "reply_to_msg_id", None)
    if reply is None:
        reply_header = getattr(message, "reply_to", None)
        reply = getattr(reply_header, "reply_to_msg_id", None)
    return TelegramMessageEnvelope(
        peer_id=_peer_id_from_event(event),
        message_id=int(message_id),
        text=str(text or ""),
        message_time_utc=message_time,
        observed_at_utc=_to_utc(observed_at_utc) or datetime.now(UTC),
        event_kind=event_kind,
        edit_time_utc=_to_utc(getattr(message, "edit_date", None)),
        reply_to_message_id=int(reply) if reply is not None else None,
        forward_origin=_forward_origin(message),
    )


class TelethonEventSource:
    """Streams allowlisted new/edit events from an authorized user session."""

    def __init__(
        self,
        credentials: TelegramCredentials,
        allowed_peer_ids: Iterable[int],
    ) -> None:
        peer_ids = frozenset(int(value) for value in allowed_peer_ids)
        if not peer_ids:
            raise TelegramConfigurationError(
                "at least one enabled numeric Telegram peer ID is required"
            )
        self._credentials = credentials
        self._allowed_peer_ids = peer_ids

    async def run(self, handler: EnvelopeHandler) -> None:
        try:
            from telethon import TelegramClient, events
        except ImportError as exc:  # pragma: no cover - depends on local extras
            raise TelegramDependencyError(
                "Telethon is not installed; run: py -m pip install -e .[telegram]"
            ) from exc

        client = TelegramClient(
            self._credentials.session,
            self._credentials.api_id,
            self._credentials.api_hash,
        )

        async def dispatch(event: object, event_kind: str) -> None:
            envelope = envelope_from_telethon_event(event, event_kind=event_kind)
            # Defense in depth: the event filter is not treated as authority.
            if envelope.peer_id not in self._allowed_peer_ids:
                return
            result = handler(envelope)
            if inspect.isawaitable(result):
                await result

        @client.on(events.NewMessage(chats=list(self._allowed_peer_ids)))
        async def on_new_message(event: object) -> None:
            await dispatch(event, "new")

        @client.on(events.MessageEdited(chats=list(self._allowed_peer_ids)))
        async def on_message_edited(event: object) -> None:
            await dispatch(event, "edit")

        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise TelegramAuthenticationError(
                    "Telegram session is not authorized; create/authorize the session separately"
                )
            await client.run_until_disconnected()
        finally:
            await client.disconnect()


def _telethon_client(credentials: TelegramCredentials):
    try:
        from telethon import TelegramClient
    except ImportError as exc:  # pragma: no cover - depends on local extras
        raise TelegramDependencyError(
            "Telethon is not installed; run: py -m pip install -e .[telegram]"
        ) from exc
    return TelegramClient(
        credentials.session,
        credentials.api_id,
        credentials.api_hash,
    )


async def authorize_telegram_session(credentials: TelegramCredentials) -> int:
    """Interactively authorize a local Telethon session and return its user ID."""

    client = _telethon_client(credentials)
    try:
        # Telethon handles phone, OTP and optional 2FA prompts.  The resulting
        # session file is local and gitignored; the values are never logged here.
        await client.start()
        me = await client.get_me()
        user_id = getattr(me, "id", None)
        if user_id is None:
            raise TelegramAuthenticationError(
                "Telegram authorization completed without a readable user identity"
            )
        return int(user_id)
    finally:
        await client.disconnect()


async def list_telegram_dialogs(
    credentials: TelegramCredentials,
) -> tuple[TelegramDialog, ...]:
    """List accessible dialogs so the operator can configure numeric peer IDs."""

    client = _telethon_client(credentials)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise TelegramAuthenticationError(
                "Telegram session is not authorized; run telegram-login first"
            )
        dialogs = await client.get_dialogs()
        result = [
            TelegramDialog(
                peer_id=int(dialog.id),
                title=str(getattr(dialog, "title", "") or ""),
                is_channel=bool(getattr(dialog, "is_channel", False)),
                is_group=bool(getattr(dialog, "is_group", False)),
            )
            for dialog in dialogs
            if bool(getattr(dialog, "is_channel", False))
            or bool(getattr(dialog, "is_group", False))
        ]
        return tuple(sorted(result, key=lambda item: (item.title.casefold(), item.peer_id)))
    finally:
        await client.disconnect()
