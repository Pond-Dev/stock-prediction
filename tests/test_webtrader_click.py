from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import html
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

from tgxm.webtrader_click import (
    PlaywrightWebTraderClicker,
    WebTraderAlreadyCommittedError,
    WebTraderAmbiguousOutcomeError,
    WebTraderConfigurationError,
    WebTraderFormDriftError,
    WebTraderIdentityError,
    WebTraderUIContractError,
)


ORIGIN = "https://webtrader.example.test"
URL = f"{ORIGIN}/terminal"


def _browser_channel() -> str | None:
    pytest.importorskip("playwright.sync_api")
    chrome_candidates = (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    )
    if any(candidate.is_file() for candidate in chrome_candidates):
        return "chrome"
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return "chrome"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            if Path(playwright.chromium.executable_path).is_file():
                return None
    except Exception:
        pass
    pytest.skip("a Playwright Chromium/Chrome executable is not installed")


def _order(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "account_id": "12345678",
        "symbol": "GOLD",
        "side": "BUY",
        "volume": Decimal("0.01"),
        "stop_loss": Decimal("1990.00"),
        "take_profit": Decimal("2010.00"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _inner_html(
    *,
    login: str = "12345678",
    server: str = "XMGlobal-MT5 Demo 1",
    mode: str = "Demo",
    ask: str = "2000.20",
    duplicate_volume: bool = False,
    result: str = "receipt",
    structured_receipt_ids: bool = True,
    include_side_control: bool = True,
    mutate_volume_after_prepare: bool = False,
) -> str:
    extra_volume = (
        '<input name="volume" aria-label="Volume in lots" value="0.01">'
        if duplicate_volume
        else ""
    )
    result_script = {
        "receipt": """
            document.querySelector('[name="order-confirmation"]').hidden = false;
        """,
        "reject": """
            document.querySelector('[name="order-error"]').hidden = false;
        """,
        "none": "",
    }[result]
    receipt_body = (
        """
        <output name="order-id" aria-label="Order ID">Order ID: 700001</output>
        <output name="deal-id" aria-label="Deal ID">Deal ID: 800001</output>
        <output name="position-id" aria-label="Position ID">Position ID: 900001</output>
        """
        if structured_receipt_ids
        else "Price 2000.20 Volume 0.01"
    )
    side_control = (
        """
      <label>Side
        <select name="side" aria-label="Side">
          <option value="BUY">BUY</option>
          <option value="SELL">SELL</option>
        </select>
      </label>
        """
        if include_side_control
        else ""
    )
    final_buttons = (
        '<button type="button" data-final name="Place order" '
        'aria-label="Place order">Place order</button>'
        if include_side_control
        else (
            '<button type="button" data-final name="Buy by Market" '
            'aria-label="Buy by Market">Buy by Market</button>'
            '<button type="button" data-final name="Sell by Market" '
            'aria-label="Sell by Market">Sell by Market</button>'
        )
    )
    mutation_script = (
        """
      document.querySelector('[name="take-profit"]').addEventListener('input', () => {
        setTimeout(() => {
          document.querySelector('[name="volume"]').value = '0.02';
        }, 100);
      });
        """
        if mutate_volume_after_prepare
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
  <body>
    <header>
      <output name="account-login" aria-label="Account login">{html.escape(login)}</output>
      <output name="account-server" aria-label="Server">{html.escape(server)}</output>
      <output name="account-mode" aria-label="Account type">{html.escape(mode)}</output>
    </header>
    <main>
      <label>Symbol
        <select name="symbol" aria-label="Symbol">
          <option value="EURUSD">EURUSD</option>
          <option value="GOLD">GOLD</option>
        </select>
      </label>
      {side_control}
      <label>Volume <input name="volume" aria-label="Volume" value="0.01"></label>
      {extra_volume}
      <label>Stop Loss <input name="stop-loss" aria-label="Stop Loss" value=""></label>
      <label>Take Profit <input name="take-profit" aria-label="Take Profit" value=""></label>
      <output name="bid" aria-label="Bid">2000.00</output>
      <output name="ask" aria-label="Ask">{html.escape(ask)}</output>
      {final_buttons}
      <div name="order-confirmation" role="status" aria-label="Order confirmation" hidden>
        {receipt_body}
      </div>
      <div name="order-error" role="alert" aria-label="Order error" hidden>Rejected</div>
    </main>
    <script>
      window.fixtureClickCount = 0;
      document.querySelectorAll('[data-final]').forEach(button => {{
        button.addEventListener('click', () => {{
          window.fixtureClickCount += 1;
          console.log('fixture-final-click');
          {result_script}
        }});
      }});
      {mutation_script}
    </script>
  </body>
</html>"""


def _page_setup(
    inner: str,
    *,
    iframe: bool,
    clicks: list[str],
    setup_threads: list[int] | None = None,
):
    outer = (
        "<!doctype html><html><body>"
        '<iframe title="MT5 WebTrader" src="/frame"></iframe>'
        "</body></html>"
    )

    def setup(page) -> None:
        if setup_threads is not None:
            setup_threads.append(threading.get_ident())
        page.on(
            "console",
            lambda message: clicks.append(message.text)
            if message.text == "fixture-final-click"
            else None,
        )

        def route_request(route) -> None:
            requested = route.request.url
            body = inner if (not iframe or requested.endswith("/frame")) else outer
            route.fulfill(status=200, content_type="text/html", body=body)

        page.route("**/*", route_request)

    return setup


def _clicker(
    tmp_path: Path,
    inner: str,
    *,
    iframe: bool = False,
    clicks: list[str] | None = None,
    receipt_timeout_seconds: float = 1.0,
    setup_threads: list[int] | None = None,
) -> PlaywrightWebTraderClicker:
    observed_clicks = clicks if clicks is not None else []
    return PlaywrightWebTraderClicker(
        url=URL,
        allowed_origins={ORIGIN},
        profile_dir=tmp_path / "dedicated-webtrader-profile",
        headless=True,
        browser_channel=_browser_channel(),
        action_timeout_seconds=3,
        receipt_timeout_seconds=receipt_timeout_seconds,
        _page_setup=_page_setup(
            inner,
            iframe=iframe,
            clicks=observed_clicks,
            setup_threads=setup_threads,
        ),
    )


def test_configuration_requires_an_exact_https_origin(tmp_path: Path) -> None:
    with pytest.raises(WebTraderConfigurationError, match="HTTPS"):
        PlaywrightWebTraderClicker(
            url="http://webtrader.example.test/terminal",
            allowed_origins={"http://webtrader.example.test"},
            profile_dir=tmp_path / "profile",
        )
    with pytest.raises(WebTraderConfigurationError, match="allowlisted"):
        PlaywrightWebTraderClicker(
            url="https://lookalike.example.test/terminal",
            allowed_origins={ORIGIN},
            profile_dir=tmp_path / "profile",
        )
    with pytest.raises(WebTraderConfigurationError, match="credentials"):
        PlaywrightWebTraderClicker(
            url="https://user:password@webtrader.example.test/terminal",
            allowed_origins={ORIGIN},
            profile_dir=tmp_path / "profile",
        )


@pytest.mark.parametrize("iframe", [False, True])
def test_prepare_and_commit_once_in_main_frame_or_mt5_iframe(
    tmp_path: Path, iframe: bool
) -> None:
    clicks: list[str] = []
    setup_threads: list[int] = []
    caller_thread = threading.get_ident()
    clicker = _clicker(
        tmp_path,
        _inner_html(),
        iframe=iframe,
        clicks=clicks,
        setup_threads=setup_threads,
    )
    try:
        clicker.initialize()
        identity = clicker.inspect_identity("12345678", "XMGlobal-MT5 Demo 1")
        assert identity.is_demo
        assert identity.origin == ORIGIN

        token = clicker.prepare_order(_order())
        receipt = clicker.commit_once(
            token,
            Decimal("2000.20"),
            Decimal("0.01"),
            Decimal("2"),
        )

        assert receipt.order_id == "700001"
        assert receipt.deal_id == "800001"
        assert receipt.position_id == "900001"
        assert receipt.origin == ORIGIN
        assert "12345678" not in repr(identity)
        assert "XMGlobal-MT5 Demo 1" not in repr(identity)
        assert "700001" not in repr(receipt)
        assert len(setup_threads) == 1
        assert setup_threads[0] != caller_thread
        with pytest.raises(WebTraderAlreadyCommittedError):
            clicker.commit_once(
                token,
                Decimal("2000.20"),
                Decimal("0.01"),
                Decimal("2"),
            )
        assert clicks == ["fixture-final-click"]
    finally:
        clicker.shutdown()


@pytest.mark.parametrize(
    ("inner", "login", "server"),
    [
        (_inner_html(mode="Live"), "12345678", "XMGlobal-MT5 Demo 1"),
        (_inner_html(login="87654321"), "12345678", "XMGlobal-MT5 Demo 1"),
        (_inner_html(server="XMGlobal-MT5 Demo 2"), "12345678", "XMGlobal-MT5 Demo 1"),
    ],
)
def test_live_or_mismatched_identity_is_rejected(
    tmp_path: Path, inner: str, login: str, server: str
) -> None:
    clicker = _clicker(tmp_path, inner)
    try:
        clicker.initialize()
        with pytest.raises(WebTraderIdentityError):
            clicker.inspect_identity(login, server)
    finally:
        clicker.shutdown()


def test_quote_drift_fails_before_the_final_click(tmp_path: Path) -> None:
    clicks: list[str] = []
    clicker = _clicker(
        tmp_path,
        _inner_html(ask="2001.00"),
        clicks=clicks,
    )
    try:
        clicker.initialize()
        clicker.inspect_identity("12345678", "XMGlobal-MT5 Demo 1")
        token = clicker.prepare_order(_order())
        with pytest.raises(WebTraderFormDriftError, match="quote drift"):
            clicker.commit_once(
                token,
                Decimal("2000.20"),
                Decimal("0.01"),
                Decimal("2"),
            )
        assert clicks == []
        with pytest.raises(WebTraderAlreadyCommittedError):
            clicker.commit_once(
                token,
                Decimal("2001.00"),
                Decimal("0.01"),
                Decimal("2"),
            )
    finally:
        clicker.shutdown()


def test_duplicate_locator_fails_closed_before_click(tmp_path: Path) -> None:
    clicks: list[str] = []
    clicker = _clicker(
        tmp_path,
        _inner_html(duplicate_volume=True),
        clicks=clicks,
    )
    try:
        clicker.initialize()
        clicker.inspect_identity("12345678", "XMGlobal-MT5 Demo 1")
        with pytest.raises(WebTraderUIContractError, match="volume.*ambiguous"):
            clicker.prepare_order(_order())
        assert clicks == []
    finally:
        clicker.shutdown()


def test_form_drift_after_prepare_fails_before_click(tmp_path: Path) -> None:
    clicks: list[str] = []
    clicker = _clicker(
        tmp_path,
        _inner_html(mutate_volume_after_prepare=True),
        clicks=clicks,
    )
    try:
        clicker.initialize()
        clicker.inspect_identity("12345678", "XMGlobal-MT5 Demo 1")
        token = clicker.prepare_order(_order())
        time.sleep(0.15)
        with pytest.raises(WebTraderFormDriftError, match="form"):
            clicker.commit_once(
                token,
                Decimal("2000.20"),
                Decimal("0.01"),
                Decimal("2"),
            )
        assert clicks == []
    finally:
        clicker.shutdown()


def test_side_specific_market_button_is_selected_without_a_prepare_click(
    tmp_path: Path,
) -> None:
    clicks: list[str] = []
    clicker = _clicker(
        tmp_path,
        _inner_html(include_side_control=False),
        clicks=clicks,
    )
    try:
        clicker.initialize()
        clicker.inspect_identity("12345678", "XMGlobal-MT5 Demo 1")
        token = clicker.prepare_order(_order(side="BUY"))
        assert clicks == []
        receipt = clicker.commit_once(
            token,
            Decimal("2000.20"),
            Decimal("0.01"),
            Decimal("2"),
        )
        assert receipt.order_id == "700001"
        assert clicks == ["fixture-final-click"]
    finally:
        clicker.shutdown()


def test_missing_receipt_is_ambiguous_and_never_clicks_twice(tmp_path: Path) -> None:
    clicks: list[str] = []
    clicker = _clicker(
        tmp_path,
        _inner_html(result="none"),
        clicks=clicks,
        receipt_timeout_seconds=0.2,
    )
    try:
        clicker.initialize()
        clicker.inspect_identity("12345678", "XMGlobal-MT5 Demo 1")
        token = clicker.prepare_order(_order())
        with pytest.raises(WebTraderAmbiguousOutcomeError, match="receipt"):
            clicker.commit_once(
                token,
                Decimal("2000.20"),
                Decimal("0.01"),
                Decimal("2"),
            )
        with pytest.raises(WebTraderAlreadyCommittedError):
            clicker.commit_once(
                token,
                Decimal("2000.20"),
                Decimal("0.01"),
                Decimal("2"),
            )
        assert clicks == ["fixture-final-click"]
    finally:
        clicker.shutdown()


def test_price_only_confirmation_is_not_mistaken_for_a_ticket(tmp_path: Path) -> None:
    clicks: list[str] = []
    clicker = _clicker(
        tmp_path,
        _inner_html(structured_receipt_ids=False),
        clicks=clicks,
        receipt_timeout_seconds=0.2,
    )
    try:
        clicker.initialize()
        clicker.inspect_identity("12345678", "XMGlobal-MT5 Demo 1")
        token = clicker.prepare_order(_order())
        with pytest.raises(WebTraderAmbiguousOutcomeError, match="receipt"):
            clicker.commit_once(
                token,
                Decimal("2000.20"),
                Decimal("0.01"),
                Decimal("2"),
            )
        assert clicks == ["fixture-final-click"]
    finally:
        clicker.shutdown()


def test_visible_rejection_is_structured_and_clicked_only_once(tmp_path: Path) -> None:
    from tgxm.webtrader_click import WebTraderRejectedError

    clicks: list[str] = []
    clicker = _clicker(
        tmp_path,
        _inner_html(result="reject"),
        clicks=clicks,
    )
    try:
        clicker.initialize()
        clicker.inspect_identity("12345678", "XMGlobal-MT5 Demo 1")
        token = clicker.prepare_order(_order())
        with pytest.raises(WebTraderRejectedError):
            clicker.commit_once(
                token,
                Decimal("2000.20"),
                Decimal("0.01"),
                Decimal("2"),
            )
        with pytest.raises(WebTraderAlreadyCommittedError):
            clicker.commit_once(
                token,
                Decimal("2000.20"),
                Decimal("0.01"),
                Decimal("2"),
            )
        assert clicks == ["fixture-final-click"]
    finally:
        clicker.shutdown()
