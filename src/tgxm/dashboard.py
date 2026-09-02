"""Read-only local status dashboard for TGXM.

The dashboard intentionally exposes only validated operating mode and adapter
names plus aggregate channel, waiting-entry, and order-intent counts.  It has
no mutation routes and never loads Telegram text, broker account details,
server names, or environment values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import urlsplit

from tgxm.config import CONFIG_PATH, ConfigError, load_config
from tgxm.store import IntentStatus


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DB_PATH = Path("state/tgxm.sqlite3")


class DashboardError(RuntimeError):
    """Raised when the local dashboard cannot be started safely."""


def _empty_database_counts(*, available: bool) -> dict[str, Any]:
    return {
        "available": available,
        "waiting_entry_count": 0,
        "intent_count": 0,
        "intent_status_counts": {},
    }


def _database_counts(path: Path) -> dict[str, Any]:
    """Read aggregate state through a SQLite read-only connection.

    A database that has not been created yet is a normal dashboard state.  A
    malformed, incompatible, or temporarily unavailable database is reported
    only as unavailable; exception text and filesystem paths are never exposed.
    """

    if not path.is_file():
        return _empty_database_counts(available=False)

    allowed_statuses = {item.value for item in IntentStatus}
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            waiting_count = int(
                connection.execute("SELECT COUNT(*) FROM waiting_entries").fetchone()[0]
            )
            intent_count = int(
                connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT status, COUNT(*)
                FROM order_intents
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        return _empty_database_counts(available=False)

    status_counts = {
        str(status): int(count)
        for status, count in rows
        if str(status) in allowed_statuses and int(count) > 0
    }
    return {
        "available": True,
        "waiting_entry_count": waiting_count,
        "intent_count": intent_count,
        "intent_status_counts": status_counts,
    }


def collect_dashboard_status(
    config_path: str | Path = CONFIG_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return the complete, deliberately sanitized dashboard payload."""

    config = load_config(config_path)
    channels = tuple(config.channels.values())
    database = _database_counts(Path(db_path))
    return {
        "safety": {
            "demo_only": True,
            "read_only": True,
            "order_actions_exposed": False,
        },
        "runtime": {
            "mode": config.runtime.mode,
            "adapter": config.broker.adapter,
        },
        "channels": {
            "configured_count": len(channels),
            "enabled_count": sum(profile.enabled for profile in channels),
            "trade_enabled_count": sum(profile.trade_enabled for profile in channels),
        },
        "waiting": {
            "count": database["waiting_entry_count"],
        },
        "intents": {
            "count": database["intent_count"],
            "status_counts": database["intent_status_counts"],
        },
        "database": {
            "available": database["available"],
        },
    }


def _status_unavailable_payload() -> dict[str, Any]:
    return {
        "error": "status_unavailable",
        "safety": {
            "demo_only": True,
            "read_only": True,
            "order_actions_exposed": False,
        },
    }


def _metric(label: str, value: object, tone: str = "") -> str:
    tone_class = f" metric--{tone}" if tone else ""
    return (
        f'<section class="metric{tone_class}">'
        f'<span class="metric__label">{escape(label)}</span>'
        f'<strong class="metric__value">{escape(str(value))}</strong>'
        "</section>"
    )


def _render_dashboard(status: Mapping[str, Any]) -> bytes:
    runtime = status["runtime"]
    channels = status["channels"]
    waiting = status["waiting"]
    intents = status["intents"]
    database = status["database"]
    status_counts = intents["status_counts"]
    if status_counts:
        status_rows = "".join(
            "<li>"
            f"<span>{escape(str(name).replace('_', ' ').title())}</span>"
            f"<strong>{int(count)}</strong>"
            "</li>"
            for name, count in status_counts.items()
        )
    else:
        status_rows = '<li class="empty">ยังไม่มีคำสั่งที่บันทึกไว้</li>'

    database_label = "พร้อมอ่าน" if database["available"] else "ยังไม่ได้สร้าง"
    html = f"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>แดชบอร์ด TGXM</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #07111f;
      --panel: rgba(17, 32, 53, .82);
      --panel-strong: #12243a;
      --text: #ecf4ff;
      --muted: #91a4bc;
      --line: rgba(164, 190, 220, .16);
      --cyan: #5eead4;
      --blue: #60a5fa;
      --amber: #fbbf24;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background:
        radial-gradient(circle at 12% 8%, rgba(37, 99, 235, .25), transparent 32rem),
        radial-gradient(circle at 90% 88%, rgba(20, 184, 166, .16), transparent 28rem),
        var(--bg);
      font: 15px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    }}
    main {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0 56px; }}
    header {{ display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; }}
    .eyebrow {{ margin: 0 0 7px; color: var(--cyan); font-size: 12px; font-weight: 800;
      letter-spacing: .16em; text-transform: uppercase; }}
    h1 {{ margin: 0; font-size: clamp(30px, 5vw, 52px); line-height: 1.04; letter-spacing: -.04em; }}
    .subtitle {{ max-width: 650px; margin: 14px 0 0; color: var(--muted); font-size: 16px; }}
    .badge {{ display: inline-flex; align-items: center; gap: 8px; flex: 0 0 auto;
      border: 1px solid rgba(94, 234, 212, .35); border-radius: 999px; padding: 9px 13px;
      color: var(--cyan); background: rgba(13, 148, 136, .1); font-weight: 750; }}
    .badge::before {{ content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--cyan);
      box-shadow: 0 0 16px var(--cyan); }}
    .grid {{ display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; margin-top: 32px; }}
    .metric, .panel {{ border: 1px solid var(--line); border-radius: 18px; background: var(--panel);
      box-shadow: 0 18px 60px rgba(0, 0, 0, .18); backdrop-filter: blur(18px); }}
    .metric {{ grid-column: span 3; min-height: 132px; padding: 22px; display: flex;
      flex-direction: column; justify-content: space-between; }}
    .metric__label {{ color: var(--muted); font-size: 13px; font-weight: 700; text-transform: uppercase;
      letter-spacing: .08em; }}
    .metric__value {{ font-size: 27px; line-height: 1.1; letter-spacing: -.025em; overflow-wrap: anywhere; }}
    .metric--safe {{ border-color: rgba(94, 234, 212, .25); }}
    .panel {{ grid-column: span 6; padding: 24px; }}
    .panel h2 {{ margin: 0 0 18px; font-size: 17px; }}
    .counts {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
    .count {{ padding: 15px; border-radius: 13px; background: rgba(5, 13, 25, .48); }}
    .count span {{ display: block; color: var(--muted); font-size: 12px; }}
    .count strong {{ display: block; margin-top: 4px; font-size: 22px; }}
    ul {{ list-style: none; margin: 0; padding: 0; }}
    li {{ display: flex; justify-content: space-between; gap: 20px; padding: 10px 0;
      border-bottom: 1px solid var(--line); }}
    li:last-child {{ border-bottom: 0; }}
    li span, .empty {{ color: var(--muted); }}
    .notice {{ margin-top: 14px; padding: 18px 20px; border: 1px solid rgba(251, 191, 36, .24);
      border-radius: 16px; color: #fde68a; background: rgba(146, 64, 14, .12); }}
    footer {{ display: flex; justify-content: space-between; gap: 16px; margin-top: 22px;
      color: var(--muted); font-size: 12px; }}
    @media (max-width: 820px) {{
      header {{ display: block; }} .badge {{ margin-top: 20px; }}
      .metric {{ grid-column: span 6; }} .panel {{ grid-column: span 12; }}
    }}
    @media (max-width: 520px) {{
      main {{ width: min(100% - 20px, 1120px); padding-top: 24px; }}
      .metric {{ grid-column: span 12; min-height: 110px; }} .counts {{ grid-template-columns: 1fr; }}
      footer {{ display: block; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <p class="eyebrow">Telegram to XM</p>
        <h1>แดชบอร์ดควบคุมบอท</h1>
        <p class="subtitle">ดูสถานะรวมบนเครื่องนี้เท่านั้น โดยไม่แสดงข้อความ Telegram,
          รหัสผ่าน เลขบัญชี เซิร์ฟเวอร์ หรือปุ่มส่งคำสั่งซื้อขาย</p>
      </div>
      <span class="badge">อ่านอย่างเดียว • Demo</span>
    </header>

    <div class="grid">
      {_metric("โหมดทำงาน", runtime["mode"], "safe")}
      {_metric("ช่องทางส่งคำสั่ง", runtime["adapter"])}
      {_metric("สัญญาณรอเข้า", waiting["count"])}
      {_metric("คำสั่งที่บันทึก", intents["count"])}

      <section class="panel">
        <h2>ช่อง Telegram</h2>
        <div class="counts">
          <div class="count"><span>ตั้งค่าแล้ว</span><strong>{int(channels["configured_count"])}</strong></div>
          <div class="count"><span>เปิดอ่าน</span><strong>{int(channels["enabled_count"])}</strong></div>
          <div class="count"><span>อนุญาตเทรด</span><strong>{int(channels["trade_enabled_count"])}</strong></div>
        </div>
      </section>

      <section class="panel">
        <h2>สถานะคำสั่ง</h2>
        <ul>{status_rows}</ul>
      </section>
    </div>

    <aside class="notice"><strong>ขอบเขตความปลอดภัย:</strong> หน้านี้อ่านข้อมูลอย่างเดียว
      ไม่สามารถเปิด Demo Active ส่งออเดอร์ หรือเปิดการเทรด Live ได้</aside>
    <footer><span>SQLite: {escape(database_label)}</span><span>รีเฟรชหน้าเพื่อดูข้อมูลล่าสุด</span></footer>
  </main>
</body>
</html>
"""
    return html.encode("utf-8")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve the two read-only dashboard endpoints."""

    server_version = "TGXMDashboard/1"
    sys_version = ""

    def __init__(
        self,
        *args: Any,
        config_path: Path,
        db_path: Path,
        **kwargs: Any,
    ) -> None:
        self._config_path = config_path
        self._db_path = db_path
        super().__init__(*args, **kwargs)

    def _host_is_local(self) -> bool:
        host_header = self.headers.get("Host", "")
        try:
            hostname = urlsplit(f"//{host_header}").hostname
        except ValueError:
            return False
        return hostname in {LOOPBACK_HOST, "localhost"}

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send(
        self,
        status_code: int,
        body: bytes,
        content_type: str,
        *,
        include_body: bool,
        allow: str | None = None,
    ) -> None:
        self.send_response(status_code)
        self._security_headers()
        if allow is not None:
            self.send_header("Allow", allow)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _status(self) -> tuple[int, Mapping[str, Any]]:
        try:
            return 200, collect_dashboard_status(self._config_path, self._db_path)
        except (ConfigError, OSError, ValueError):
            return 503, _status_unavailable_payload()

    def _serve_read(self, *, include_body: bool) -> None:
        if not self._host_is_local():
            self._send(
                421,
                _json_bytes({"error": "local_host_required"}),
                "application/json; charset=utf-8",
                include_body=include_body,
            )
            return

        route = urlsplit(self.path).path
        if route == "/api/status":
            status_code, payload = self._status()
            self._send(
                status_code,
                _json_bytes(payload),
                "application/json; charset=utf-8",
                include_body=include_body,
            )
            return
        if route == "/":
            status_code, payload = self._status()
            body = (
                _render_dashboard(payload)
                if status_code == 200
                else b"TGXM dashboard status is unavailable."
            )
            self._send(
                status_code,
                body,
                "text/html; charset=utf-8" if status_code == 200 else "text/plain; charset=utf-8",
                include_body=include_body,
            )
            return
        self._send(
            404,
            _json_bytes({"error": "not_found"}),
            "application/json; charset=utf-8",
            include_body=include_body,
        )

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._serve_read(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._serve_read(include_body=False)

    def _method_not_allowed(self) -> None:
        self._send(
            405,
            _json_bytes({"error": "read_only_dashboard"}),
            "application/json; charset=utf-8",
            include_body=True,
            allow="GET, HEAD",
        )

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed

    def log_message(self, format: str, *args: Any) -> None:
        # Paths are intentionally not written to terminal logs.
        return


def create_dashboard_server(
    config_path: str | Path = CONFIG_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    host: str = LOOPBACK_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Create a dashboard server bound strictly to IPv4 loopback."""

    if host != LOOPBACK_HOST:
        raise DashboardError(f"dashboard host must be {LOOPBACK_HOST}")
    if type(port) is not int or not 0 <= port <= 65_535:
        raise DashboardError("dashboard port must be an integer between 0 and 65535")
    handler = partial(
        DashboardRequestHandler,
        config_path=Path(config_path),
        db_path=Path(db_path),
    )
    try:
        server = ThreadingHTTPServer((LOOPBACK_HOST, port), handler)
    except OSError as exc:
        raise DashboardError("dashboard could not bind to the local port") from exc
    server.daemon_threads = True
    return server


def run_dashboard(
    config_path: str | Path = CONFIG_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    port: int = DEFAULT_PORT,
    output_fn: Callable[[str], None] = print,
) -> None:
    """Run the read-only dashboard until interrupted by the operator."""

    with create_dashboard_server(config_path, db_path, port=port) as server:
        output_fn(f"TGXM local dashboard: http://{LOOPBACK_HOST}:{server.server_port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            output_fn("TGXM local dashboard stopped.")


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_PORT",
    "LOOPBACK_HOST",
    "DashboardError",
    "collect_dashboard_status",
    "create_dashboard_server",
    "run_dashboard",
]
