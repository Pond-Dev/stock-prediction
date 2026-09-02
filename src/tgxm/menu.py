"""Interactive terminal editor for validated bot configuration."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

from tgxm.config import (
    CONFIG_PATH,
    AppConfig,
    ChannelProfile,
    ConfigError,
    load_config,
    save_config,
)


InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]


_CHOICE_HINTS: dict[str, tuple[str, ...]] = {
    "runtime.mode": ("observe", "shadow", "demo_armed"),
    "broker.adapter": ("xm_webtrader", "mt5"),
    "broker.webtrader_browser_channel": ("chromium", "chrome", "msedge"),
    "broker.webtrader_selector_contract": ("xm-mt5-web-v1",),
    "risk.mode": ("fixed_lot", "risk_percent"),
    "risk.manual_exposure_policy": ("block", "allow_new"),
    "risk.same_side_conflict_policy": ("block", "allow_within_limits"),
    "risk.opposite_side_conflict_policy": ("block", "allow_hedge"),
    "execution.management_action_policy": ("notify_only",),
    "indicator.timeframe": ("M1", "M5", "M15", "M30", "H1", "H4", "D1"),
}
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _json_display(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _location(path: tuple[str, ...]) -> str:
    return "/" + "/".join(path)


def _at_path(root: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = root
    for segment in path:
        value = value[segment]
    return value


def _parse_value(text: str, current: Any) -> Any:
    stripped = text.strip()
    if not stripped:
        raise ConfigError("edit cancelled")
    if stripped.lower() == "none":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        if isinstance(current, str) or current is None:
            return stripped
        raise ConfigError("enter a valid JSON value (for example true, 30, or [\"GOLD\"])")


def _field_hint(path: tuple[str, ...]) -> str | None:
    dotted = ".".join(path)
    if len(path) >= 3 and path[0] == "channels":
        field = path[-1]
        channel_hints = {
            "parser": (
                "compact_gold_v1",
                "suggested_trade_v1",
                "narrative_signal_v1",
            ),
            "two_level_semantics": ("zone_single_market", "manual_review"),
            "entry_mode": ("zone_single_market", "manual_review"),
            "tp_strategy": ("single_tp",),
        }
        choices = channel_hints.get(field)
    else:
        choices = _CHOICE_HINTS.get(dotted)
    if choices is None:
        return None
    return "choices: " + ", ".join(choices)


def _show_mapping(
    mapping: dict[str, Any],
    path: tuple[str, ...],
    output_fn: OutputFn,
) -> list[str]:
    keys = list(mapping)
    output_fn("")
    output_fn(f"TGXM configuration: {_location(path)}")
    output_fn("Secret values are never entered here; use environment-variable names.")
    for index, key in enumerate(keys, start=1):
        value = mapping[key]
        rendered = "<section>" if isinstance(value, dict) else _json_display(value)
        output_fn(f"  {index}. {key}: {rendered}")
    commands = "number=select/edit, s=validate+save, q=save+quit"
    if path:
        commands += ", b=back"
    if path in {("channels",), ("symbol_aliases",)}:
        commands += ", a=add, d=delete"
    output_fn(commands)
    return keys


def _edit_leaf(
    draft: dict[str, Any],
    path: tuple[str, ...],
    input_fn: InputFn,
    output_fn: OutputFn,
) -> None:
    parent = _at_path(draft, path[:-1])
    current = parent[path[-1]]
    hint = _field_hint(path)
    if hint:
        output_fn(hint)
    try:
        raw = input_fn(
            f"New JSON value for {'.'.join(path)} (blank cancels) "
            f"[{_json_display(current)}]: "
        )
        updated = _parse_value(raw, current)
    except ConfigError as exc:
        output_fn(str(exc))
        return
    if path[-1].endswith("_env") and (
        not isinstance(updated, str) or not _ENV_NAME_RE.fullmatch(updated)
    ):
        output_fn(
            "Rejected: enter an uppercase environment-variable name, never a secret value."
        )
        return
    parent[path[-1]] = updated
    try:
        AppConfig.from_dict(draft)
    except ConfigError as exc:
        output_fn(f"Draft updated but is not yet valid: {exc}")
    else:
        output_fn("Draft updated and valid.")


def _add_item(
    draft: dict[str, Any],
    path: tuple[str, ...],
    input_fn: InputFn,
    output_fn: OutputFn,
) -> None:
    mapping = _at_path(draft, path)
    if path == ("channels",):
        name = input_fn("New stable channel profile name: ").strip()
        if not name:
            output_fn("Add cancelled.")
            return
        if name in mapping:
            output_fn(f"Channel profile {name!r} already exists.")
            return
        profile = ChannelProfile.disabled_default()
        mapping[name] = {
            "peer_id": profile.peer_id,
            "parser": profile.parser,
            "enabled": profile.enabled,
            "trade_enabled": profile.trade_enabled,
            "allowed_symbols": list(profile.allowed_symbols),
            "two_level_semantics": profile.two_level_semantics,
            "entry_mode": profile.entry_mode,
            "tp_strategy": profile.tp_strategy,
            "tp_index": profile.tp_index,
            "signal_expiry_minutes": profile.signal_expiry_minutes,
            "max_spread_points": profile.max_spread_points,
            "required_markers": list(profile.required_markers),
            "ignored_markers": list(profile.ignored_markers),
            "reject_open_sl": profile.reject_open_sl,
            "profile_version": profile.profile_version,
        }
        try:
            AppConfig.from_dict(draft)
        except ConfigError as exc:
            del mapping[name]
            output_fn(f"Cannot add profile: {exc}")
            return
        output_fn(f"Added disabled profile {name!r}; select it to configure fields.")
        return

    if path == ("symbol_aliases",):
        alias = input_fn("New uppercase Telegram symbol alias: ").strip()
        if not alias:
            output_fn("Add cancelled.")
            return
        if alias in mapping:
            output_fn(f"Symbol alias {alias!r} already exists.")
            return
        canonical = input_fn("Canonical symbol: ").strip()
        candidate = deepcopy(draft)
        _at_path(candidate, path)[alias] = canonical
        try:
            AppConfig.from_dict(candidate)
        except ConfigError as exc:
            output_fn(f"Cannot add alias: {exc}")
            return
        mapping[alias] = canonical
        output_fn(f"Added {alias} -> {canonical}.")
        return

    output_fn("Items can only be added to channels or symbol_aliases.")


def _delete_item(
    draft: dict[str, Any],
    path: tuple[str, ...],
    input_fn: InputFn,
    output_fn: OutputFn,
) -> None:
    mapping = _at_path(draft, path)
    name = input_fn("Exact key to delete (blank cancels): ").strip()
    if not name:
        output_fn("Delete cancelled.")
        return
    if name not in mapping:
        output_fn(f"No key named {name!r}.")
        return
    candidate = deepcopy(draft)
    del _at_path(candidate, path)[name]
    try:
        AppConfig.from_dict(candidate)
    except ConfigError as exc:
        output_fn(f"Cannot delete {name!r}: {exc}")
        return
    del mapping[name]
    output_fn(f"Deleted {name!r}.")


def _validated_save(
    draft: dict[str, Any], config_path: Path, output_fn: OutputFn
) -> AppConfig | None:
    try:
        config = AppConfig.from_dict(draft)
        save_config(config, config_path)
    except (ConfigError, OSError) as exc:
        output_fn(f"Not saved: {exc}")
        return None
    output_fn(f"Saved validated configuration to {config_path}.")
    return config


def run_config_menu(
    config_path: str | Path = CONFIG_PATH,
    *,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
) -> AppConfig:
    """Edit all persisted operational fields through a nested terminal menu.

    A missing local file starts from recommended defaults.  Draft edits may be
    temporarily inconsistent so coupled fields can be changed, but no bytes are
    written until the complete draft passes :func:`AppConfig.from_dict`.
    ``q`` validates, atomically saves, and returns the resulting configuration.
    """

    path = Path(config_path)
    original = load_config(path) if path.exists() else AppConfig.default()
    draft = deepcopy(original.to_dict())
    location: tuple[str, ...] = ()

    output_fn("TGXM safe configuration menu (Observe/Shadow/XM Demo only).")
    output_fn("Live mode and disabled safety invariants are always rejected.")

    while True:
        current = _at_path(draft, location)
        if not isinstance(current, dict):  # defensive: navigation only enters mappings
            raise ConfigError(f"menu location {_location(location)} is not a section")
        keys = _show_mapping(current, location, output_fn)
        try:
            command = input_fn("Select: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            output_fn("No changes saved.")
            return original

        if command == "q":
            saved = _validated_save(draft, path, output_fn)
            if saved is not None:
                return saved
            continue
        if command == "s":
            saved = _validated_save(draft, path, output_fn)
            if saved is not None:
                draft = deepcopy(saved.to_dict())
            continue
        if command == "b":
            if location:
                location = location[:-1]
            else:
                output_fn("Already at the root menu.")
            continue
        if command == "a":
            _add_item(draft, location, input_fn, output_fn)
            continue
        if command == "d":
            _delete_item(draft, location, input_fn, output_fn)
            continue

        try:
            selected = int(command)
        except ValueError:
            output_fn("Unknown command.")
            continue
        if not 1 <= selected <= len(keys):
            output_fn("Selection is out of range.")
            continue
        key = keys[selected - 1]
        selected_path = location + (key,)
        value = current[key]
        if isinstance(value, dict):
            location = selected_path
        else:
            _edit_leaf(draft, selected_path, input_fn, output_fn)


__all__ = ["run_config_menu"]
