from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tgxm.models import EntryKind, EntrySemantics, EventType, IssueCode, Side
from tgxm.normalization import normalize_text
from tgxm.parsers import parse_message


@dataclass(frozen=True)
class Profile:
    parser: str
    allowed_symbols: tuple[str, ...]
    two_level_semantics: str = "zone_single_market"
    signal_expiry_minutes: int = 30
    required_markers: tuple[str, ...] = ()
    ignored_markers: tuple[str, ...] = ()
    profile_version: str = "1.0.0"


ALIASES = {"GOLD": "GOLD", "XAUUSD": "GOLD"}
MESSAGE_TIME = datetime(2026, 8, 27, 1, 30, tzinfo=timezone.utc)


def compact_profile(**overrides: object) -> Profile:
    values: dict[str, object] = {
        "parser": "compact_gold_v1",
        "allowed_symbols": ("GOLD",),
    }
    values.update(overrides)
    return Profile(**values)  # type: ignore[arg-type]


def parse(
    text: str,
    profile: Profile,
    *,
    aliases: dict[str, str] | None = None,
    is_edit: bool = False,
    revision: int = 1,
):
    return parse_message(
        text,
        profile=profile,
        profile_name="fixture_profile",
        symbol_aliases=ALIASES if aliases is None else aliases,
        source_channel_id=-100100000001,
        source_message_id=42,
        message_time_utc=MESSAGE_TIME,
        is_edit=is_edit,
        revision=revision,
    )


def issue_codes(result) -> set[IssueCode]:
    return {issue.code for issue in result.issues}


def test_normalization_handles_bold_zero_width_spacing_dash_and_confirmed_typo() -> None:
    text = "  𝗚𝗢𝗟𝗗\u200b   SELL\r\nTP1   HlT — 60 PIPS  "

    assert normalize_text(text) == "GOLD SELL\nTP1 HIT - 60 PIPS"


def test_compact_parser_retains_all_targets_and_maps_alias() -> None:
    result = parse(
        "🔥 XAUUSD SELL 4601 OR 4605 SL 4618 "
        "TP 4595 TP 4590 TP 4585 TP 4580",
        compact_profile(),
    )

    assert result.event_type is EventType.NEW_SIGNAL
    assert result.issues == ()
    assert result.signal is not None
    assert result.signal.symbol_alias == "XAUUSD"
    assert result.signal.canonical_symbol == "GOLD"
    assert result.signal.side is Side.SELL
    assert result.signal.entry.kind is EntryKind.ZONE
    assert result.signal.entry.semantics is EntrySemantics.ZONE_SINGLE_MARKET
    assert result.signal.entry.low == Decimal("4601")
    assert result.signal.entry.high == Decimal("4605")
    assert result.signal.stop_loss == Decimal("4618")
    assert result.signal.take_profits == (
        Decimal("4595"),
        Decimal("4590"),
        Decimal("4585"),
        Decimal("4580"),
    )
    assert result.signal.expires_at_utc == datetime(
        2026, 8, 27, 2, 0, tzinfo=timezone.utc
    )


def test_compact_parser_orders_reversed_zone_boundaries() -> None:
    result = parse(
        "GOLD SELL 4605 / 4601 TP 4595 TP 4590 SL 4618",
        compact_profile(),
    )

    assert result.signal is not None
    assert result.signal.entry.low == Decimal("4601")
    assert result.signal.entry.high == Decimal("4605")


def test_vip_compact_accepts_stop_after_seven_targets() -> None:
    result = parse(
        "XAUUSD SELL 4651 OR 4655 "
        "TP 4646 TP 4643 TP 4640 TP 4635 TP 4630 TP 4624 TP 4620 "
        "SL 4678",
        compact_profile(),
    )

    assert result.signal is not None
    assert len(result.signal.take_profits) == 7
    assert result.signal.take_profits[-1] == Decimal("4620")


def test_single_entry_uses_single_price_semantics() -> None:
    result = parse(
        "GOLD BUY 4500 SL 4490 TP 4510 TP 4520",
        compact_profile(two_level_semantics="manual_review"),
    )

    assert result.signal is not None
    assert result.signal.entry == result.signal.entry.__class__(
        kind=EntryKind.SINGLE,
        low=Decimal("4500"),
        high=Decimal("4500"),
        semantics=EntrySemantics.SINGLE_PRICE,
    )


def test_compact_teaser_is_incomplete_and_has_no_signal() -> None:
    result = parse("GOLD SELL 4631", compact_profile())

    assert result.event_type is EventType.INCOMPLETE_SIGNAL
    assert result.signal is None
    assert issue_codes(result) == {
        IssueCode.MISSING_NUMERIC_STOP_LOSS,
        IssueCode.MISSING_TAKE_PROFIT,
    }


def test_sl_open_is_never_executable() -> None:
    result = parse(
        "XAUUSD SELL 4651 OR 4655 TP 4646 TP 4643 SL OPEN",
        compact_profile(),
    )

    assert result.event_type is EventType.INCOMPLETE_SIGNAL
    assert result.signal is None
    assert IssueCode.MISSING_NUMERIC_STOP_LOSS in issue_codes(result)


@pytest.mark.parametrize(
    "text",
    [
        "GOLD SELL TP1 HIT 60+ PIPS PROFIT DONE",
        "RUNNING 70 PIPS",
        "ALL TARGETS HIT",
        "SL HIT",
    ],
)
def test_result_updates_never_create_signals(text: str) -> None:
    result = parse(text, compact_profile())

    assert result.event_type is EventType.RESULT_UPDATE
    assert result.signal is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Close Now", EventType.CLOSE_REQUEST),
        ("Close Half", EventType.CLOSE_REQUEST),
        ("Move SL to BE", EventType.MODIFY_REQUEST),
        ("Delete Pending", EventType.MODIFY_REQUEST),
        ("Time Frame 4H (forecast)", EventType.FORECAST),
        ("Join our VIP for free signals", EventType.ADVERTISEMENT),
    ],
)
def test_non_executable_message_classes(text: str, expected: EventType) -> None:
    result = parse(text, compact_profile())

    assert result.event_type is expected
    assert result.signal is None


def test_two_prices_without_explicit_semantics_fail_closed() -> None:
    result = parse(
        "GOLD SELL 4601 OR 4605 SL 4618 TP 4595",
        compact_profile(two_level_semantics="manual_review"),
    )

    assert result.event_type is EventType.UNKNOWN
    assert result.signal is None
    assert IssueCode.UNSUPPORTED_ENTRY_SEMANTICS in issue_codes(result)


def test_three_entry_prices_are_ambiguous() -> None:
    result = parse(
        "GOLD SELL 4601 OR 4605 OR 4609 SL 4618 TP 4595",
        compact_profile(),
    )

    assert result.event_type is EventType.UNKNOWN
    assert result.signal is None
    assert IssueCode.TOO_MANY_ENTRY_LEVELS in issue_codes(result)


def test_unknown_symbol_is_not_fuzzy_mapped() -> None:
    result = parse(
        "SILVER SELL 30 SL 31 TP 29",
        compact_profile(),
    )

    assert result.event_type is EventType.UNKNOWN
    assert result.signal is None
    assert IssueCode.SYMBOL_NOT_ALLOWED in issue_codes(result)


def test_ambiguous_comma_number_is_rejected_instead_of_partially_parsed() -> None:
    result = parse(
        "GOLD SELL 4,601 SL 4,618 TP 4,595",
        compact_profile(),
    )

    assert result.event_type is EventType.UNKNOWN
    assert IssueCode.INVALID_DECIMAL in issue_codes(result)


def test_invalid_buy_price_relationship_has_no_canonical_signal() -> None:
    result = parse(
        "GOLD BUY 4500 SL 4510 TP 4520",
        compact_profile(),
    )

    assert result.event_type is EventType.UNKNOWN
    assert result.signal is None
    assert IssueCode.INVALID_PRICE_RELATIONSHIP in issue_codes(result)


def test_take_profit_order_is_validated() -> None:
    result = parse(
        "GOLD SELL 4600 SL 4610 TP 4580 TP 4590",
        compact_profile(),
    )

    assert result.event_type is EventType.UNKNOWN
    assert result.signal is None
    assert IssueCode.UNORDERED_TAKE_PROFITS in issue_codes(result)


def test_signal_edit_keeps_root_identity_and_revision() -> None:
    result = parse(
        "GOLD BUY 4500 SL 4490 TP 4510",
        compact_profile(),
        is_edit=True,
        revision=2,
    )

    assert result.event_type is EventType.SIGNAL_EDIT
    assert result.signal is not None
    assert result.signal.event_type is EventType.SIGNAL_EDIT
    assert result.signal.signal_id == "-100100000001:42"
    assert result.signal.revision == 2


def test_profile_required_marker_is_enforced() -> None:
    result = parse(
        "GOLD BUY 4500 SL 4490 TP 4510",
        compact_profile(required_markers=("AUTHORIZED",)),
    )

    assert result.event_type is EventType.UNKNOWN
    assert IssueCode.REQUIRED_MARKER_MISSING in issue_codes(result)


def test_united_suggested_trade_parser_v1() -> None:
    profile = Profile(
        parser="suggested_trade_v1",
        allowed_symbols=("EURNZD",),
        two_level_semantics="manual_review",
        required_markers=("SUGGESTED TRADE",),
    )
    result = parse(
        "SUGGESTED TRADE:\n"
        "Swing Trade\n"
        "Sell EURNZD\n"
        "Entry - 1.9622\n"
        "Stop - 1.9644\n"
        "Take - 1.9586\n"
        "Our Risk - 1%",
        profile,
        aliases={},
    )

    assert result.event_type is EventType.NEW_SIGNAL
    assert result.signal is not None
    assert result.signal.canonical_symbol == "EURNZD"
    assert result.signal.entry.low == Decimal("1.9622")
    assert result.signal.stop_loss == Decimal("1.9644")
    assert result.signal.take_profits == (Decimal("1.9586"),)


def test_united_analysis_without_marker_is_forecast() -> None:
    profile = Profile(
        parser="suggested_trade_v1",
        allowed_symbols=("EURGBP",),
        required_markers=("SUGGESTED TRADE",),
    )
    result = parse(
        "EURGBP remains bearish below resistance.",
        profile,
        aliases={},
    )

    assert result.event_type is EventType.FORECAST
    assert result.signal is None


def test_united_trade_like_text_without_marker_is_unknown() -> None:
    profile = Profile(parser="suggested_trade_v1", allowed_symbols=("EURNZD",))
    result = parse(
        "Sell EURNZD Entry 1.9622 Stop 1.9644 Take 1.9586",
        profile,
        aliases={},
    )

    assert result.event_type is EventType.UNKNOWN
    assert IssueCode.REQUIRED_MARKER_MISSING in issue_codes(result)


def test_united_marker_with_missing_stop_is_incomplete() -> None:
    profile = Profile(parser="suggested_trade_v1", allowed_symbols=("EURNZD",))
    result = parse(
        "SUGGESTED TRADE Sell EURNZD Entry 1.9622 Take 1.9586",
        profile,
        aliases={},
    )

    assert result.event_type is EventType.INCOMPLETE_SIGNAL
    assert IssueCode.MISSING_NUMERIC_STOP_LOSS in issue_codes(result)


def test_anabel_narrative_signal_parser_v1() -> None:
    profile = Profile(
        parser="narrative_signal_v1",
        allowed_symbols=("GOLD",),
        two_level_semantics="manual_review",
        required_markers=("(signal)",),
        ignored_markers=("(forecast)",),
    )
    result = parse(
        "GOLD Expected Growth! BUY!\n"
        "The asset is approaching pivot point 4598.7\n"
        "Bias - Bullish\n"
        "Safe Stop Loss - 4584.6\n"
        "Goal - 4621.1\n"
        "Time Frame: 30m (signal)",
        profile,
    )

    assert result.event_type is EventType.NEW_SIGNAL
    assert result.signal is not None
    assert result.signal.side is Side.BUY
    assert result.signal.entry.low == Decimal("4598.7")
    assert result.signal.stop_loss == Decimal("4584.6")
    assert result.signal.take_profits == (Decimal("4621.1"),)


def test_anabel_forecast_is_never_parsed_as_signal() -> None:
    profile = Profile(
        parser="narrative_signal_v1",
        allowed_symbols=("GOLD",),
        ignored_markers=("(forecast)",),
    )
    result = parse(
        "GOLD BUY Bias - Bullish Goal 4621 Stop Loss 4584 pivot 4598 (forecast)",
        profile,
    )

    assert result.event_type is EventType.FORECAST
    assert result.signal is None


def test_anabel_conflicting_signal_and_forecast_markers_are_unknown() -> None:
    profile = Profile(
        parser="narrative_signal_v1",
        allowed_symbols=("GOLD",),
        ignored_markers=("(forecast)",),
    )
    result = parse(
        "GOLD BUY pivot 4500 Bias - Bullish Stop Loss 4490 Goal 4510 "
        "(signal) (forecast)",
        profile,
    )

    assert result.event_type is EventType.UNKNOWN
    assert result.signal is None
    assert IssueCode.CONFLICTING_MARKERS in issue_codes(result)


def test_anabel_direction_bias_conflict_is_quarantined() -> None:
    profile = Profile(parser="narrative_signal_v1", allowed_symbols=("GOLD",))
    result = parse(
        "GOLD SELL pivot point 4598 Bias - Bullish Stop Loss 4610 Goal 4580 (signal)",
        profile,
    )

    assert result.event_type is EventType.UNKNOWN
    assert result.signal is None
    assert IssueCode.CONFLICTING_DIRECTION_BIAS in issue_codes(result)


def test_anabel_without_signal_marker_is_unknown() -> None:
    profile = Profile(parser="narrative_signal_v1", allowed_symbols=("GOLD",))
    result = parse(
        "GOLD BUY pivot point 4598 Bias - Bullish Stop Loss 4584 Goal 4621",
        profile,
    )

    assert result.event_type is EventType.UNKNOWN
    assert IssueCode.REQUIRED_MARKER_MISSING in issue_codes(result)


@pytest.mark.parametrize(
    "parser_name",
    ["compact_gold_v1", "suggested_trade_v1", "narrative_signal_v1"],
)
def test_config_parser_v1_names_are_recognized(parser_name: str) -> None:
    profile = Profile(parser=parser_name, allowed_symbols=("GOLD",))
    if parser_name == "compact_gold_v1":
        text = "GOLD BUY 4500 SL 4490 TP 4510"
    elif parser_name == "suggested_trade_v1":
        text = "SUGGESTED TRADE Buy GOLD Entry 4500 Stop 4490 Take 4510"
    else:
        text = (
            "GOLD BUY pivot point 4500 Bias - Bullish Stop Loss 4490 "
            "Goal 4510 (signal)"
        )

    result = parse(text, profile)

    assert IssueCode.UNKNOWN_PARSER not in issue_codes(result)
    assert result.signal is not None
