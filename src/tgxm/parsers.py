"""Deterministic, channel-profiled Telegram signal parsers.

No parser in this module guesses missing values.  Recognized commentary is
classified for audit, while only a complete and statically valid parse returns
a :class:`~tgxm.models.CanonicalSignal`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from tgxm.models import (
    CanonicalSignal,
    EntryKind,
    EntrySemantics,
    EntrySpec,
    EventType,
    Issue,
    IssueCode,
    ParseResult,
    RawTelegramEvent,
    Side,
)
from tgxm.normalization import normalize_text
from tgxm.validation import validate_canonical_signal


_NUMBER = r"(?<![A-Z0-9_.])\d+(?:\.\d+)?(?![A-Z0-9_.])"
_NUMBER_CAPTURE = rf"({_NUMBER})"
_AMBIGUOUS_COMMA_NUMBER = re.compile(r"\d\s*,\s*\d")

_RESULT_PATTERNS = (
    re.compile(r"\bTP\s*#?\d+\s+(?:HIT|DONE)\b"),
    re.compile(r"\bRUNNING(?:\s+\d+(?:\.\d+)?)?(?:\s+PIPS?)?\b"),
    re.compile(r"\bPROFIT\s+(?:IS\s+)?DONE\b"),
    re.compile(r"\b(?:ALL\s+)?TARGETS?\s+(?:HIT|DONE)\b"),
    re.compile(r"\bSL\s+HIT\b"),
)
_CLOSE_PATTERNS = (
    re.compile(r"\bCLOSE\s+(?:NOW|HALF|ALL|FULL|TRADE|POSITION)\b"),
    re.compile(r"\bBOOK\s+(?:THE\s+)?PROFIT\b"),
)
_MODIFY_PATTERNS = (
    re.compile(
        r"\bMOVE\s+(?:SL|STOP(?:\s+LOSS)?)\s+(?:TO\s+)?"
        r"(?:BE|BREAK[ -]?EVEN|\d+(?:\.\d+)?)\b"
    ),
    re.compile(r"\b(?:SET|CHANGE|UPDATE)\s+(?:SL|STOP(?:\s+LOSS)?)\b"),
    re.compile(r"\b(?:DELETE\s+PENDING|CANCEL\s+SIGNAL)\b"),
)
_ADVERTISEMENT_PATTERNS = (
    re.compile(r"\bJOIN\s+(?:OUR\s+)?VIP\b"),
    re.compile(r"\bSUBSCRIBE\b"),
    re.compile(r"\bADVERTISEMENT\b"),
    re.compile(r"\bPROMO(?:TION)?\b"),
    re.compile(r"\bFREE\s+SIGNALS?\b"),
    re.compile(r"\bVIEW\s+POST\b"),
)

_SL_OPEN = re.compile(
    r"\b(?:SL|STOP\s+LOSS|STOP)\b\s*(?:[:=@-]\s*)?OPEN\b"
)
_STOP_PRICE = re.compile(
    rf"\b(?:SL|STOP\s+LOSS|STOP)\b\s*(?:[:=@-]\s*)?{_NUMBER_CAPTURE}"
)
_TAKE_PRICE = re.compile(
    rf"(?:"
    rf"\bTP(?:\s*#?\d+)?\b|"
    rf"\bTAKE\s+PROFIT(?:\s*#?\d+)?\b|"
    rf"\bTARGET(?:\s*#?\d+)?\b|"
    rf"\bGOAL(?:\s*#?\d+)?\b|"
    rf"\bTAKE\b"
    rf")\s*(?:[:=@-]\s*)?{_NUMBER_CAPTURE}"
)
_EXPLICIT_ENTRY = re.compile(
    rf"\bENTRY(?:\s+(?:LEVEL|ZONE))?\b\s*(?:[:=@-]\s*)?"
    rf"{_NUMBER_CAPTURE}(?:\s*(OR|/|TO|-)\s*{_NUMBER_CAPTURE})?"
)
_INLINE_ENTRY = re.compile(
    rf"^\s*(?:@|:|-)?\s*{_NUMBER_CAPTURE}"
    rf"(?:\s*(OR|/|TO|-)\s*{_NUMBER_CAPTURE})?"
)
_EXTRA_ENTRY_LEVEL = re.compile(rf"^\s*(?:OR|/|TO|-)\s*{_NUMBER_CAPTURE}")

_PARSER_ALIASES = {
    "compact_gold": "compact_gold",
    "compact_gold_v1": "compact_gold",
    "compact": "compact_gold",
    "mr_charlie": "compact_gold",
    "mr_charlie_fx": "compact_gold",
    "vip_gold": "compact_gold",
    "vip_gold_signals": "compact_gold",
    "suggested_trade": "suggested_trade",
    "suggested_trade_v1": "suggested_trade",
    "united": "suggested_trade",
    "united_signals": "suggested_trade",
    "narrative_signal": "narrative_signal",
    "narrative_signal_v1": "narrative_signal",
    "narrative": "narrative_signal",
    "anabel": "narrative_signal",
    "anabel_signals": "narrative_signal",
}
_PARSER_VERSIONS = {
    "compact_gold": "1.0.0",
    "suggested_trade": "1.0.0",
    "narrative_signal": "1.0.0",
}


def _issue(code: IssueCode, message: str, field: str | None = None) -> Issue:
    return Issue(code=code, message=message, field=field)


def _profile_value(profile: object, name: str, default: Any = None) -> Any:
    if isinstance(profile, Mapping):
        return profile.get(name, default)
    return getattr(profile, name, default)


def _as_string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    return ()


def _parser_family(profile: object) -> str | None:
    raw = _profile_value(profile, "parser", "")
    key = str(getattr(raw, "value", raw)).strip().lower().replace("-", "_").replace(" ", "_")
    return _PARSER_ALIASES.get(key)


def _normalized_aliases(symbol_aliases: Mapping[str, str] | None) -> dict[str, str]:
    if not symbol_aliases:
        return {}
    return {
        str(alias).strip().upper(): str(canonical).strip().upper()
        for alias, canonical in symbol_aliases.items()
        if str(alias).strip() and str(canonical).strip()
    }


def _allowed_symbols(profile: object) -> set[str]:
    raw = _profile_value(profile, "allowed_symbols", ())
    return {item.strip().upper() for item in _as_string_tuple(raw) if item.strip()}


def _resolve_symbol(
    alias: str,
    *,
    profile: object,
    symbol_aliases: Mapping[str, str] | None,
) -> str | None:
    alias = alias.upper()
    aliases = _normalized_aliases(symbol_aliases)
    canonical = aliases.get(alias, alias)
    allowed = _allowed_symbols(profile)
    if alias in allowed or canonical in allowed:
        return canonical
    return None


def _symbol_candidates(
    profile: object, symbol_aliases: Mapping[str, str] | None
) -> set[str]:
    allowed = _allowed_symbols(profile)
    aliases = _normalized_aliases(symbol_aliases)
    candidates = set(allowed)
    candidates.update(
        alias
        for alias, canonical in aliases.items()
        if alias in allowed or canonical in allowed
    )
    return candidates


def _decimal(value: str) -> Decimal | None:
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _parse_number_matches(matches: Sequence[re.Match[str]], group: int = 1) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for match in matches:
        value = _decimal(match.group(group))
        if value is not None:
            values.append(value)
    return tuple(values)


def _non_signal_classification(text_upper: str, profile: object) -> tuple[EventType, tuple[Issue, ...]] | None:
    ignored = tuple(marker.upper() for marker in _as_string_tuple(_profile_value(profile, "ignored_markers", ())))
    matched_ignored = next((marker for marker in ignored if marker and marker in text_upper), None)
    if matched_ignored is not None:
        if "FORECAST" in matched_ignored or "FORECAST" in text_upper:
            return EventType.FORECAST, ()
        return (
            EventType.UNKNOWN,
            (
                _issue(
                    IssueCode.IGNORED_MARKER,
                    "The message contains a profile marker that is explicitly non-actionable.",
                ),
            ),
        )

    if "(FORECAST)" in text_upper:
        return EventType.FORECAST, ()
    if any(pattern.search(text_upper) for pattern in _CLOSE_PATTERNS):
        return EventType.CLOSE_REQUEST, ()
    if any(pattern.search(text_upper) for pattern in _MODIFY_PATTERNS):
        return EventType.MODIFY_REQUEST, ()
    if any(pattern.search(text_upper) for pattern in _RESULT_PATTERNS):
        return EventType.RESULT_UPDATE, ()
    if any(pattern.search(text_upper) for pattern in _ADVERTISEMENT_PATTERNS):
        return EventType.ADVERTISEMENT, ()
    return None


def _required_markers_present(text_upper: str, profile: object) -> bool:
    required = tuple(marker.upper() for marker in _as_string_tuple(_profile_value(profile, "required_markers", ())))
    return all(marker in text_upper for marker in required if marker)


def _extract_unique_side(text_upper: str) -> tuple[Side | None, tuple[Issue, ...]]:
    sides = {match.group(1) for match in re.finditer(r"\b(BUY|SELL)\b", text_upper)}
    if len(sides) > 1:
        return None, (
            _issue(IssueCode.AMBIGUOUS_SIDE, "Both BUY and SELL appear in the candidate message.", "side"),
        )
    if not sides:
        return None, (_issue(IssueCode.MISSING_SIDE, "A BUY or SELL side is required.", "side"),)
    return Side(next(iter(sides))), ()


def _extract_compact_header(
    text_upper: str,
    *,
    profile: object,
    symbol_aliases: Mapping[str, str] | None,
) -> tuple[str | None, str | None, Side | None, int | None, tuple[Issue, ...]]:
    matches = list(re.finditer(r"\b([A-Z][A-Z0-9._-]{1,31})\s+(BUY|SELL)\b", text_upper))
    if not matches:
        return None, None, None, None, (
            _issue(IssueCode.MISSING_SYMBOL, "Expected '<symbol> BUY|SELL' compact grammar.", "symbol"),
        )

    resolved: list[tuple[str, str, Side, int]] = []
    saw_disallowed = False
    for match in matches:
        alias = match.group(1)
        canonical = _resolve_symbol(alias, profile=profile, symbol_aliases=symbol_aliases)
        if canonical is None:
            saw_disallowed = True
            continue
        resolved.append((alias, canonical, Side(match.group(2)), match.end()))

    unique = {(alias, canonical, side) for alias, canonical, side, _ in resolved}
    if len(unique) > 1:
        return None, None, None, None, (
            _issue(IssueCode.AMBIGUOUS_SYMBOL, "Multiple symbol/side headers were found.", "symbol"),
        )
    if not resolved:
        code = IssueCode.SYMBOL_NOT_ALLOWED if saw_disallowed else IssueCode.MISSING_SYMBOL
        return None, None, None, None, (
            _issue(code, "The compact symbol is not allowlisted by this profile.", "symbol"),
        )
    alias, canonical, side, header_end = resolved[0]
    return alias, canonical, side, header_end, ()


def _extract_narrative_symbol(
    text_upper: str,
    *,
    profile: object,
    symbol_aliases: Mapping[str, str] | None,
) -> tuple[str | None, str | None, tuple[Issue, ...]]:
    candidates = _symbol_candidates(profile, symbol_aliases)
    found = {
        candidate
        for candidate in candidates
        if re.search(rf"(?<![A-Z0-9._-]){re.escape(candidate)}(?![A-Z0-9._-])", text_upper)
    }
    if len(found) > 1:
        return None, None, (
            _issue(IssueCode.AMBIGUOUS_SYMBOL, "Multiple allowlisted symbols appear in the message.", "symbol"),
        )
    if not found:
        return None, None, (
            _issue(IssueCode.MISSING_SYMBOL, "No exact allowlisted symbol was found.", "symbol"),
        )
    alias = next(iter(found))
    canonical = _resolve_symbol(alias, profile=profile, symbol_aliases=symbol_aliases)
    if canonical is None:
        return None, None, (
            _issue(IssueCode.SYMBOL_NOT_ALLOWED, "The symbol is not allowlisted.", "symbol"),
        )
    return alias, canonical, ()


def _extract_entry(
    text_upper: str, *, inline_after: int | None = None
) -> tuple[tuple[Decimal, ...], tuple[Issue, ...]]:
    explicit_matches = list(_EXPLICIT_ENTRY.finditer(text_upper))
    if len(explicit_matches) > 1:
        return (), (
            _issue(IssueCode.TOO_MANY_ENTRY_LEVELS, "Multiple Entry clauses are ambiguous.", "entry"),
        )

    match: re.Match[str] | None
    if explicit_matches:
        match = explicit_matches[0]
    elif inline_after is not None:
        match = _INLINE_ENTRY.search(text_upper[inline_after:])
        if match is None:
            return (), (_issue(IssueCode.MISSING_ENTRY, "A numeric entry is required.", "entry"),)
    else:
        return (), (_issue(IssueCode.MISSING_ENTRY, "A numeric Entry clause is required.", "entry"),)

    first = _decimal(match.group(1))
    second = _decimal(match.group(3)) if match.group(3) else None
    if first is None or (match.group(3) and second is None):
        return (), (_issue(IssueCode.INVALID_DECIMAL, "Entry price is not a finite Decimal.", "entry"),)

    searched_text = text_upper if explicit_matches else text_upper[inline_after:]
    if _EXTRA_ENTRY_LEVEL.match(searched_text[match.end():]):
        return (), (
            _issue(IssueCode.TOO_MANY_ENTRY_LEVELS, "Only one or two entry levels are supported.", "entry"),
        )
    return ((first,) if second is None else (first, second)), ()


def _entry_spec(levels: tuple[Decimal, ...], profile: object) -> tuple[EntrySpec | None, tuple[Issue, ...]]:
    if len(levels) == 1:
        return (
            EntrySpec(
                kind=EntryKind.SINGLE,
                low=levels[0],
                high=levels[0],
                semantics=EntrySemantics.SINGLE_PRICE,
            ),
            (),
        )
    if len(levels) != 2:
        return None, (_issue(IssueCode.MISSING_ENTRY, "One or two entry prices are required.", "entry"),)

    raw_semantics = _profile_value(profile, "two_level_semantics", None)
    raw_semantics = getattr(raw_semantics, "value", raw_semantics)
    semantics_key = str(raw_semantics or "").strip().lower().replace("-", "_")
    if semantics_key in {"zone", "zone_single_market", "entry_zone", "one_zone_one_order"}:
        semantics = EntrySemantics.ZONE_SINGLE_MARKET
    elif not semantics_key or semantics_key in {"none", "unconfirmed", "ambiguous"}:
        return None, (
            _issue(
                IssueCode.AMBIGUOUS_ENTRY_SEMANTICS,
                "Two entry levels require explicit profile semantics.",
                "entry",
            ),
        )
    else:
        return None, (
            _issue(
                IssueCode.UNSUPPORTED_ENTRY_SEMANTICS,
                "The configured two-level semantics are not supported by this release.",
                "entry",
            ),
        )

    return (
        EntrySpec(
            kind=EntryKind.ZONE,
            low=min(levels),
            high=max(levels),
            semantics=semantics,
        ),
        (),
    )


def _extract_stop_and_targets(text_upper: str) -> tuple[Decimal | None, tuple[Decimal, ...], tuple[Issue, ...]]:
    issues: list[Issue] = []
    if _SL_OPEN.search(text_upper):
        issues.append(
            _issue(
                IssueCode.MISSING_NUMERIC_STOP_LOSS,
                "SL OPEN is never executable; a numeric Stop Loss is required.",
                "stop_loss",
            )
        )
        stop_loss = None
    else:
        stop_matches = list(_STOP_PRICE.finditer(text_upper))
        if len(stop_matches) != 1:
            issues.append(
                _issue(
                    IssueCode.MISSING_NUMERIC_STOP_LOSS,
                    "Exactly one numeric Stop Loss is required.",
                    "stop_loss",
                )
            )
            stop_loss = None
        else:
            stop_loss = _decimal(stop_matches[0].group(1))
            if stop_loss is None:
                issues.append(
                    _issue(IssueCode.INVALID_DECIMAL, "Stop Loss is not a finite Decimal.", "stop_loss")
                )

    take_matches = list(_TAKE_PRICE.finditer(text_upper))
    take_profits = _parse_number_matches(take_matches)
    if not take_profits:
        issues.append(
            _issue(
                IssueCode.MISSING_TAKE_PROFIT,
                "At least one numeric Take Profit is required.",
                "take_profits",
            )
        )
    return stop_loss, take_profits, tuple(issues)


def _canonical_result(
    *,
    event: RawTelegramEvent,
    normalized: str,
    profile: object,
    profile_name: str | None,
    parser_family: str,
    symbol_alias: str,
    canonical_symbol: str,
    side: Side,
    entry: EntrySpec,
    stop_loss: Decimal,
    take_profits: tuple[Decimal, ...],
) -> ParseResult:
    event_type = EventType.SIGNAL_EDIT if event.is_edit else EventType.NEW_SIGNAL

    message_time = event.message_time_utc
    if isinstance(message_time, datetime) and message_time.tzinfo is not None:
        message_time = message_time.astimezone(timezone.utc)

    expires_at: datetime | None = None
    expiry_value = _profile_value(profile, "signal_expiry_minutes", None)
    if (
        isinstance(message_time, datetime)
        and isinstance(expiry_value, int)
        and not isinstance(expiry_value, bool)
        and expiry_value > 0
    ):
        expires_at = message_time + timedelta(minutes=expiry_value)

    resolved_profile_name = (
        profile_name
        or str(_profile_value(profile, "profile_name", "")).strip()
        or str(_profile_value(profile, "name", "")).strip()
        or str(_profile_value(profile, "parser", parser_family)).strip()
    )
    profile_version = str(_profile_value(profile, "profile_version", "1")).strip()
    raw_hash = hashlib.sha256(event.text.encode("utf-8")).hexdigest()

    signal = CanonicalSignal(
        signal_id=f"{event.channel_id}:{event.message_id}",
        revision=event.revision,
        source_channel_id=event.channel_id,
        source_message_id=event.message_id,
        reply_to_message_id=event.reply_to_message_id,
        forward_origin=event.forward_origin,
        event_type=event_type,
        symbol_alias=symbol_alias,
        canonical_symbol=canonical_symbol,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=take_profits,
        message_time_utc=message_time,
        expires_at_utc=expires_at,
        parser_profile=resolved_profile_name,
        parser_version=_PARSER_VERSIONS[parser_family],
        profile_version=profile_version,
        raw_message_hash=raw_hash,
    )
    validation = validate_canonical_signal(signal)
    if not validation.valid:
        return ParseResult(
            event_type=EventType.UNKNOWN,
            signal=None,
            issues=validation.issues,
            normalized_text=normalized,
        )
    return ParseResult(event_type=event_type, signal=signal, issues=(), normalized_text=normalized)


def _incomplete_or_unknown(
    event_type: EventType, normalized: str, issues: Sequence[Issue]
) -> ParseResult:
    return ParseResult(
        event_type=event_type,
        signal=None,
        issues=tuple(issues),
        normalized_text=normalized,
    )


def _parse_compact(
    event: RawTelegramEvent,
    normalized: str,
    profile: object,
    profile_name: str | None,
    symbol_aliases: Mapping[str, str] | None,
) -> ParseResult:
    text_upper = normalized.upper()
    alias, canonical, side, header_end, header_issues = _extract_compact_header(
        text_upper, profile=profile, symbol_aliases=symbol_aliases
    )
    if header_issues:
        return _incomplete_or_unknown(EventType.UNKNOWN, normalized, header_issues)

    levels, entry_issues = _extract_entry(text_upper, inline_after=header_end)
    entry, semantics_issues = _entry_spec(levels, profile) if levels else (None, ())
    stop_loss, take_profits, protective_issues = _extract_stop_and_targets(text_upper)
    issues = (*entry_issues, *semantics_issues, *protective_issues)
    if issues:
        ambiguous = any(
            issue.code
            in {
                IssueCode.AMBIGUOUS_ENTRY_SEMANTICS,
                IssueCode.UNSUPPORTED_ENTRY_SEMANTICS,
                IssueCode.TOO_MANY_ENTRY_LEVELS,
            }
            for issue in issues
        )
        event_type = EventType.UNKNOWN if ambiguous else EventType.INCOMPLETE_SIGNAL
        return _incomplete_or_unknown(event_type, normalized, issues)

    assert alias is not None and canonical is not None and side is not None
    assert entry is not None and stop_loss is not None
    return _canonical_result(
        event=event,
        normalized=normalized,
        profile=profile,
        profile_name=profile_name,
        parser_family="compact_gold",
        symbol_alias=alias,
        canonical_symbol=canonical,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=take_profits,
    )


def _parse_suggested_trade(
    event: RawTelegramEvent,
    normalized: str,
    profile: object,
    profile_name: str | None,
    symbol_aliases: Mapping[str, str] | None,
) -> ParseResult:
    text_upper = normalized.upper()
    if "SUGGESTED TRADE" not in text_upper:
        if re.search(r"\b(?:BULLISH|BEARISH)\b", text_upper):
            return _incomplete_or_unknown(EventType.FORECAST, normalized, ())
        return _incomplete_or_unknown(
            EventType.UNKNOWN,
            normalized,
            (
                _issue(
                    IssueCode.REQUIRED_MARKER_MISSING,
                    "UnitedSignals grammar requires the SUGGESTED TRADE marker.",
                ),
            ),
        )

    header_matches = list(re.finditer(r"\b(BUY|SELL)\s+([A-Z][A-Z0-9._-]{1,31})\b", text_upper))
    resolved: list[tuple[Side, str, str]] = []
    for match in header_matches:
        alias = match.group(2)
        canonical = _resolve_symbol(alias, profile=profile, symbol_aliases=symbol_aliases)
        if canonical is not None:
            resolved.append((Side(match.group(1)), alias, canonical))

    issues: list[Issue] = []
    if len(set(resolved)) != 1:
        if resolved:
            issues.append(
                _issue(IssueCode.AMBIGUOUS_SYMBOL, "Suggested trade has multiple side/symbol headers.", "symbol")
            )
        elif header_matches:
            issues.append(
                _issue(IssueCode.SYMBOL_NOT_ALLOWED, "Suggested-trade symbol is not allowlisted.", "symbol")
            )
        else:
            issues.append(
                _issue(IssueCode.MISSING_SYMBOL, "Expected 'Buy|Sell <symbol>' after the marker.", "symbol")
            )

    levels, entry_issues = _extract_entry(text_upper)
    entry, semantics_issues = _entry_spec(levels, profile) if levels else (None, ())
    stop_loss, take_profits, protective_issues = _extract_stop_and_targets(text_upper)
    issues.extend((*entry_issues, *semantics_issues, *protective_issues))
    if issues:
        ambiguous = any(
            issue.code
            in {
                IssueCode.AMBIGUOUS_SYMBOL,
                IssueCode.AMBIGUOUS_ENTRY_SEMANTICS,
                IssueCode.UNSUPPORTED_ENTRY_SEMANTICS,
                IssueCode.TOO_MANY_ENTRY_LEVELS,
            }
            for issue in issues
        )
        return _incomplete_or_unknown(
            EventType.UNKNOWN if ambiguous else EventType.INCOMPLETE_SIGNAL,
            normalized,
            issues,
        )

    side, alias, canonical = resolved[0]
    assert entry is not None and stop_loss is not None
    return _canonical_result(
        event=event,
        normalized=normalized,
        profile=profile,
        profile_name=profile_name,
        parser_family="suggested_trade",
        symbol_alias=alias,
        canonical_symbol=canonical,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=take_profits,
    )


def _parse_narrative_signal(
    event: RawTelegramEvent,
    normalized: str,
    profile: object,
    profile_name: str | None,
    symbol_aliases: Mapping[str, str] | None,
) -> ParseResult:
    text_upper = normalized.upper()
    has_signal_marker = "(SIGNAL)" in text_upper
    has_forecast_marker = "(FORECAST)" in text_upper
    if has_signal_marker and has_forecast_marker:
        return _incomplete_or_unknown(
            EventType.UNKNOWN,
            normalized,
            (_issue(IssueCode.CONFLICTING_MARKERS, "Both (signal) and (forecast) markers are present."),),
        )
    if has_forecast_marker:
        return _incomplete_or_unknown(EventType.FORECAST, normalized, ())
    if not has_signal_marker:
        return _incomplete_or_unknown(
            EventType.UNKNOWN,
            normalized,
            (
                _issue(
                    IssueCode.REQUIRED_MARKER_MISSING,
                    "Anabel narrative grammar requires the (signal) marker.",
                ),
            ),
        )

    alias, canonical, symbol_issues = _extract_narrative_symbol(
        text_upper, profile=profile, symbol_aliases=symbol_aliases
    )
    side, side_issues = _extract_unique_side(text_upper)

    issues: list[Issue] = [*symbol_issues, *side_issues]
    bias_matches = {match.group(1) for match in re.finditer(r"\bBIAS\s*[:=-]\s*(BULLISH|BEARISH)\b", text_upper)}
    if len(bias_matches) != 1:
        issues.append(
            _issue(
                IssueCode.CONFLICTING_DIRECTION_BIAS,
                "Exactly one explicit Bullish or Bearish Bias is required.",
                "side",
            )
        )
    elif side is not None:
        bias = next(iter(bias_matches))
        if (side is Side.BUY and bias != "BULLISH") or (side is Side.SELL and bias != "BEARISH"):
            issues.append(
                _issue(
                    IssueCode.CONFLICTING_DIRECTION_BIAS,
                    "Explicit direction conflicts with narrative Bias.",
                    "side",
                )
            )

    narrative_entry = re.compile(
        rf"\b(?:PIVOT(?:\s+POINT)?|KEY\s+LEVEL)\b\s*(?:[:=@-]\s*)?{_NUMBER_CAPTURE}"
    )
    entry_matches = list(narrative_entry.finditer(text_upper))
    if len(entry_matches) == 1:
        entry_value = _decimal(entry_matches[0].group(1))
        levels = (entry_value,) if entry_value is not None else ()
        entry_issues: tuple[Issue, ...] = () if levels else (
            _issue(IssueCode.INVALID_DECIMAL, "Narrative entry is not a finite Decimal.", "entry"),
        )
    else:
        levels = ()
        entry_issues = (
            _issue(
                IssueCode.MISSING_ENTRY if not entry_matches else IssueCode.TOO_MANY_ENTRY_LEVELS,
                "Exactly one approved pivot/key-level entry is required.",
                "entry",
            ),
        )
    entry, semantics_issues = _entry_spec(levels, profile) if levels else (None, ())
    stop_loss, take_profits, protective_issues = _extract_stop_and_targets(text_upper)
    issues.extend((*entry_issues, *semantics_issues, *protective_issues))

    if issues:
        ambiguous_codes = {
            IssueCode.AMBIGUOUS_SYMBOL,
            IssueCode.AMBIGUOUS_SIDE,
            IssueCode.CONFLICTING_DIRECTION_BIAS,
            IssueCode.TOO_MANY_ENTRY_LEVELS,
        }
        event_type = EventType.UNKNOWN if any(issue.code in ambiguous_codes for issue in issues) else EventType.INCOMPLETE_SIGNAL
        return _incomplete_or_unknown(event_type, normalized, issues)

    assert alias is not None and canonical is not None and side is not None
    assert entry is not None and stop_loss is not None
    return _canonical_result(
        event=event,
        normalized=normalized,
        profile=profile,
        profile_name=profile_name,
        parser_family="narrative_signal",
        symbol_alias=alias,
        canonical_symbol=canonical,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=take_profits,
    )


def parse_event(
    event: RawTelegramEvent,
    *,
    profile: object,
    profile_name: str | None = None,
    symbol_aliases: Mapping[str, str] | None = None,
) -> ParseResult:
    """Classify and parse one Telegram event under an explicit channel profile."""

    normalized = normalize_text(event.text)
    if not normalized:
        return _incomplete_or_unknown(
            EventType.UNKNOWN,
            normalized,
            (_issue(IssueCode.EMPTY_MESSAGE, "Empty Telegram text is not actionable."),),
        )

    text_upper = normalized.upper()
    if "(SIGNAL)" in text_upper and "(FORECAST)" in text_upper:
        return _incomplete_or_unknown(
            EventType.UNKNOWN,
            normalized,
            (
                _issue(
                    IssueCode.CONFLICTING_MARKERS,
                    "Both (signal) and (forecast) markers are present.",
                ),
            ),
        )
    non_signal = _non_signal_classification(text_upper, profile)
    if non_signal is not None:
        event_type, issues = non_signal
        return _incomplete_or_unknown(event_type, normalized, issues)

    if _AMBIGUOUS_COMMA_NUMBER.search(text_upper):
        return _incomplete_or_unknown(
            EventType.UNKNOWN,
            normalized,
            (
                _issue(
                    IssueCode.INVALID_DECIMAL,
                    "Comma-separated numbers are ambiguous and are not normalized into prices.",
                ),
            ),
        )

    family = _parser_family(profile)
    if family is None:
        return _incomplete_or_unknown(
            EventType.UNKNOWN,
            normalized,
            (_issue(IssueCode.UNKNOWN_PARSER, "Channel profile selects an unknown parser."),),
        )

    # UnitedSignals analysis remains a FORECAST even when its profile also
    # requires SUGGESTED TRADE for executable candidates.
    if (
        family == "suggested_trade"
        and "SUGGESTED TRADE" not in text_upper
        and re.search(r"\b(?:BULLISH|BEARISH)\b", text_upper)
    ):
        return _incomplete_or_unknown(EventType.FORECAST, normalized, ())

    if not _required_markers_present(text_upper, profile):
        return _incomplete_or_unknown(
            EventType.UNKNOWN,
            normalized,
            (
                _issue(
                    IssueCode.REQUIRED_MARKER_MISSING,
                    "One or more profile-required markers are absent.",
                ),
            ),
        )

    if family == "compact_gold":
        return _parse_compact(event, normalized, profile, profile_name, symbol_aliases)
    if family == "suggested_trade":
        return _parse_suggested_trade(event, normalized, profile, profile_name, symbol_aliases)
    return _parse_narrative_signal(event, normalized, profile, profile_name, symbol_aliases)


def parse_message(
    text: str,
    *,
    profile: object,
    source_channel_id: int,
    source_message_id: int,
    profile_name: str | None = None,
    symbol_aliases: Mapping[str, str] | None = None,
    revision: int = 1,
    is_edit: bool = False,
    reply_to_message_id: int | None = None,
    forward_origin: str | None = None,
    message_time_utc: datetime | None = None,
) -> ParseResult:
    """Convenience wrapper for callers that have not built a raw event object."""

    return parse_event(
        RawTelegramEvent(
            channel_id=source_channel_id,
            message_id=source_message_id,
            text=text,
            revision=revision,
            is_edit=is_edit,
            reply_to_message_id=reply_to_message_id,
            forward_origin=forward_origin,
            message_time_utc=message_time_utc,
        ),
        profile=profile,
        profile_name=profile_name,
        symbol_aliases=symbol_aliases,
    )
