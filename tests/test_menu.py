from __future__ import annotations

from pathlib import Path
from typing import Iterable

from tgxm.config import AppConfig, load_config, save_config
from tgxm.menu import run_config_menu


def _input_script(responses: Iterable[str]):
    iterator = iter(responses)

    def read(_: str) -> str:
        return next(iterator)

    return read


def _index(mapping: dict[str, object], key: str) -> str:
    return str(list(mapping).index(key) + 1)


def test_quit_creates_valid_local_config_from_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config" / "settings.local.json"
    output: list[str] = []

    returned = run_config_menu(
        path,
        input_fn=_input_script(["q"]),
        output_fn=output.append,
    )

    assert path.exists()
    assert returned == AppConfig.default()
    assert load_config(path) == returned
    assert any("Live mode" in line for line in output)
    assert any("Saved validated configuration" in line for line in output)


def test_nested_menu_edits_channel_expiry_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.json"
    defaults = AppConfig.default().to_dict()
    root_channels = _index(defaults, "channels")
    mr_index = _index(defaults["channels"], "mr_charlie")
    expiry_index = _index(
        defaults["channels"]["mr_charlie"], "signal_expiry_minutes"
    )

    result = run_config_menu(
        path,
        input_fn=_input_script(
            [root_channels, mr_index, expiry_index, "45", "b", "b", "q"]
        ),
        output_fn=lambda _: None,
    )

    assert result.channels["mr_charlie"].signal_expiry_minutes == 45
    assert load_config(path) == result


def test_coupled_channel_policy_fields_can_be_edited_before_save(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.json"
    data = AppConfig.default().to_dict()
    channels_index = _index(data, "channels")
    profile_index = _index(data["channels"], "mr_charlie")
    profile = data["channels"]["mr_charlie"]
    semantics_index = _index(profile, "two_level_semantics")
    entry_index = _index(profile, "entry_mode")
    output: list[str] = []

    result = run_config_menu(
        path,
        input_fn=_input_script(
            [
                channels_index,
                profile_index,
                semantics_index,
                "manual_review",
                entry_index,
                "manual_review",
                "b",
                "b",
                "q",
            ]
        ),
        output_fn=output.append,
    )

    assert result.channels["mr_charlie"].two_level_semantics == "manual_review"
    assert result.channels["mr_charlie"].entry_mode == "manual_review"
    assert any("not yet valid" in line for line in output)


def test_menu_rejects_live_draft_until_operator_fixes_it(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.json"
    save_config(AppConfig.default(), path)
    root = AppConfig.default().to_dict()
    runtime_index = _index(root, "runtime")
    mode_index = _index(root["runtime"], "mode")
    output: list[str] = []

    result = run_config_menu(
        path,
        input_fn=_input_script(
            [
                runtime_index,
                mode_index,
                "live",
                "b",
                "q",
                runtime_index,
                mode_index,
                "observe",
                "b",
                "q",
            ]
        ),
        output_fn=output.append,
    )

    assert result.runtime.mode == "observe"
    assert load_config(path).runtime.mode == "observe"
    assert any("Not saved" in line and "Live trading" in line for line in output)


def test_menu_never_retains_or_persists_a_secret_value(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.json"
    root = AppConfig.default().to_dict()
    telegram_index = _index(root, "telegram")
    api_hash_index = _index(root["telegram"], "api_hash_env")
    output: list[str] = []

    result = run_config_menu(
        path,
        input_fn=_input_script(
            [telegram_index, api_hash_index, "actual-secret-hash", "b", "q"]
        ),
        output_fn=output.append,
    )

    assert result.telegram.api_hash_env == "TGXM_TELEGRAM_API_HASH"
    assert "actual-secret-hash" not in path.read_text(encoding="utf-8")
    assert any("never a secret value" in line for line in output)


def test_menu_adds_a_complete_editable_channel_profile(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.json"
    root = AppConfig.default().to_dict()
    channels_index = _index(root, "channels")
    new_profile = {
        "peer_id": None,
        "parser": "compact_gold_v1",
        "enabled": False,
        "trade_enabled": False,
        "allowed_symbols": [],
        "two_level_semantics": "zone_single_market",
        "entry_mode": "zone_single_market",
        "tp_strategy": "single_tp",
        "tp_index": 1,
        "signal_expiry_minutes": 30,
        "required_markers": [],
        "ignored_markers": [],
        "reject_open_sl": True,
        "profile_version": "1.0.0",
    }
    new_profile_index = str(len(root["channels"]) + 1)
    peer_index = _index(new_profile, "peer_id")
    enabled_index = _index(new_profile, "enabled")
    symbols_index = _index(new_profile, "allowed_symbols")

    result = run_config_menu(
        path,
        input_fn=_input_script(
            [
                channels_index,
                "a",
                "my_gold_room",
                new_profile_index,
                peer_index,
                "-1009876543210",
                symbols_index,
                '["GOLD"]',
                enabled_index,
                "true",
                "b",
                "b",
                "q",
            ]
        ),
        output_fn=lambda _: None,
    )

    profile = result.channels["my_gold_room"]
    assert profile.peer_id == -1009876543210
    assert profile.allowed_symbols == ("GOLD",)
    assert profile.enabled is True
    assert profile.trade_enabled is False


def test_menu_can_add_symbol_alias_and_refuses_deleting_last_canonical_mapping(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.local.json"
    root = AppConfig.default().to_dict()
    aliases_index = _index(root, "symbol_aliases")
    output: list[str] = []

    result = run_config_menu(
        path,
        input_fn=_input_script(
            [
                aliases_index,
                "a",
                "XAUUSD.M",
                "GOLD",
                "d",
                "GOLD",
                "d",
                "XAUUSD",
                "d",
                "XAUUSD.M",
                "b",
                "q",
            ]
        ),
        output_fn=output.append,
    )

    assert result.symbol_aliases["XAUUSD.M"] == "GOLD"
    assert set(result.symbol_aliases) == {"XAUUSD.M"}
    assert any("Cannot delete" in line for line in output)


def test_eof_leaves_existing_file_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.json"
    original = AppConfig.default()
    save_config(original, path)
    before = path.read_bytes()

    def eof(_: str) -> str:
        raise EOFError

    returned = run_config_menu(path, input_fn=eof, output_fn=lambda _: None)

    assert returned == original
    assert path.read_bytes() == before
