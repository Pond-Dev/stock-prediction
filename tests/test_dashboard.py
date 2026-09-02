from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import http.client
import json
from pathlib import Path
import threading
from typing import Iterator

import pytest

from tgxm.config import AppConfig, save_config
from tgxm.dashboard import (
    DashboardError,
    collect_dashboard_status,
    create_dashboard_server,
)
from tgxm.store import IntentStatus, OrderIntent, RawEvent, SQLiteStore


PRIVATE_TEXT = "PRIVATE_TELEGRAM_SIGNAL_334455"
PRIVATE_ACCOUNT = "PRIVATE_DEMO_ACCOUNT_778899"
PRIVATE_SERVER = "PRIVATE_XM_SERVER_VALUE"
PRIVATE_PROFILE = "private_gold_room"
PRIVATE_TERMINAL_PATH = "C:/PRIVATE_ACCOUNT_HOME/terminal64.exe"


def _write_config(path: Path) -> AppConfig:
    base = AppConfig.default()
    private_profile = replace(
        base.channels["mr_charlie"],
        peer_id=-100778899,
        trade_enabled=True,
    )
    config = replace(
        base,
        broker=replace(base.broker, terminal_path=PRIVATE_TERMINAL_PATH),
        channels={
            **base.channels,
            "mr_charlie": private_profile,
            PRIVATE_PROFILE: replace(
                private_profile,
                peer_id=-100889900,
                enabled=False,
                trade_enabled=False,
            ),
        },
    ).validate()
    save_config(config, path)
    return config


def _write_database(path: Path) -> None:
    now = datetime(2026, 8, 28, 3, 0, tzinfo=UTC)
    with SQLiteStore(path, clock=lambda: now) as store:
        raw = store.append_raw_event(
            RawEvent(
                chat_id=-100778899,
                message_id=44,
                revision=1,
                event_type="new",
                observed_at_utc=now,
                raw_text=PRIVATE_TEXT,
                metadata={"server": PRIVATE_SERVER},
            )
        ).record
        store.register_waiting_entry(raw)

        first = store.create_order_intent(
            OrderIntent(
                account_id=PRIVATE_ACCOUNT,
                signal_id="private-signal-one",
                signal_revision=1,
                leg_index=0,
                broker_symbol="GOLD",
                side="SELL",
                volume=Decimal("0.01"),
                stop_loss=Decimal("4618"),
                take_profit=Decimal("4595"),
                client_reference="private-reference-one",
                request_metadata={"server": PRIVATE_SERVER},
            )
        ).record
        store.transition_order_intent(
            first.id,
            IntentStatus.SUBMITTING,
            expected_status=IntentStatus.INTENT_PERSISTED,
        )
        store.transition_order_intent(
            first.id,
            IntentStatus.OPEN,
            expected_status=IntentStatus.SUBMITTING,
            broker_position_id="private-position-id",
        )
        store.create_order_intent(
            OrderIntent(
                account_id=PRIVATE_ACCOUNT,
                signal_id="private-signal-two",
                signal_revision=1,
                leg_index=0,
                broker_symbol="GOLD",
                side="BUY",
                volume=Decimal("0.01"),
                stop_loss=Decimal("4500"),
                take_profit=Decimal("4700"),
                client_reference="private-reference-two",
            )
        )


@contextmanager
def _running_server(config_path: Path, db_path: Path) -> Iterator[tuple[str, int]]:
    server = create_dashboard_server(config_path, db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(host: str, port: int, method: str, route: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request(method, route)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_collect_dashboard_status_contains_only_sanitized_aggregates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "settings.json"
    db_path = tmp_path / "runtime.sqlite3"
    config = _write_config(config_path)
    _write_database(db_path)
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_ACCOUNTS", PRIVATE_ACCOUNT)
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_SERVERS", PRIVATE_SERVER)

    status = collect_dashboard_status(config_path, db_path)

    assert status == {
        "safety": {
            "demo_only": True,
            "read_only": True,
            "order_actions_exposed": False,
        },
        "runtime": {"mode": "observe", "adapter": config.broker.adapter},
        "channels": {
            "configured_count": len(config.channels),
            "enabled_count": 1,
            "trade_enabled_count": 1,
        },
        "waiting": {"count": 1},
        "intents": {
            "count": 2,
            "status_counts": {"INTENT_PERSISTED": 1, "OPEN": 1},
        },
        "database": {"available": True},
    }
    rendered = json.dumps(status, sort_keys=True)
    for private_value in (
        PRIVATE_TEXT,
        PRIVATE_ACCOUNT,
        PRIVATE_SERVER,
        PRIVATE_PROFILE,
        PRIVATE_TERMINAL_PATH,
        "TGXM_ALLOWED_DEMO_ACCOUNTS",
        "TGXM_ALLOWED_DEMO_SERVERS",
        "-100778899",
        "-100889900",
    ):
        assert private_value not in rendered


def test_dashboard_serves_polished_html_and_json_from_loopback(tmp_path: Path) -> None:
    config_path = tmp_path / "settings.json"
    db_path = tmp_path / "runtime.sqlite3"
    _write_config(config_path)
    _write_database(db_path)

    with _running_server(config_path, db_path) as (host, port):
        assert host == "127.0.0.1"
        api_status, api_headers, api_body = _request(host, port, "GET", "/api/status")
        page_status, page_headers, page_body = _request(host, port, "GET", "/")

    assert api_status == 200
    assert api_headers["Content-Type"] == "application/json; charset=utf-8"
    assert api_headers["Cache-Control"] == "no-store"
    assert json.loads(api_body)["intents"]["count"] == 2

    assert page_status == 200
    assert page_headers["Content-Type"] == "text/html; charset=utf-8"
    assert "form-action 'none'" in page_headers["Content-Security-Policy"]
    html = page_body.decode("utf-8")
    assert "แดชบอร์ดควบคุมบอท" in html
    assert "อ่านอย่างเดียว • Demo" in html
    assert "สัญญาณรอเข้า" in html
    assert "หน้านี้อ่านข้อมูลอย่างเดียว" in html
    assert "<form" not in html.lower()
    for private_value in (
        PRIVATE_TEXT,
        PRIVATE_ACCOUNT,
        PRIVATE_SERVER,
        PRIVATE_PROFILE,
        PRIVATE_TERMINAL_PATH,
    ):
        assert private_value not in html


def test_dashboard_has_no_mutation_or_trading_routes(tmp_path: Path) -> None:
    config_path = tmp_path / "settings.json"
    db_path = tmp_path / "runtime.sqlite3"
    _write_config(config_path)
    _write_database(db_path)

    with _running_server(config_path, db_path) as (host, port):
        for route in (
            "/api/activate-demo",
            "/api/orders",
            "/api/submit-order",
            "/runtime/start",
        ):
            status, headers, body = _request(host, port, "POST", route)
            assert status == 405
            assert headers["Allow"] == "GET, HEAD"
            assert json.loads(body) == {"error": "read_only_dashboard"}

        status, _, body = _request(host, port, "GET", "/api/orders")
        assert status == 404
        assert json.loads(body) == {"error": "not_found"}

        status, _, body = _request(host, port, "GET", "/api/status")
        assert status == 200
        assert json.loads(body)["intents"]["count"] == 2


def test_dashboard_refuses_non_loopback_bind_and_does_not_create_missing_db(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "settings.json"
    db_path = tmp_path / "missing.sqlite3"
    _write_config(config_path)

    with pytest.raises(DashboardError, match="127.0.0.1"):
        create_dashboard_server(config_path, db_path, host="0.0.0.0", port=0)

    status = collect_dashboard_status(config_path, db_path)
    assert status["database"] == {"available": False}
    assert status["waiting"] == {"count": 0}
    assert status["intents"] == {"count": 0, "status_counts": {}}
    assert not db_path.exists()
