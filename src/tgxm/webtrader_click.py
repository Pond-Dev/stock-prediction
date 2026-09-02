"""Fail-closed, Demo-only XM WebTrader click boundary.

The public methods are synchronous, while every Playwright object is created
and used on one dedicated background thread.  This lets the existing asyncio
runtime call the boundary without violating Playwright's thread-affinity
rules.  The module deliberately has no credential, cookie-export, screenshot,
trace, or arbitrary-JavaScript API.

This layer only performs the browser interaction.  A caller must persist an
order intent before :meth:`commit_once` and independently reconcile the
returned ticket(s) against read-only broker evidence.  A browser confirmation
alone is not proof that a protected broker position exists.
"""

from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import queue
import re
import secrets
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
from urllib.parse import SplitResult, urlsplit


class WebTraderError(RuntimeError):
    """Base class for the redacted WebTrader boundary errors."""


class WebTraderDependencyError(WebTraderError):
    """Playwright or the configured Chromium browser is unavailable."""


class WebTraderConfigurationError(WebTraderError):
    """A local browser configuration is unsafe or malformed."""


class WebTraderStateError(WebTraderError):
    """The browser boundary was used in an invalid lifecycle state."""


class WebTraderIdentityError(WebTraderError):
    """The visible account is not the exact expected Demo account."""


class WebTraderUIContractError(WebTraderError):
    """The expected WebTrader controls are missing or ambiguous."""


class WebTraderFormDriftError(WebTraderError):
    """The visible form or quote changed outside the approved request."""


class WebTraderAlreadyCommittedError(WebTraderError):
    """A prepared token has already entered its one permitted commit."""


class WebTraderAmbiguousOutcomeError(WebTraderError):
    """A click may have happened, but no exact receipt was observed."""


class WebTraderRejectedError(WebTraderError):
    """The WebTrader UI visibly rejected the submitted order."""


@runtime_checkable
class WebTraderOrder(Protocol):
    """Structural order contract accepted by :meth:`prepare_order`."""

    account_id: str
    symbol: str
    side: object
    volume: object
    stop_loss: object
    take_profit: object | None


@dataclass(frozen=True, slots=True)
class WebTraderIdentity:
    """Non-secret account identity read from the visible WebTrader UI."""

    login: str = field(repr=False)
    server: str = field(repr=False)
    is_demo: bool
    origin: str

    def __post_init__(self) -> None:
        if not self.login.strip() or not self.server.strip():
            raise ValueError("login and server are required")
        if not self.is_demo:
            raise ValueError("only a positively identified Demo account is valid")
        _canonical_origin(self.origin)


@dataclass(frozen=True, slots=True)
class PreparedOrderToken:
    """Opaque, process-local authority for one prepared form."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or len(self.value) < 20:
            raise ValueError("prepared token is invalid")


@dataclass(frozen=True, slots=True)
class WebTraderReceipt:
    """Structured ticket evidence extracted after exactly one final click."""

    order_id: str | None = field(repr=False)
    deal_id: str | None = field(repr=False)
    position_id: str | None = field(repr=False)
    clicked_at_utc: datetime
    origin: str

    def __post_init__(self) -> None:
        identifiers = (self.order_id, self.deal_id, self.position_id)
        if not any(identifiers):
            raise ValueError("at least one broker receipt identifier is required")
        for value in identifiers:
            if value is not None and not re.fullmatch(r"[0-9]+", value):
                raise ValueError("broker receipt identifiers must be numeric")
        if (
            self.clicked_at_utc.tzinfo is None
            or self.clicked_at_utc.utcoffset() is None
        ):
            raise ValueError("clicked_at_utc must be timezone-aware")
        object.__setattr__(
            self, "clicked_at_utc", self.clicked_at_utc.astimezone(UTC)
        )
        _canonical_origin(self.origin)


@runtime_checkable
class WebTraderClickExecutor(Protocol):
    """Browser-click interface consumed by a broker/coordinator wrapper."""

    def initialize(self) -> None: ...

    def shutdown(self) -> None: ...

    def inspect_identity(
        self, expected_login: str, expected_server: str
    ) -> WebTraderIdentity: ...

    def prepare_order(self, request: WebTraderOrder) -> PreparedOrderToken: ...

    def commit_once(
        self,
        token: PreparedOrderToken,
        expected_quote: Decimal,
        point: Decimal,
        max_drift_points: Decimal,
    ) -> WebTraderReceipt: ...


@dataclass(frozen=True, slots=True)
class _OrderValues:
    account_id: str
    symbol: str
    side: str
    volume: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None


@dataclass(frozen=True, slots=True)
class _PreparedOrder:
    token: PreparedOrderToken
    values: _OrderValues
    identity: WebTraderIdentity


@dataclass(slots=True)
class _WorkItem:
    operation: Callable[..., Any]
    arguments: tuple[Any, ...]
    future: Future[Any]


_PROFILE_SENTINEL = ".tgxm-webtrader-profile"
_PROFILE_SENTINEL_CONTENT = "TGXM dedicated WebTrader profile v1\n"
_ALLOWED_BROWSER_CHANNELS = frozenset({"chrome", "msedge", "chromium"})

_IFRAME_SELECTOR = (
    'iframe[title="MT5 WebTrader" i], '
    'iframe[title^="MT5 WebTrader " i], '
    'iframe[aria-label="MT5 WebTrader" i]'
)

_LOGIN_SELECTORS = (
    '[name="account-login" i]',
    '[name="account-number" i]',
    '[aria-label="Account login" i]',
    '[aria-label="Account number" i]',
    '[data-testid="account-login"]',
)
_SERVER_SELECTORS = (
    '[name="account-server" i]',
    '[aria-label="Account server" i]',
    '[aria-label="Server" i]',
    '[data-testid="account-server"]',
)
_MODE_SELECTORS = (
    '[name="account-mode" i]',
    '[name="account-type" i]',
    '[aria-label="Account mode" i]',
    '[aria-label="Account type" i]',
    '[data-testid="account-mode"]',
)
_NEW_ORDER_SELECTORS = (
    'button[name="New order" i]',
    'button[aria-label="New order" i]',
    '[role="button"][aria-label="New order" i]',
    '[data-testid="new-order"]',
)
_SYMBOL_SELECTORS = (
    '[name="symbol" i]',
    '[aria-label="Symbol" i]',
    '[data-testid="order-symbol"]',
)
_SIDE_SELECTORS = (
    'select[name="side" i]',
    'input[name="side" i]:not([type="radio"])',
    '[role="combobox"][aria-label="Side" i]',
    '[aria-label="Order side" i]',
    '[data-testid="order-side"]',
)
_VOLUME_SELECTORS = (
    '[name="volume" i]',
    '[name="lots" i]',
    '[aria-label="Volume" i]',
    '[aria-label="Volume in lots" i]',
    '[data-testid="order-volume"]',
)
_STOP_LOSS_SELECTORS = (
    '[name="stop-loss" i]',
    '[name="stop_loss" i]',
    '[name="sl" i]',
    '[aria-label="Stop Loss" i]',
    '[data-testid="order-stop-loss"]',
)
_TAKE_PROFIT_SELECTORS = (
    '[name="take-profit" i]',
    '[name="take_profit" i]',
    '[name="tp" i]',
    '[aria-label="Take Profit" i]',
    '[data-testid="order-take-profit"]',
)
_BID_SELECTORS = (
    '[name="bid" i]',
    '[aria-label="Bid" i]',
    '[data-testid="quote-bid"]',
)
_ASK_SELECTORS = (
    '[name="ask" i]',
    '[aria-label="Ask" i]',
    '[data-testid="quote-ask"]',
)
_FINAL_BUTTON_SELECTORS = (
    'button[name="Place order" i]',
    'button[aria-label="Place order" i]',
    '[role="button"][aria-label="Place order" i]',
    '[data-testid="place-order"]',
)
_BUY_FINAL_BUTTON_SELECTORS = (
    'button[name="Buy by Market" i]',
    'button[aria-label="Buy by Market" i]',
    '[role="button"][aria-label="Buy by Market" i]',
    '[data-testid="place-buy-order"]',
)
_SELL_FINAL_BUTTON_SELECTORS = (
    'button[name="Sell by Market" i]',
    'button[aria-label="Sell by Market" i]',
    '[role="button"][aria-label="Sell by Market" i]',
    '[data-testid="place-sell-order"]',
)
_CONFIRMATION_SELECTORS = (
    '[name="order-confirmation" i]',
    '[role="status"][aria-label="Order confirmation" i]',
    '[data-testid="order-confirmation"]',
)
_REJECTION_SELECTORS = (
    '[name="order-error" i]',
    '[role="alert"][aria-label="Order error" i]',
    '[data-testid="order-error"]',
)
_ORDER_ID_SELECTORS = (
    '[name="order-id" i]',
    '[aria-label="Order ID" i]',
    '[aria-label="Order ticket" i]',
    '[data-testid="order-id"]',
)
_DEAL_ID_SELECTORS = (
    '[name="deal-id" i]',
    '[aria-label="Deal ID" i]',
    '[aria-label="Deal ticket" i]',
    '[data-testid="deal-id"]',
)
_POSITION_ID_SELECTORS = (
    '[name="position-id" i]',
    '[aria-label="Position ID" i]',
    '[aria-label="Position ticket" i]',
    '[data-testid="position-id"]',
)

_LABEL_FALLBACKS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "account login": ("Account login", "Account number"),
        "account server": ("Server", "Account server"),
        "account mode": ("Account type", "Account mode"),
        "symbol": ("Symbol",),
        "side": ("Side", "Order side"),
        "volume": ("Volume", "Volume in lots"),
        "stop loss": ("Stop Loss",),
        "take profit": ("Take Profit",),
        "bid": ("Bid",),
        "ask": ("Ask",),
    }
)


def _canonical_origin(value: str) -> str:
    """Return a canonical HTTPS origin and reject credentials/wildcards."""

    if not isinstance(value, str) or not value.strip():
        raise WebTraderConfigurationError("an HTTPS origin is required")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise WebTraderConfigurationError("WebTrader origin is malformed") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise WebTraderConfigurationError("WebTrader origin must use exact HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise WebTraderConfigurationError("WebTrader URLs must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise WebTraderConfigurationError(
            "an allowlisted WebTrader origin must not contain a path, query, or fragment"
        )
    host = parsed.hostname.lower()
    if "*" in host:
        raise WebTraderConfigurationError("wildcard WebTrader origins are forbidden")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port in (None, 443):
        return f"https://{host}"
    return f"https://{host}:{port}"


def _validated_navigation_url(value: str, allowed_origins: frozenset[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebTraderConfigurationError("WebTrader URL is required")
    stripped = value.strip()
    if any(ord(character) < 32 for character in stripped):
        raise WebTraderConfigurationError("WebTrader URL contains control characters")
    try:
        parsed = urlsplit(stripped)
    except ValueError as exc:
        raise WebTraderConfigurationError("WebTrader URL is malformed") from exc
    if parsed.username is not None or parsed.password is not None:
        raise WebTraderConfigurationError("WebTrader URLs must not contain credentials")
    if _origin_from_split(parsed) not in allowed_origins:
        raise WebTraderConfigurationError(
            "WebTrader URL origin is not exactly allowlisted"
        )
    return stripped


def _origin_from_split(parsed: SplitResult) -> str:
    base = f"{parsed.scheme}://{parsed.netloc}"
    return _canonical_origin(base)


def _page_origin(url: str) -> str:
    try:
        return _origin_from_split(urlsplit(url))
    except (ValueError, WebTraderConfigurationError) as exc:
        raise WebTraderIdentityError(
            "WebTrader navigated outside an approved HTTPS origin"
        ) from exc


def _required_text(value: object, field_name: str) -> str:
    text = str(value).strip()
    if not text or any(character in text for character in "\r\n\0"):
        raise WebTraderConfigurationError(f"{field_name} is invalid")
    return text


def _decimal(value: object, field_name: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise WebTraderConfigurationError(
            f"{field_name} must use Decimal, integer, or decimal text"
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise WebTraderConfigurationError(f"{field_name} must be a decimal") from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise WebTraderConfigurationError(f"{field_name} must be finite and {qualifier}")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _side_text(value: object) -> str:
    raw = getattr(value, "value", value)
    side = str(raw).strip().upper()
    if side not in {"BUY", "SELL"}:
        raise WebTraderConfigurationError("order side must be BUY or SELL")
    return side


def _order_values(request: WebTraderOrder) -> _OrderValues:
    try:
        account_id = _required_text(request.account_id, "account_id")
        symbol = _required_text(request.symbol, "symbol")
        side = _side_text(request.side)
        volume = _decimal(request.volume, "volume")
        stop_loss = _decimal(request.stop_loss, "stop_loss")
        take_profit_raw = request.take_profit
    except AttributeError as exc:
        raise WebTraderConfigurationError(
            "request does not implement the WebTraderOrder contract"
        ) from exc
    take_profit = (
        None
        if take_profit_raw is None
        else _decimal(take_profit_raw, "take_profit")
    )
    return _OrderValues(
        account_id=account_id,
        symbol=symbol,
        side=side,
        volume=volume,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


class PlaywrightWebTraderClicker:
    """Headed/headless persistent-browser implementation of the click layer.

    ``_page_setup`` exists only to let offline tests route an HTTPS URL to a
    local HTML fixture.  Production callers should leave it unset.
    """

    def __init__(
        self,
        *,
        url: str,
        allowed_origins: frozenset[str] | set[str] | tuple[str, ...],
        profile_dir: str | Path,
        headless: bool = False,
        browser_channel: str | None = None,
        action_timeout_seconds: float = 10.0,
        receipt_timeout_seconds: float = 10.0,
        _page_setup: Callable[[Any], None] | None = None,
    ) -> None:
        if isinstance(allowed_origins, (str, bytes)):
            raise WebTraderConfigurationError(
                "allowed_origins must be a collection of exact HTTPS origins"
            )
        origins = frozenset(_canonical_origin(item) for item in allowed_origins)
        if not origins:
            raise WebTraderConfigurationError(
                "at least one exact WebTrader HTTPS origin is required"
            )
        self._allowed_origins = origins
        self._url = _validated_navigation_url(url, origins)
        self._profile_dir = Path(profile_dir).resolve(strict=False)
        self._validate_profile_path()
        if not isinstance(headless, bool):
            raise WebTraderConfigurationError("headless must be true or false")
        if browser_channel is not None and browser_channel not in _ALLOWED_BROWSER_CHANNELS:
            raise WebTraderConfigurationError("browser_channel is not supported")
        self._headless = headless
        self._browser_channel = browser_channel
        self._action_timeout_ms = self._timeout_ms(
            action_timeout_seconds, "action_timeout_seconds"
        )
        self._receipt_timeout_ms = self._timeout_ms(
            receipt_timeout_seconds, "receipt_timeout_seconds"
        )
        self._page_setup = _page_setup

        self._lifecycle_lock = threading.RLock()
        self._queue: queue.Queue[_WorkItem | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._startup: Future[None] | None = None
        self._state = "NEW"
        self._known_tokens: set[str] = set()
        self._consumed_tokens: set[str] = set()

        # Worker-thread-only state.
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._verified_identity: WebTraderIdentity | None = None
        self._prepared: dict[str, _PreparedOrder] = {}

    @staticmethod
    def _timeout_ms(value: object, field_name: str) -> int:
        if isinstance(value, bool):
            raise WebTraderConfigurationError(f"{field_name} must be a number")
        try:
            seconds = float(value)
        except (TypeError, ValueError) as exc:
            raise WebTraderConfigurationError(f"{field_name} must be a number") from exc
        if not 0.1 <= seconds <= 120:
            raise WebTraderConfigurationError(
                f"{field_name} must be between 0.1 and 120 seconds"
            )
        return int(seconds * 1000)

    def _validate_profile_path(self) -> None:
        path = self._profile_dir
        if path == Path(path.anchor) or path == Path.home().resolve(strict=False):
            raise WebTraderConfigurationError(
                "profile_dir must be a dedicated subdirectory"
            )
        if path.exists() and not path.is_dir():
            raise WebTraderConfigurationError("profile_dir must be a directory")

    def initialize(self) -> None:
        with self._lifecycle_lock:
            if self._state == "RUNNING":
                return
            if self._state != "NEW":
                raise WebTraderStateError("WebTrader browser cannot be restarted")
            self._state = "STARTING"
            startup: Future[None] = Future()
            self._startup = startup
            thread = threading.Thread(
                target=self._worker_main,
                name="tgxm-webtrader",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        try:
            startup.result(timeout=(self._action_timeout_ms / 1000) + 15)
        except FutureTimeoutError as exc:
            with self._lifecycle_lock:
                self._state = "FAILED"
            raise WebTraderDependencyError(
                "WebTrader browser startup timed out"
            ) from exc
        except WebTraderError:
            with self._lifecycle_lock:
                self._state = "FAILED"
            raise
        except Exception as exc:
            with self._lifecycle_lock:
                self._state = "FAILED"
            raise WebTraderDependencyError(
                "WebTrader browser could not initialize"
            ) from exc
        with self._lifecycle_lock:
            self._state = "RUNNING"

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if self._state == "CLOSED":
                return
            thread = self._thread
            if thread is None:
                self._state = "CLOSED"
                return
            self._state = "CLOSING"
            self._queue.put(None)
        thread.join(timeout=(self._action_timeout_ms / 1000) + 15)
        with self._lifecycle_lock:
            if thread.is_alive():
                self._state = "FAILED"
                raise WebTraderStateError("WebTrader browser did not shut down cleanly")
            self._state = "CLOSED"

    def __enter__(self) -> PlaywrightWebTraderClicker:
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.shutdown()

    def inspect_identity(
        self, expected_login: str, expected_server: str
    ) -> WebTraderIdentity:
        login = _required_text(expected_login, "expected_login")
        server = _required_text(expected_server, "expected_server")
        return self._call(self._worker_inspect_identity, login, server)

    def prepare_order(self, request: WebTraderOrder) -> PreparedOrderToken:
        values = _order_values(request)
        token = self._call(self._worker_prepare_order, values)
        with self._lifecycle_lock:
            self._known_tokens.add(token.value)
        return token

    def commit_once(
        self,
        token: PreparedOrderToken,
        expected_quote: Decimal,
        point: Decimal,
        max_drift_points: Decimal,
    ) -> WebTraderReceipt:
        if not isinstance(token, PreparedOrderToken):
            raise WebTraderStateError("prepared token type is invalid")
        quote = _decimal(expected_quote, "expected_quote")
        point_value = _decimal(point, "point")
        drift = _decimal(max_drift_points, "max_drift_points", allow_zero=True)
        with self._lifecycle_lock:
            if token.value not in self._known_tokens:
                raise WebTraderStateError("prepared token is unknown")
            if token.value in self._consumed_tokens:
                raise WebTraderAlreadyCommittedError(
                    "prepared order token has already been committed"
                )
            # Consumption happens before the worker call.  Cancellation,
            # timeout, or any later exception can therefore never re-click.
            self._consumed_tokens.add(token.value)
        return self._call(
            self._worker_commit_once,
            token,
            quote,
            point_value,
            drift,
            commit_operation=True,
        )

    def _call(
        self,
        operation: Callable[..., Any],
        *arguments: Any,
        commit_operation: bool = False,
    ) -> Any:
        with self._lifecycle_lock:
            if self._state != "RUNNING":
                raise WebTraderStateError("WebTrader browser is not running")
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._state = "FAILED"
                raise WebTraderStateError("WebTrader browser worker is unavailable")
            future: Future[Any] = Future()
            self._queue.put(_WorkItem(operation, arguments, future))
        timeout = (
            self._action_timeout_ms
            + (self._receipt_timeout_ms if commit_operation else 0)
        ) / 1000 + 10
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            with self._lifecycle_lock:
                self._state = "FAILED"
            if commit_operation:
                raise WebTraderAmbiguousOutcomeError(
                    "WebTrader commit timed out; reconciliation is required"
                ) from exc
            raise WebTraderStateError("WebTrader operation timed out") from exc

    def _worker_main(self) -> None:
        startup = self._startup
        assert startup is not None
        try:
            self._worker_initialize()
        except WebTraderError as exc:
            self._worker_shutdown()
            startup.set_exception(exc)
            return
        except Exception:
            self._worker_shutdown()
            startup.set_exception(
                WebTraderDependencyError("WebTrader browser could not initialize")
            )
            return
        startup.set_result(None)
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                if not item.future.set_running_or_notify_cancel():
                    continue
                try:
                    result = item.operation(*item.arguments)
                except BaseException as exc:  # returned only to the local caller
                    item.future.set_exception(exc)
                else:
                    item.future.set_result(result)
        finally:
            self._worker_shutdown()

    def _worker_initialize(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise WebTraderDependencyError(
                "Playwright is required for WebTrader browser execution"
            ) from exc

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        sentinel = self._profile_dir / _PROFILE_SENTINEL
        entries = list(self._profile_dir.iterdir())
        if entries and not sentinel.is_file():
            raise WebTraderConfigurationError(
                "profile_dir is not an existing TGXM dedicated browser profile"
            )
        if not sentinel.exists():
            sentinel.write_text(_PROFILE_SENTINEL_CONTENT, encoding="utf-8")

        playwright: Any | None = None
        context: Any | None = None
        try:
            playwright = sync_playwright().start()
            launch_options: dict[str, Any] = {
                "user_data_dir": str(self._profile_dir),
                "headless": self._headless,
                "locale": "en-US",
                "accept_downloads": False,
            }
            if self._browser_channel is not None:
                launch_options["channel"] = self._browser_channel
            context = playwright.chromium.launch_persistent_context(**launch_options)
            context.set_default_timeout(self._action_timeout_ms)
            existing_pages = context.pages
            page = (
                existing_pages[0]
                if len(existing_pages) == 1 and existing_pages[0].url == "about:blank"
                else context.new_page()
            )
            if self._page_setup is not None:
                self._page_setup(page)
            page.goto(
                self._url,
                wait_until="domcontentloaded",
                timeout=self._action_timeout_ms,
            )
        except WebTraderError:
            raise
        except Exception as exc:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:
                pass
            raise WebTraderDependencyError(
                "configured Chromium browser could not open WebTrader"
            ) from exc
        self._playwright = playwright
        self._context = context
        self._page = page
        self._assert_allowed_page_and_frame_origin(None)

    def _worker_shutdown(self) -> None:
        context, playwright = self._context, self._playwright
        self._context = None
        self._playwright = None
        self._page = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    def _page_required(self) -> Any:
        if self._page is None:
            raise WebTraderStateError("WebTrader page is unavailable")
        return self._page

    def _assert_allowed_page_and_frame_origin(self, scope: Any | None) -> str:
        page = self._page_required()
        origin = _page_origin(page.url)
        if origin not in self._allowed_origins:
            raise WebTraderIdentityError(
                "WebTrader navigated outside an approved HTTPS origin"
            )
        if scope is not None and scope is not page:
            try:
                frame_url = scope.locator("html").evaluate("el => el.ownerDocument.URL")
            except Exception as exc:
                raise WebTraderUIContractError(
                    "WebTrader frame identity is unavailable"
                ) from exc
            if _page_origin(str(frame_url)) not in self._allowed_origins:
                raise WebTraderIdentityError(
                    "WebTrader frame uses an unapproved HTTPS origin"
                )
        return origin

    def _resolve_scope(self) -> Any:
        page = self._page_required()
        deadline = time.monotonic() + self._action_timeout_ms / 1000
        while True:
            try:
                iframe = page.locator(_IFRAME_SELECTOR)
                iframe_count = iframe.count()
                if iframe_count > 1:
                    raise WebTraderUIContractError(
                        "multiple MT5 WebTrader frames are ambiguous"
                    )
                candidates: list[Any] = [page]
                if iframe_count == 1:
                    candidates.append(page.frame_locator(_IFRAME_SELECTOR))
                matches: list[Any] = []
                for candidate in candidates:
                    count = self._contract_locator(
                        candidate,
                        _LOGIN_SELECTORS,
                        "account login",
                        allow_missing=True,
                    ).count()
                    if count > 1:
                        raise WebTraderUIContractError(
                            "account login control is ambiguous"
                        )
                    if count == 1:
                        matches.append(candidate)
                if len(matches) > 1:
                    raise WebTraderUIContractError(
                        "WebTrader identity appears in multiple frames"
                    )
                if len(matches) == 1:
                    self._assert_allowed_page_and_frame_origin(matches[0])
                    return matches[0]
            except WebTraderError:
                raise
            except Exception:
                # A frame may still be loading.  Keep polling only until the
                # bounded contract timeout, without exposing browser details.
                pass
            if time.monotonic() >= deadline:
                raise WebTraderUIContractError(
                    "WebTrader account identity controls are missing"
                )
            page.wait_for_timeout(50)

    @staticmethod
    def _css_union(selectors: tuple[str, ...]) -> str:
        return ", ".join(selectors)

    def _contract_locator(
        self,
        scope: Any,
        selectors: tuple[str, ...],
        description: str,
        *,
        allow_missing: bool = False,
        button_names: tuple[str, ...] = (),
    ) -> Any:
        locator = scope.locator(self._css_union(selectors))
        for label in _LABEL_FALLBACKS.get(description, ()):
            locator = locator.or_(scope.get_by_label(label, exact=True))
        for name in button_names:
            locator = locator.or_(scope.get_by_role("button", name=name, exact=True))
        if not allow_missing:
            count = locator.count()
            if count == 0:
                raise WebTraderUIContractError(
                    f"WebTrader {description} control is missing"
                )
            if count != 1:
                raise WebTraderUIContractError(
                    f"WebTrader {description} control is ambiguous"
                )
        return locator

    def _unique_locator(
        self,
        scope: Any,
        selectors: tuple[str, ...],
        description: str,
        *,
        button_names: tuple[str, ...] = (),
    ) -> Any:
        locator = self._contract_locator(
            scope,
            selectors,
            description,
            button_names=button_names,
        )
        # Recheck here because a dynamic UI can change between construction
        # and use; strict uniqueness is part of every interaction.
        if locator.count() != 1:
            raise WebTraderUIContractError(
                f"WebTrader {description} control changed or is ambiguous"
            )
        return locator

    @staticmethod
    def _control_value(locator: Any) -> str:
        try:
            value = locator.evaluate(
                """element => {
                    if ('value' in element) return String(element.value ?? '');
                    return String(element.textContent ?? '');
                }"""
            )
        except Exception as exc:
            raise WebTraderUIContractError(
                "WebTrader control value could not be read"
            ) from exc
        return str(value).strip()

    @staticmethod
    def _strip_label(value: str, labels: tuple[str, ...]) -> str:
        result = value.strip()
        for label in labels:
            match = re.fullmatch(
                rf"{re.escape(label)}\s*(?::|#|-)?\s*(.+)",
                result,
                flags=re.IGNORECASE,
            )
            if match:
                return match.group(1).strip()
        return result

    def _read_identity(self, scope: Any) -> WebTraderIdentity:
        login = self._strip_label(
            self._control_value(
                self._unique_locator(scope, _LOGIN_SELECTORS, "account login")
            ),
            ("Account", "Account login", "Account number", "Login"),
        )
        server = self._strip_label(
            self._control_value(
                self._unique_locator(scope, _SERVER_SELECTORS, "account server")
            ),
            ("Server", "Account server"),
        )
        mode = self._strip_label(
            self._control_value(
                self._unique_locator(scope, _MODE_SELECTORS, "account mode")
            ),
            ("Mode", "Account mode", "Account type", "Type"),
        )
        normalized_mode = re.sub(r"\s+", " ", mode).strip().upper()
        is_demo = "DEMO" in normalized_mode and not any(
            marker in normalized_mode for marker in ("LIVE", "REAL")
        )
        if not is_demo:
            raise WebTraderIdentityError(
                "active WebTrader account is not positively identified as Demo"
            )
        return WebTraderIdentity(
            login=login,
            server=server,
            is_demo=True,
            origin=self._assert_allowed_page_and_frame_origin(scope),
        )

    def _worker_inspect_identity(
        self, expected_login: str, expected_server: str
    ) -> WebTraderIdentity:
        scope = self._resolve_scope()
        identity = self._read_identity(scope)
        if identity.login != expected_login or identity.server != expected_server:
            raise WebTraderIdentityError(
                "active WebTrader Demo login/server does not exactly match policy"
            )
        self._verified_identity = identity
        return identity

    def _reverify_identity(self) -> tuple[Any, WebTraderIdentity]:
        expected = self._verified_identity
        if expected is None:
            raise WebTraderIdentityError(
                "WebTrader identity must be inspected before order preparation"
            )
        scope = self._resolve_scope()
        current = self._read_identity(scope)
        if (
            current.login != expected.login
            or current.server != expected.server
            or current.origin != expected.origin
        ):
            raise WebTraderIdentityError(
                "active WebTrader Demo identity changed"
            )
        return scope, current

    def _ensure_order_form(self, scope: Any) -> None:
        symbol = self._contract_locator(
            scope,
            _SYMBOL_SELECTORS,
            "symbol",
            allow_missing=True,
        )
        count = symbol.count()
        if count > 1:
            raise WebTraderUIContractError("WebTrader symbol control is ambiguous")
        if count == 1:
            return
        opener = self._unique_locator(
            scope,
            _NEW_ORDER_SELECTORS,
            "new order",
            button_names=("New order", "New Order"),
        )
        try:
            opener.click(timeout=self._action_timeout_ms)
        except Exception as exc:
            raise WebTraderUIContractError(
                "WebTrader order form could not be opened"
            ) from exc
        try:
            self._unique_locator(scope, _SYMBOL_SELECTORS, "symbol").wait_for(
                state="visible", timeout=self._action_timeout_ms
            )
        except WebTraderError:
            raise
        except Exception as exc:
            raise WebTraderUIContractError(
                "WebTrader order form did not become available"
            ) from exc

    @staticmethod
    def _set_control(locator: Any, value: str) -> None:
        try:
            tag = str(locator.evaluate("element => element.tagName")).upper()
            control_type = str(
                locator.evaluate("element => element.getAttribute('type') || ''")
            ).lower()
            if tag == "SELECT":
                options = locator.locator("option")
                matching_values = [
                    options.nth(index).get_attribute("value")
                    for index in range(options.count())
                    if (
                        (options.nth(index).text_content() or "").strip() == value
                        or (options.nth(index).get_attribute("value") or "") == value
                    )
                ]
                if len(matching_values) != 1:
                    raise WebTraderUIContractError(
                        "WebTrader choice is missing or ambiguous"
                    )
                locator.select_option(value=matching_values[0])
            elif control_type in {"radio", "checkbox"}:
                raise WebTraderUIContractError(
                    "WebTrader control type is not supported by this selector profile"
                )
            else:
                locator.fill(value)
        except WebTraderError:
            raise
        except Exception as exc:
            raise WebTraderUIContractError(
                "WebTrader form control could not be updated"
            ) from exc

    def _side_control(self, scope: Any) -> Any | None:
        locator = self._contract_locator(
            scope, _SIDE_SELECTORS, "side", allow_missing=True
        )
        count = locator.count()
        if count > 1:
            raise WebTraderUIContractError("WebTrader side control is ambiguous")
        return locator if count == 1 else None

    def _side_specific_final_button(self, scope: Any, side: str) -> Any:
        selectors = (
            _BUY_FINAL_BUTTON_SELECTORS if side == "BUY" else _SELL_FINAL_BUTTON_SELECTORS
        )
        name = "Buy by Market" if side == "BUY" else "Sell by Market"
        return self._unique_locator(
            scope,
            selectors,
            f"{side.lower()} final order",
            button_names=(name,),
        )

    def _fill_order_form(self, scope: Any, values: _OrderValues) -> None:
        self._set_control(
            self._unique_locator(scope, _SYMBOL_SELECTORS, "symbol"),
            values.symbol,
        )
        side_control = self._side_control(scope)
        if side_control is not None:
            self._set_control(side_control, values.side)
        else:
            # Some MT5 order dialogs encode direction only in distinct final
            # BUY/SELL buttons.  Prove the requested button now, but never
            # click either direction during preparation.
            self._side_specific_final_button(scope, values.side)
        fields = (
            (_VOLUME_SELECTORS, "volume", _decimal_text(values.volume)),
            (
                _STOP_LOSS_SELECTORS,
                "stop loss",
                _decimal_text(values.stop_loss),
            ),
            (
                _TAKE_PROFIT_SELECTORS,
                "take profit",
                "" if values.take_profit is None else _decimal_text(values.take_profit),
            ),
        )
        for selectors, description, value in fields:
            self._set_control(
                self._unique_locator(scope, selectors, description), value
            )

    @staticmethod
    def _ui_decimal(value: str, field_name: str, *, blank_is_none: bool = False) -> Decimal | None:
        text = value.strip()
        if blank_is_none and text in {"", "-", "0", "0.0", "0.00"}:
            return None
        matches = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", text)
        if len(matches) != 1:
            raise WebTraderUIContractError(
                f"WebTrader {field_name} value is missing or ambiguous"
            )
        try:
            parsed = Decimal(matches[0].replace(",", ""))
        except InvalidOperation as exc:
            raise WebTraderUIContractError(
                f"WebTrader {field_name} value is invalid"
            ) from exc
        if not parsed.is_finite():
            raise WebTraderUIContractError(
                f"WebTrader {field_name} value is invalid"
            )
        return parsed

    def _read_order_form(self, scope: Any, expected_side: str) -> _OrderValues:
        expected_identity = self._verified_identity
        if expected_identity is None:
            raise WebTraderIdentityError("WebTrader identity is not verified")
        symbol = self._control_value(
            self._unique_locator(scope, _SYMBOL_SELECTORS, "symbol")
        ).strip()
        side_control = self._side_control(scope)
        if side_control is None:
            self._side_specific_final_button(scope, expected_side)
            side = expected_side
        else:
            side = self._control_value(side_control).strip().upper()
        volume = self._ui_decimal(
            self._control_value(
                self._unique_locator(scope, _VOLUME_SELECTORS, "volume")
            ),
            "volume",
        )
        stop_loss = self._ui_decimal(
            self._control_value(
                self._unique_locator(scope, _STOP_LOSS_SELECTORS, "stop loss")
            ),
            "stop loss",
        )
        take_profit = self._ui_decimal(
            self._control_value(
                self._unique_locator(scope, _TAKE_PROFIT_SELECTORS, "take profit")
            ),
            "take profit",
            blank_is_none=True,
        )
        assert volume is not None and stop_loss is not None
        return _OrderValues(
            account_id=expected_identity.login,
            symbol=symbol,
            side=side,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    @staticmethod
    def _assert_form_matches(actual: _OrderValues, expected: _OrderValues) -> None:
        if actual != expected:
            raise WebTraderFormDriftError(
                "WebTrader form does not exactly match the prepared order"
            )

    def _worker_prepare_order(self, values: _OrderValues) -> PreparedOrderToken:
        if any(token not in self._consumed_tokens for token in self._prepared):
            raise WebTraderStateError("another WebTrader form is already prepared")
        scope, identity = self._reverify_identity()
        if values.account_id != identity.login:
            raise WebTraderIdentityError(
                "order account does not match the verified WebTrader Demo login"
            )
        self._ensure_order_form(scope)
        self._fill_order_form(scope, values)
        self._assert_form_matches(self._read_order_form(scope, values.side), values)
        token = PreparedOrderToken(secrets.token_urlsafe(24))
        self._prepared[token.value] = _PreparedOrder(token, values, identity)
        return token

    def _current_quote(self, scope: Any, side: str) -> Decimal:
        selectors = _ASK_SELECTORS if side == "BUY" else _BID_SELECTORS
        description = "ask" if side == "BUY" else "bid"
        quote = self._ui_decimal(
            self._control_value(
                self._unique_locator(scope, selectors, description)
            ),
            description,
        )
        assert quote is not None
        if quote <= 0:
            raise WebTraderFormDriftError("WebTrader quote is not positive")
        return quote

    @staticmethod
    def _assert_protection_side(values: _OrderValues, quote: Decimal) -> None:
        if values.side == "BUY":
            valid = values.stop_loss < quote and (
                values.take_profit is None or quote < values.take_profit
            )
        else:
            valid = quote < values.stop_loss and (
                values.take_profit is None or values.take_profit < quote
            )
        if not valid:
            raise WebTraderFormDriftError(
                "WebTrader quote no longer has valid SL/TP relationships"
            )

    def _worker_commit_once(
        self,
        token: PreparedOrderToken,
        expected_quote: Decimal,
        point: Decimal,
        max_drift_points: Decimal,
    ) -> WebTraderReceipt:
        prepared = self._prepared.pop(token.value, None)
        if prepared is None:
            raise WebTraderStateError("prepared order token is unavailable")
        scope, identity = self._reverify_identity()
        if identity != prepared.identity:
            raise WebTraderIdentityError("WebTrader Demo identity changed after prepare")
        self._assert_form_matches(
            self._read_order_form(scope, prepared.values.side), prepared.values
        )
        visible_quote = self._current_quote(scope, prepared.values.side)
        drift_points = abs(visible_quote - expected_quote) / point
        if drift_points > max_drift_points:
            raise WebTraderFormDriftError(
                "WebTrader quote drift exceeds the approved tolerance"
            )
        self._assert_protection_side(prepared.values, visible_quote)
        generic_button = self._contract_locator(
            scope,
            _FINAL_BUTTON_SELECTORS,
            "final order",
            allow_missing=True,
            button_names=("Place order", "Place Order"),
        )
        generic_count = generic_button.count()
        if generic_count > 1:
            raise WebTraderUIContractError(
                "WebTrader final order control is ambiguous"
            )
        if generic_count == 1:
            # A generic submit is allowed only when direction is represented
            # by an independently read-back side control.
            if self._side_control(scope) is None:
                raise WebTraderUIContractError(
                    "generic final order control has no verifiable side control"
                )
            button = generic_button
        else:
            button = self._side_specific_final_button(scope, prepared.values.side)
        try:
            if not button.is_visible() or not button.is_enabled():
                raise WebTraderUIContractError(
                    "WebTrader final order control is not actionable"
                )
        except WebTraderError:
            raise
        except Exception as exc:
            raise WebTraderUIContractError(
                "WebTrader final order control could not be verified"
            ) from exc

        # There is deliberately one and only one final-click call in this
        # module.  Any exception after entering it is treated as ambiguous.
        clicked_at = datetime.now(UTC)
        try:
            button.click(timeout=self._action_timeout_ms)
            return self._wait_for_receipt(scope, clicked_at)
        except WebTraderRejectedError:
            raise
        except WebTraderAmbiguousOutcomeError:
            raise
        except Exception as exc:
            raise WebTraderAmbiguousOutcomeError(
                "WebTrader outcome is ambiguous after final click; do not retry"
            ) from exc

    def _visible_optional(
        self, scope: Any, selectors: tuple[str, ...], description: str
    ) -> Any | None:
        locator = self._contract_locator(
            scope, selectors, description, allow_missing=True
        )
        count = locator.count()
        if count > 1:
            raise WebTraderUIContractError(
                f"WebTrader {description} evidence is ambiguous"
            )
        if count == 1 and locator.is_visible():
            return locator
        return None

    @staticmethod
    def _receipt_id(value: str, label: str) -> str | None:
        stripped = value.strip()
        # A dedicated, structured ID control may contain only the ticket.  In
        # unstructured confirmation text the label is mandatory, otherwise a
        # displayed price or volume could be mistaken for broker identity.
        if re.fullmatch(r"[0-9]+", stripped):
            return stripped
        match = re.search(
            rf"\b{label}(?:\s+(?:ID|ticket))?\s*(?:#|:|=|-)?\s*([0-9]+)\b",
            stripped,
            flags=re.IGNORECASE,
        )
        return None if match is None else match.group(1)

    def _structured_receipt_id(
        self,
        confirmation: Any,
        selectors: tuple[str, ...],
        description: str,
        label: str,
    ) -> str | None:
        locator = self._contract_locator(
            confirmation, selectors, description, allow_missing=True
        )
        count = locator.count()
        if count > 1:
            raise WebTraderUIContractError(
                f"WebTrader {description} evidence is ambiguous"
            )
        if count == 0:
            return None
        return self._receipt_id(self._control_value(locator), label)

    def _wait_for_receipt(
        self, scope: Any, clicked_at: datetime
    ) -> WebTraderReceipt:
        page = self._page_required()
        deadline = time.monotonic() + self._receipt_timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                self._assert_allowed_page_and_frame_origin(scope)
                rejection = self._visible_optional(
                    scope, _REJECTION_SELECTORS, "order rejection"
                )
                if rejection is not None:
                    raise WebTraderRejectedError(
                        "WebTrader visibly rejected the order"
                    )
                confirmation = self._visible_optional(
                    scope, _CONFIRMATION_SELECTORS, "order confirmation"
                )
                if confirmation is not None:
                    order_id = self._structured_receipt_id(
                        confirmation,
                        _ORDER_ID_SELECTORS,
                        "order ID",
                        "order",
                    )
                    deal_id = self._structured_receipt_id(
                        confirmation,
                        _DEAL_ID_SELECTORS,
                        "deal ID",
                        "deal",
                    )
                    position_id = self._structured_receipt_id(
                        confirmation,
                        _POSITION_ID_SELECTORS,
                        "position ID",
                        "position",
                    )
                    if not any((order_id, deal_id, position_id)):
                        text = self._control_value(confirmation)
                        order_id = self._receipt_id(text, "order")
                        deal_id = self._receipt_id(text, "deal")
                        position_id = self._receipt_id(text, "position")
                    if any((order_id, deal_id, position_id)):
                        return WebTraderReceipt(
                            order_id=order_id,
                            deal_id=deal_id,
                            position_id=position_id,
                            clicked_at_utc=clicked_at,
                            origin=self._assert_allowed_page_and_frame_origin(scope),
                        )
            except WebTraderRejectedError:
                raise
            except WebTraderError as exc:
                raise WebTraderAmbiguousOutcomeError(
                    "WebTrader evidence became ambiguous after final click"
                ) from exc
            page.wait_for_timeout(50)
        raise WebTraderAmbiguousOutcomeError(
            "WebTrader returned no exact receipt ticket; reconciliation is required"
        )


# Concise construction name for callers; the Protocol remains separately named
# so wrappers can also use structural typing without importing this module.
WebTraderClicker = PlaywrightWebTraderClicker


__all__ = [
    "PreparedOrderToken",
    "PlaywrightWebTraderClicker",
    "WebTraderAlreadyCommittedError",
    "WebTraderAmbiguousOutcomeError",
    "WebTraderClickExecutor",
    "WebTraderClicker",
    "WebTraderConfigurationError",
    "WebTraderDependencyError",
    "WebTraderError",
    "WebTraderFormDriftError",
    "WebTraderIdentity",
    "WebTraderIdentityError",
    "WebTraderOrder",
    "WebTraderReceipt",
    "WebTraderRejectedError",
    "WebTraderStateError",
    "WebTraderUIContractError",
]
