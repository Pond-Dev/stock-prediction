"""Durable SQLite persistence for Telegram evidence and broker intents.

The store deliberately keeps its DTOs independent from parser models.  This
keeps the persistence boundary small and lets callers persist only the fields
needed for idempotency and broker execution.  Prices and volumes are stored as
decimal text; binary floating point is rejected at this boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Sequence


UTC = timezone.utc


class StoreError(RuntimeError):
    """Base class for persistence failures."""


class PersistenceConflictError(StoreError):
    """A unique identity was reused with different immutable evidence."""


class IntentNotFoundError(StoreError):
    """The requested order intent does not exist."""


class InvalidIntentTransitionError(StoreError):
    """An order intent state transition is not allowed."""


class ConcurrentTransitionError(StoreError):
    """An intent changed after it was read and before it was updated."""


class IntentStatus(str, Enum):
    INTENT_PERSISTED = "INTENT_PERSISTED"
    SUBMITTING = "SUBMITTING"
    OPEN = "OPEN"
    PARTIAL_OPEN = "PARTIAL_OPEN"
    BROKER_REJECTED = "BROKER_REJECTED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    SAFE_FAILED = "SAFE_FAILED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


_ALLOWED_TRANSITIONS: Mapping[IntentStatus, frozenset[IntentStatus]] = {
    IntentStatus.INTENT_PERSISTED: frozenset(
        {
            IntentStatus.SUBMITTING,
            IntentStatus.BROKER_REJECTED,
            IntentStatus.CANCELLED,
            IntentStatus.SAFE_FAILED,
        }
    ),
    IntentStatus.SUBMITTING: frozenset(
        {
            IntentStatus.OPEN,
            IntentStatus.PARTIAL_OPEN,
            IntentStatus.BROKER_REJECTED,
            IntentStatus.RECONCILE_REQUIRED,
            IntentStatus.SAFE_FAILED,
        }
    ),
    IntentStatus.RECONCILE_REQUIRED: frozenset(
        {
            IntentStatus.OPEN,
            IntentStatus.PARTIAL_OPEN,
            IntentStatus.BROKER_REJECTED,
            IntentStatus.CLOSED,
            IntentStatus.SAFE_FAILED,
        }
    ),
    IntentStatus.OPEN: frozenset(
        {
            IntentStatus.RECONCILE_REQUIRED,
            IntentStatus.CLOSED,
            IntentStatus.SAFE_FAILED,
        }
    ),
    IntentStatus.PARTIAL_OPEN: frozenset(
        {
            IntentStatus.OPEN,
            IntentStatus.RECONCILE_REQUIRED,
            IntentStatus.CLOSED,
            IntentStatus.SAFE_FAILED,
        }
    ),
    IntentStatus.BROKER_REJECTED: frozenset(),
    IntentStatus.SAFE_FAILED: frozenset(),
    IntentStatus.CANCELLED: frozenset(),
    IntentStatus.CLOSED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class RawEvent:
    chat_id: int
    message_id: int
    revision: int
    event_type: str
    observed_at_utc: datetime
    message_time_utc: datetime | None = None
    raw_text: str | None = None
    reply_to_message_id: int | None = None
    forward_chat_id: int | None = None
    forward_message_id: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.message_id < 0:
            raise ValueError("message_id must be non-negative")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if not self.event_type.strip():
            raise ValueError("event_type is required")
        _as_utc_text(self.observed_at_utc, "observed_at_utc")
        if self.message_time_utc is not None:
            _as_utc_text(self.message_time_utc, "message_time_utc")


@dataclass(frozen=True, slots=True)
class RawEventRecord:
    id: int
    chat_id: int
    message_id: int
    revision: int
    event_type: str
    observed_at_utc: datetime
    message_time_utc: datetime | None
    raw_text: str | None
    reply_to_message_id: int | None
    forward_chat_id: int | None
    forward_message_id: int | None
    metadata: Mapping[str, Any]
    payload_hash: str


@dataclass(frozen=True, slots=True)
class WaitingEntryRecord:
    """Durable pointer to one exact raw revision awaiting its entry zone."""

    chat_id: int
    message_id: int
    revision: int
    raw_event_id: int
    updated_at_utc: datetime


@dataclass(frozen=True, slots=True)
class OrderIntent:
    account_id: str
    signal_id: str
    signal_revision: int
    leg_index: int
    broker_symbol: str
    side: str
    volume: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None
    entry_price: Decimal | None = None
    expected_risk: Decimal | None = None
    client_reference: str = ""
    request_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("account_id", self.account_id),
            ("signal_id", self.signal_id),
            ("broker_symbol", self.broker_symbol),
            ("side", self.side),
            ("client_reference", self.client_reference),
        ):
            if not str(value).strip():
                raise ValueError(f"{name} is required")
        if self.signal_revision < 0:
            raise ValueError("signal_revision must be non-negative")
        if self.leg_index < 0:
            raise ValueError("leg_index must be non-negative")
        if self.side.upper() not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if _as_decimal(self.volume, "volume") <= 0:
            raise ValueError("volume must be positive")
        if _as_decimal(self.stop_loss, "stop_loss") <= 0:
            raise ValueError("a positive numeric stop_loss is required")
        for name, value in (
            ("take_profit", self.take_profit),
            ("entry_price", self.entry_price),
        ):
            if value is not None and _as_decimal(value, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.expected_risk is not None and _as_decimal(
            self.expected_risk, "expected_risk"
        ) < 0:
            raise ValueError("expected_risk cannot be negative")


@dataclass(frozen=True, slots=True)
class OrderIntentRecord:
    id: int
    account_id: str
    signal_id: str
    signal_revision: int
    leg_index: int
    broker_symbol: str
    side: str
    volume: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None
    entry_price: Decimal | None
    expected_risk: Decimal | None
    client_reference: str
    request_metadata: Mapping[str, Any]
    payload_hash: str
    status: IntentStatus
    broker_order_id: str | None
    broker_deal_id: str | None
    broker_position_id: str | None
    last_error_code: str | None
    last_error_message: str | None
    created_at_utc: datetime
    updated_at_utc: datetime
    version: int


@dataclass(frozen=True, slots=True)
class IntentTransitionRecord:
    id: int
    intent_id: int
    from_status: IntentStatus | None
    to_status: IntentStatus
    happened_at_utc: datetime
    detail: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AppendResult:
    """The stored record and whether this call inserted it."""

    record: RawEventRecord | OrderIntentRecord
    created: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc_text(value: datetime, name: str) -> str:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _from_utc_text(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _as_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, int, or decimal text, not float")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _decimal_text(value: Any, name: str) -> str:
    return format(_as_decimal(value, name), "f")


def _optional_decimal_text(value: Any | None, name: str) -> str | None:
    return None if value is None else _decimal_text(value, name)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _as_utc_text(value, "metadata datetime")
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _canonical_json(value: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _decode_mapping(value: str) -> Mapping[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise StoreError("stored JSON metadata is not an object")
    return MappingProxyType(decoded)


def _payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


class SQLiteStore:
    """Single-process durable store with fail-closed state transitions."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.path = str(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=timeout_seconds,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
        self._initialize_schema()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _initialize_schema(self) -> None:
        statuses = ",".join(f"'{item.value}'" for item in IntentStatus)
        # sqlite3.executescript manages its own transaction boundary.  Keep it
        # outside `_transaction` so it cannot implicitly commit an active
        # BEGIN IMMEDIATE transaction.
        with self._lock:
            self._conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS raw_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL CHECK(message_id >= 0),
                    revision INTEGER NOT NULL CHECK(revision >= 0),
                    event_type TEXT NOT NULL,
                    observed_at_utc TEXT NOT NULL,
                    message_time_utc TEXT,
                    raw_text TEXT,
                    reply_to_message_id INTEGER,
                    forward_chat_id INTEGER,
                    forward_message_id INTEGER,
                    metadata_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    UNIQUE(chat_id, message_id, revision)
                );

                CREATE TRIGGER IF NOT EXISTS raw_events_no_update
                BEFORE UPDATE ON raw_events
                BEGIN
                    SELECT RAISE(ABORT, 'raw_events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS raw_events_no_delete
                BEFORE DELETE ON raw_events
                BEGIN
                    SELECT RAISE(ABORT, 'raw_events are append-only');
                END;

                CREATE TABLE IF NOT EXISTS waiting_entries (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL CHECK(message_id >= 0),
                    revision INTEGER NOT NULL CHECK(revision >= 0),
                    raw_event_id INTEGER NOT NULL UNIQUE REFERENCES raw_events(id),
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY(chat_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS order_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    signal_revision INTEGER NOT NULL CHECK(signal_revision >= 0),
                    leg_index INTEGER NOT NULL CHECK(leg_index >= 0),
                    broker_symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                    volume_text TEXT NOT NULL,
                    stop_loss_text TEXT NOT NULL,
                    take_profit_text TEXT,
                    entry_price_text TEXT,
                    expected_risk_text TEXT,
                    client_reference TEXT NOT NULL,
                    request_metadata_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    broker_order_id TEXT,
                    broker_deal_id TEXT,
                    broker_position_id TEXT,
                    last_error_code TEXT,
                    last_error_message TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
                    UNIQUE(account_id, signal_id, leg_index)
                );

                CREATE INDEX IF NOT EXISTS order_intents_status_idx
                    ON order_intents(status);
                CREATE INDEX IF NOT EXISTS order_intents_client_reference_idx
                    ON order_intents(client_reference);

                CREATE TABLE IF NOT EXISTS order_intent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id INTEGER NOT NULL REFERENCES order_intents(id),
                    from_status TEXT CHECK(from_status IS NULL OR from_status IN ({statuses})),
                    to_status TEXT NOT NULL CHECK(to_status IN ({statuses})),
                    happened_at_utc TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS order_intent_events_no_update
                BEFORE UPDATE ON order_intent_events
                BEGIN
                    SELECT RAISE(ABORT, 'order_intent_events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS order_intent_events_no_delete
                BEFORE DELETE ON order_intent_events
                BEGIN
                    SELECT RAISE(ABORT, 'order_intent_events are append-only');
                END;
                """
            )
        with self._transaction() as conn:
            current = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(self.SCHEMA_VERSION),),
                )
            elif int(current["value"]) != self.SCHEMA_VERSION:
                raise StoreError(
                    f"unsupported schema version {current['value']}; "
                    f"expected {self.SCHEMA_VERSION}"
                )

    def append_raw_event(self, event: RawEvent) -> AppendResult:
        metadata_json = _canonical_json(dict(event.metadata))
        intrinsic = {
            "chat_id": event.chat_id,
            "message_id": event.message_id,
            "revision": event.revision,
            "event_type": event.event_type,
            "message_time_utc": (
                _as_utc_text(event.message_time_utc, "message_time_utc")
                if event.message_time_utc is not None
                else None
            ),
            "raw_text": event.raw_text,
            "reply_to_message_id": event.reply_to_message_id,
            "forward_chat_id": event.forward_chat_id,
            "forward_message_id": event.forward_message_id,
            "metadata": json.loads(metadata_json),
        }
        fingerprint = _payload_hash(_canonical_json(intrinsic))
        observed = _as_utc_text(event.observed_at_utc, "observed_at_utc")
        with self._transaction() as conn:
            existing = conn.execute(
                """
                SELECT * FROM raw_events
                WHERE chat_id = ? AND message_id = ? AND revision = ?
                """,
                (event.chat_id, event.message_id, event.revision),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != fingerprint:
                    raise PersistenceConflictError(
                        "raw event identity already exists with different evidence"
                    )
                conn.execute(
                    """
                    DELETE FROM waiting_entries
                    WHERE chat_id = ? AND message_id = ? AND revision < ?
                    """,
                    (event.chat_id, event.message_id, event.revision),
                )
                return AppendResult(self._raw_event_from_row(existing), False)
            cursor = conn.execute(
                """
                INSERT INTO raw_events(
                    chat_id, message_id, revision, event_type,
                    observed_at_utc, message_time_utc, raw_text,
                    reply_to_message_id, forward_chat_id, forward_message_id,
                    metadata_json, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.chat_id,
                    event.message_id,
                    event.revision,
                    event.event_type,
                    observed,
                    intrinsic["message_time_utc"],
                    event.raw_text,
                    event.reply_to_message_id,
                    event.forward_chat_id,
                    event.forward_message_id,
                    metadata_json,
                    fingerprint,
                ),
            )
            row = conn.execute(
                "SELECT * FROM raw_events WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            assert row is not None
            # Newer Telegram evidence atomically revokes execution authority
            # from an older revision, even if the caller crashes immediately
            # after this transaction commits.
            conn.execute(
                """
                DELETE FROM waiting_entries
                WHERE chat_id = ? AND message_id = ? AND revision < ?
                """,
                (event.chat_id, event.message_id, event.revision),
            )
            return AppendResult(self._raw_event_from_row(row), True)

    def get_raw_event(
        self, chat_id: int, message_id: int, revision: int
    ) -> RawEventRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM raw_events
                WHERE chat_id = ? AND message_id = ? AND revision = ?
                """,
                (chat_id, message_id, revision),
            ).fetchone()
        return None if row is None else self._raw_event_from_row(row)

    def list_raw_events(self, *, chat_id: int | None = None) -> list[RawEventRecord]:
        sql = "SELECT * FROM raw_events"
        params: tuple[Any, ...] = ()
        if chat_id is not None:
            sql += " WHERE chat_id = ?"
            params = (chat_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._raw_event_from_row(row) for row in rows]

    def register_waiting_entry(self, raw_event: RawEventRecord) -> WaitingEntryRecord:
        """Register only the latest exact raw revision as actively waiting.

        The runtime calls this method only after ``TradingEngine`` returns
        ``WAITING_ENTRY``.  A lower revision can never replace a newer edit.
        """

        if not isinstance(raw_event, RawEventRecord):
            raise TypeError("raw_event must be a RawEventRecord")
        updated = _as_utc_text(self._clock(), "waiting updated_at_utc")
        with self._transaction() as conn:
            durable = conn.execute(
                """
                SELECT id, chat_id, message_id, revision
                FROM raw_events WHERE id = ?
                """,
                (raw_event.id,),
            ).fetchone()
            if durable is None or (
                int(durable["chat_id"]) != raw_event.chat_id
                or int(durable["message_id"]) != raw_event.message_id
                or int(durable["revision"]) != raw_event.revision
            ):
                raise StoreError("waiting entry does not match exact durable raw evidence")
            latest = conn.execute(
                """
                SELECT MAX(revision) AS revision
                FROM raw_events WHERE chat_id = ? AND message_id = ?
                """,
                (raw_event.chat_id, raw_event.message_id),
            ).fetchone()
            if latest is None or int(latest["revision"]) != raw_event.revision:
                raise PersistenceConflictError(
                    "an older raw revision cannot be registered as the latest waiting entry"
                )
            current = conn.execute(
                """
                SELECT * FROM waiting_entries
                WHERE chat_id = ? AND message_id = ?
                """,
                (raw_event.chat_id, raw_event.message_id),
            ).fetchone()
            if current is not None:
                current_revision = int(current["revision"])
                if raw_event.revision < current_revision:
                    raise PersistenceConflictError(
                        "an older raw revision cannot replace the latest waiting entry"
                    )
                if raw_event.revision == current_revision:
                    if int(current["raw_event_id"]) != raw_event.id:
                        raise PersistenceConflictError(
                            "waiting revision points to different raw evidence"
                        )
                    return self._waiting_entry_from_row(current)
                conn.execute(
                    """
                    UPDATE waiting_entries
                    SET revision = ?, raw_event_id = ?, updated_at_utc = ?
                    WHERE chat_id = ? AND message_id = ?
                    """,
                    (
                        raw_event.revision,
                        raw_event.id,
                        updated,
                        raw_event.chat_id,
                        raw_event.message_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO waiting_entries(
                        chat_id, message_id, revision, raw_event_id, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        raw_event.chat_id,
                        raw_event.message_id,
                        raw_event.revision,
                        raw_event.id,
                        updated,
                    ),
                )
            row = conn.execute(
                """
                SELECT * FROM waiting_entries
                WHERE chat_id = ? AND message_id = ?
                """,
                (raw_event.chat_id, raw_event.message_id),
            ).fetchone()
            assert row is not None
            return self._waiting_entry_from_row(row)

    def clear_waiting_entry(
        self,
        chat_id: int,
        message_id: int,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        """Remove one waiting pointer, optionally guarded by exact revision."""

        if type(chat_id) is not int or type(message_id) is not int:
            raise TypeError("chat_id and message_id must be integers")
        if message_id < 0:
            raise ValueError("message_id must be non-negative")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        sql = "DELETE FROM waiting_entries WHERE chat_id = ? AND message_id = ?"
        params: tuple[Any, ...] = (chat_id, message_id)
        if expected_revision is not None:
            sql += " AND revision = ?"
            params += (expected_revision,)
        with self._transaction() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount == 1

    def list_waiting_entries(self) -> list[WaitingEntryRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM waiting_entries
                ORDER BY chat_id, message_id
                """
            ).fetchall()
        return [self._waiting_entry_from_row(row) for row in rows]

    def list_waiting_raw_events(self) -> list[RawEventRecord]:
        """Load exact raw rows referenced by the durable waiting registry."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT raw_events.*
                FROM waiting_entries
                JOIN raw_events
                  ON raw_events.id = waiting_entries.raw_event_id
                 AND raw_events.chat_id = waiting_entries.chat_id
                 AND raw_events.message_id = waiting_entries.message_id
                 AND raw_events.revision = waiting_entries.revision
                WHERE NOT EXISTS (
                    SELECT 1 FROM raw_events AS newer
                    WHERE newer.chat_id = waiting_entries.chat_id
                      AND newer.message_id = waiting_entries.message_id
                      AND newer.revision > waiting_entries.revision
                )
                ORDER BY waiting_entries.chat_id, waiting_entries.message_id
                """
            ).fetchall()
            count = int(
                self._conn.execute("SELECT COUNT(*) FROM waiting_entries").fetchone()[0]
            )
        if len(rows) != count:
            raise StoreError("waiting registry does not match exact raw evidence")
        return [self._raw_event_from_row(row) for row in rows]

    def create_order_intent(self, intent: OrderIntent) -> AppendResult:
        volume = _decimal_text(intent.volume, "volume")
        stop_loss = _decimal_text(intent.stop_loss, "stop_loss")
        take_profit = _optional_decimal_text(intent.take_profit, "take_profit")
        entry_price = _optional_decimal_text(intent.entry_price, "entry_price")
        expected_risk = _optional_decimal_text(intent.expected_risk, "expected_risk")
        metadata_json = _canonical_json(dict(intent.request_metadata))
        immutable = {
            "account_id": intent.account_id,
            "signal_id": intent.signal_id,
            "signal_revision": intent.signal_revision,
            "leg_index": intent.leg_index,
            "broker_symbol": intent.broker_symbol,
            "side": intent.side.upper(),
            "volume": volume,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_price": entry_price,
            "expected_risk": expected_risk,
            "client_reference": intent.client_reference,
            "request_metadata": json.loads(metadata_json),
        }
        fingerprint = _payload_hash(_canonical_json(immutable))
        now = _as_utc_text(self._clock(), "clock result")
        with self._transaction() as conn:
            existing = conn.execute(
                """
                SELECT * FROM order_intents
                WHERE account_id = ? AND signal_id = ? AND leg_index = ?
                """,
                (intent.account_id, intent.signal_id, intent.leg_index),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != fingerprint:
                    raise PersistenceConflictError(
                        "order intent identity already exists with different immutable data"
                    )
                return AppendResult(self._intent_from_row(existing), False)
            cursor = conn.execute(
                """
                INSERT INTO order_intents(
                    account_id, signal_id, signal_revision, leg_index,
                    broker_symbol, side, volume_text, stop_loss_text,
                    take_profit_text, entry_price_text, expected_risk_text,
                    client_reference, request_metadata_json, payload_hash,
                    status, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.account_id,
                    intent.signal_id,
                    intent.signal_revision,
                    intent.leg_index,
                    intent.broker_symbol,
                    intent.side.upper(),
                    volume,
                    stop_loss,
                    take_profit,
                    entry_price,
                    expected_risk,
                    intent.client_reference,
                    metadata_json,
                    fingerprint,
                    IntentStatus.INTENT_PERSISTED.value,
                    now,
                    now,
                ),
            )
            intent_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO order_intent_events(
                    intent_id, from_status, to_status, happened_at_utc, detail_json
                ) VALUES (?, NULL, ?, ?, ?)
                """,
                (
                    intent_id,
                    IntentStatus.INTENT_PERSISTED.value,
                    now,
                    _canonical_json({"reason": "intent_created"}),
                ),
            )
            row = conn.execute(
                "SELECT * FROM order_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            assert row is not None
            return AppendResult(self._intent_from_row(row), True)

    def get_order_intent(self, intent_id: int) -> OrderIntentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM order_intents WHERE id = ?", (intent_id,)
            ).fetchone()
        return None if row is None else self._intent_from_row(row)

    def find_order_intent(
        self, account_id: str, signal_id: str, leg_index: int
    ) -> OrderIntentRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM order_intents
                WHERE account_id = ? AND signal_id = ? AND leg_index = ?
                """,
                (account_id, signal_id, leg_index),
            ).fetchone()
        return None if row is None else self._intent_from_row(row)

    def list_order_intents(
        self, *, statuses: Sequence[IntentStatus | str] | None = None
    ) -> list[OrderIntentRecord]:
        sql = "SELECT * FROM order_intents"
        params: tuple[Any, ...] = ()
        if statuses is not None:
            normalized = tuple(IntentStatus(item).value for item in statuses)
            if not normalized:
                return []
            placeholders = ",".join("?" for _ in normalized)
            sql += f" WHERE status IN ({placeholders})"
            params = normalized
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._intent_from_row(row) for row in rows]

    def unfinished_intents(self) -> list[OrderIntentRecord]:
        return self.list_order_intents(
            statuses=(
                IntentStatus.INTENT_PERSISTED,
                IntentStatus.SUBMITTING,
                IntentStatus.RECONCILE_REQUIRED,
                IntentStatus.OPEN,
                IntentStatus.PARTIAL_OPEN,
            )
        )

    def transition_order_intent(
        self,
        intent_id: int,
        to_status: IntentStatus | str,
        *,
        expected_status: IntentStatus | str | None = None,
        broker_order_id: str | int | None = None,
        broker_deal_id: str | int | None = None,
        broker_position_id: str | int | None = None,
        error_code: str | int | None = None,
        error_message: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> OrderIntentRecord:
        target = IntentStatus(to_status)
        expected = IntentStatus(expected_status) if expected_status is not None else None
        updates = {
            "broker_order_id": None if broker_order_id is None else str(broker_order_id),
            "broker_deal_id": None if broker_deal_id is None else str(broker_deal_id),
            "broker_position_id": (
                None if broker_position_id is None else str(broker_position_id)
            ),
            "last_error_code": None if error_code is None else str(error_code),
            "last_error_message": error_message,
        }
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM order_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise IntentNotFoundError(f"order intent {intent_id} does not exist")
            current = IntentStatus(row["status"])
            if expected is not None and current is not expected:
                raise ConcurrentTransitionError(
                    f"expected {expected.value}, found {current.value}"
                )
            if current is target:
                for column, proposed in updates.items():
                    if proposed is not None and row[column] != proposed:
                        raise PersistenceConflictError(
                            f"idempotent transition conflicts on {column}"
                        )
                return self._intent_from_row(row)
            if target not in _ALLOWED_TRANSITIONS[current]:
                raise InvalidIntentTransitionError(
                    f"cannot transition {current.value} to {target.value}"
                )
            now = _as_utc_text(self._clock(), "clock result")
            merged: dict[str, str | None] = {}
            for column, proposed in updates.items():
                existing = row[column]
                immutable_broker_id = column in {
                    "broker_order_id",
                    "broker_deal_id",
                    "broker_position_id",
                }
                if (
                    immutable_broker_id
                    and proposed is not None
                    and existing is not None
                    and existing != proposed
                ):
                    raise PersistenceConflictError(
                        f"cannot replace existing {column} on intent {intent_id}"
                    )
                merged[column] = existing if proposed is None else proposed
            cursor = conn.execute(
                """
                UPDATE order_intents
                SET status = ?, broker_order_id = ?, broker_deal_id = ?,
                    broker_position_id = ?, last_error_code = ?,
                    last_error_message = ?, updated_at_utc = ?, version = version + 1
                WHERE id = ? AND version = ?
                """,
                (
                    target.value,
                    merged["broker_order_id"],
                    merged["broker_deal_id"],
                    merged["broker_position_id"],
                    merged["last_error_code"],
                    merged["last_error_message"],
                    now,
                    intent_id,
                    row["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentTransitionError(
                    f"order intent {intent_id} changed concurrently"
                )
            transition_detail = dict(detail or {})
            transition_detail.update(
                {
                    key: value
                    for key, value in updates.items()
                    if value is not None
                }
            )
            conn.execute(
                """
                INSERT INTO order_intent_events(
                    intent_id, from_status, to_status, happened_at_utc, detail_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    current.value,
                    target.value,
                    now,
                    _canonical_json(transition_detail),
                ),
            )
            updated = conn.execute(
                "SELECT * FROM order_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            assert updated is not None
            return self._intent_from_row(updated)

    def list_intent_transitions(self, intent_id: int) -> list[IntentTransitionRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM order_intent_events
                WHERE intent_id = ? ORDER BY id
                """,
                (intent_id,),
            ).fetchall()
        return [self._transition_from_row(row) for row in rows]

    @staticmethod
    def _raw_event_from_row(row: sqlite3.Row) -> RawEventRecord:
        observed = _from_utc_text(row["observed_at_utc"])
        assert observed is not None
        return RawEventRecord(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            message_id=int(row["message_id"]),
            revision=int(row["revision"]),
            event_type=row["event_type"],
            observed_at_utc=observed,
            message_time_utc=_from_utc_text(row["message_time_utc"]),
            raw_text=row["raw_text"],
            reply_to_message_id=row["reply_to_message_id"],
            forward_chat_id=row["forward_chat_id"],
            forward_message_id=row["forward_message_id"],
            metadata=_decode_mapping(row["metadata_json"]),
            payload_hash=row["payload_hash"],
        )

    @staticmethod
    def _waiting_entry_from_row(row: sqlite3.Row) -> WaitingEntryRecord:
        updated = _from_utc_text(row["updated_at_utc"])
        assert updated is not None
        return WaitingEntryRecord(
            chat_id=int(row["chat_id"]),
            message_id=int(row["message_id"]),
            revision=int(row["revision"]),
            raw_event_id=int(row["raw_event_id"]),
            updated_at_utc=updated,
        )

    @staticmethod
    def _intent_from_row(row: sqlite3.Row) -> OrderIntentRecord:
        created = _from_utc_text(row["created_at_utc"])
        updated = _from_utc_text(row["updated_at_utc"])
        assert created is not None and updated is not None
        return OrderIntentRecord(
            id=int(row["id"]),
            account_id=row["account_id"],
            signal_id=row["signal_id"],
            signal_revision=int(row["signal_revision"]),
            leg_index=int(row["leg_index"]),
            broker_symbol=row["broker_symbol"],
            side=row["side"],
            volume=Decimal(row["volume_text"]),
            stop_loss=Decimal(row["stop_loss_text"]),
            take_profit=(
                None
                if row["take_profit_text"] is None
                else Decimal(row["take_profit_text"])
            ),
            entry_price=(
                None
                if row["entry_price_text"] is None
                else Decimal(row["entry_price_text"])
            ),
            expected_risk=(
                None
                if row["expected_risk_text"] is None
                else Decimal(row["expected_risk_text"])
            ),
            client_reference=row["client_reference"],
            request_metadata=_decode_mapping(row["request_metadata_json"]),
            payload_hash=row["payload_hash"],
            status=IntentStatus(row["status"]),
            broker_order_id=row["broker_order_id"],
            broker_deal_id=row["broker_deal_id"],
            broker_position_id=row["broker_position_id"],
            last_error_code=row["last_error_code"],
            last_error_message=row["last_error_message"],
            created_at_utc=created,
            updated_at_utc=updated,
            version=int(row["version"]),
        )

    @staticmethod
    def _transition_from_row(row: sqlite3.Row) -> IntentTransitionRecord:
        happened = _from_utc_text(row["happened_at_utc"])
        assert happened is not None
        return IntentTransitionRecord(
            id=int(row["id"]),
            intent_id=int(row["intent_id"]),
            from_status=(
                None if row["from_status"] is None else IntentStatus(row["from_status"])
            ),
            to_status=IntentStatus(row["to_status"]),
            happened_at_utc=happened,
            detail=_decode_mapping(row["detail_json"]),
        )


__all__ = [
    "AppendResult",
    "ConcurrentTransitionError",
    "IntentNotFoundError",
    "IntentStatus",
    "IntentTransitionRecord",
    "InvalidIntentTransitionError",
    "OrderIntent",
    "OrderIntentRecord",
    "PersistenceConflictError",
    "RawEvent",
    "RawEventRecord",
    "SQLiteStore",
    "StoreError",
    "WaitingEntryRecord",
]
