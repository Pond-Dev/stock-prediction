from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from dataclasses import replace
from types import SimpleNamespace

import tgxm.cli as cli
from tgxm.broker import BrokerUnavailableError
from tgxm.config import AppConfig, save_config
from tgxm.cli import main
from tgxm.indicator import Candle
from tgxm.webtrader_click import WebTraderIdentityError


def _install_webtrader_login_fakes(
    monkeypatch,
    *,
    identity_error: Exception | None = None,
    input_error: BaseException | None = None,
    margin_mode: str = "RETAIL_HEDGING",
):
    events: list[str] = []
    state: dict[str, object] = {}
    login = "9081726354"
    server = "XMGlobal-Demo 17"

    class FakeMetaTrader5Broker:
        def __init__(self, **kwargs):
            state["mt5_kwargs"] = kwargs

        def initialize(self):
            events.append("mt5.initialize")

        def discover_account(self):
            events.append("mt5.discover_account")
            return SimpleNamespace(
                login=login,
                server=server,
                margin_mode=margin_mode,
            )

        def shutdown(self):
            events.append("mt5.shutdown")

    class FakePlaywrightWebTraderClicker:
        def __init__(self, **kwargs):
            state["clicker_kwargs"] = kwargs

        def initialize(self):
            events.append("clicker.initialize")

        def inspect_identity(self, **kwargs):
            events.append("clicker.inspect_identity")
            state["identity_kwargs"] = kwargs
            if identity_error is not None:
                raise identity_error
            return SimpleNamespace(
                login=login,
                server=server,
                is_demo=True,
            )

        def shutdown(self):
            events.append("clicker.shutdown")

    def fake_input(prompt):
        events.append("manual.input")
        state["input_prompt"] = prompt
        if input_error is not None:
            raise input_error
        return ""

    def fake_load_environment_file(path):
        events.append("environment.load")
        state["env_file"] = path
        return True

    monkeypatch.setattr(cli, "MetaTrader5ReadOnlyVerifier", FakeMetaTrader5Broker)
    monkeypatch.setattr(
        cli, "PlaywrightWebTraderClicker", FakePlaywrightWebTraderClicker
    )
    monkeypatch.setattr(cli, "load_environment_file", fake_load_environment_file)
    monkeypatch.setattr(cli, "load_integer_allowlist", lambda name: frozenset({9081726354}))
    monkeypatch.setattr(cli, "load_text_allowlist", lambda name: frozenset({server}))
    monkeypatch.setattr("builtins.input", fake_input)
    return events, state, login, server


def test_cli_initializes_and_validates_safe_config(tmp_path, capsys):
    path = tmp_path / "settings.json"

    assert main(["--config", str(path), "init-config"]) == 0
    assert main(["--config", str(path), "validate-config"]) == 0

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["runtime"]["mode"] == "observe"
    assert data["risk"]["fixed_lot"] == 0.01
    assert "valid config" in capsys.readouterr().out


def test_cli_refuses_to_replace_config_without_force(tmp_path, capsys):
    path = tmp_path / "settings.json"
    assert main(["--config", str(path), "init-config"]) == 0

    assert main(["--config", str(path), "init-config"]) == 2
    assert "already exists" in capsys.readouterr().err


def test_cli_parses_compact_signal_offline(tmp_path, capsys):
    path = tmp_path / "settings.json"
    assert main(["--config", str(path), "init-config"]) == 0
    capsys.readouterr()

    result = main(
        [
            "--config",
            str(path),
            "parse",
            "--channel",
            "mr_charlie",
            "--peer-id",
            "-100123",
            "--text",
            "GOLD SELL 4601 OR 4605 SL 4618 TP 4595 TP 4590 PRIVATE_MARKER",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["event_type"] == "NEW_SIGNAL"
    assert output["signal"]["canonical_symbol"] == "GOLD"
    assert "normalized_text" not in output
    assert "PRIVATE_MARKER" not in json.dumps(output)


def test_cli_non_signal_is_successfully_classified_but_not_executable(tmp_path, capsys):
    path = tmp_path / "settings.json"
    assert main(["--config", str(path), "init-config"]) == 0
    capsys.readouterr()

    result = main(
        [
            "--config",
            str(path),
            "parse",
            "--channel",
            "mr_charlie",
            "--peer-id",
            "-100123",
            "--text",
            "GOLD SELL TP2 HIT PROFIT DONE",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["event_type"] == "RESULT_UPDATE"
    assert output["signal"] is None


def test_cli_replay_persists_privately_without_echoing_raw_text(tmp_path, capsys):
    path = tmp_path / "settings.json"
    database = tmp_path / "runtime.sqlite3"
    replay = tmp_path / "events.jsonl"
    config = AppConfig.default()
    profile = replace(config.channels["mr_charlie"], peer_id=-100123)
    save_config(
        replace(config, channels={**config.channels, "mr_charlie": profile}), path
    )
    private_text = "GOLD SELL 4601 OR 4605 SL 4618 TP 4595"
    replay.write_text(
        json.dumps(
            {
                "peer_id": -100123,
                "message_id": 44,
                "text": private_text,
                "message_time_utc": "2026-08-27T03:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--config",
                str(path),
                "replay",
                "--file",
                str(replay),
                "--db",
                str(database),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert private_text not in output
    result = json.loads(output)
    assert result["processed"] == 1
    assert result["decision_counts"] == {"OBSERVED": 1}


def test_doctor_reports_readiness_without_secret_values(tmp_path, capsys, monkeypatch):
    path = tmp_path / "settings.json"
    save_config(AppConfig.default(), path)
    monkeypatch.setattr(cli, "_browser_channel_available", lambda channel: False)
    for name in (
        "TGXM_TELEGRAM_API_ID",
        "TGXM_TELEGRAM_API_HASH",
        "TGXM_TELEGRAM_SESSION",
        "TGXM_ALLOWED_DEMO_ACCOUNTS",
        "TGXM_ALLOWED_DEMO_SERVERS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert main(["--config", str(path), "doctor"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["readiness"]["observe"] is False
    assert report["config"]["profiles_missing_peer_id"] == ["mr_charlie"]
    assert "telegram-secret" not in output


def test_replay_validation_is_atomic_and_rejects_wrong_field_types(tmp_path, capsys):
    path = tmp_path / "settings.json"
    database = tmp_path / "runtime.sqlite3"
    replay = tmp_path / "bad.jsonl"
    config = AppConfig.default()
    profile = replace(config.channels["mr_charlie"], peer_id=-100123)
    save_config(replace(config, channels={**config.channels, "mr_charlie": profile}), path)
    valid = {
        "peer_id": -100123,
        "message_id": 1,
        "text": "GOLD SELL 4601 SL 4618 TP 4595 PRIVATE_ATOMIC",
        "message_time_utc": "2026-08-27T03:00:00Z",
    }
    invalid = {**valid, "message_id": 2, "revision": True}
    replay.write_text(
        json.dumps(valid) + "\n" + json.dumps(invalid) + "\n",
        encoding="utf-8",
    )

    assert main([
        "--config", str(path), "replay", "--file", str(replay), "--db", str(database)
    ]) == 2
    captured = capsys.readouterr()
    assert "PRIVATE_ATOMIC" not in captured.err
    assert not database.exists()


def test_webtrader_login_uses_mt5_evidence_and_prints_only_sanitized_success(
    tmp_path, capsys, monkeypatch
):
    path = tmp_path / "settings.json"
    env_file = tmp_path / "manual.env"
    config = AppConfig.default()
    save_config(config, path)
    events, state, login, server = _install_webtrader_login_fakes(monkeypatch)

    result = main(
        [
            "--config",
            str(path),
            "--env-file",
            str(env_file),
            "webtrader-login",
        ]
    )

    assert result == 0
    assert events == [
        "environment.load",
        "mt5.initialize",
        "mt5.discover_account",
        "clicker.initialize",
        "manual.input",
        "clicker.inspect_identity",
        "clicker.shutdown",
        "mt5.shutdown",
    ]
    assert state["env_file"] == str(env_file)
    policy = state["mt5_kwargs"]["policy"]
    assert policy.allowed_demo_accounts == frozenset({login})
    assert policy.allowed_servers == frozenset({server})
    assert state["mt5_kwargs"]["terminal_path"] is None
    clicker_kwargs = state["clicker_kwargs"]
    assert clicker_kwargs == {
        "url": config.broker.webtrader_url,
        "allowed_origins": config.broker.webtrader_allowed_origins,
        "profile_dir": config.broker.webtrader_profile_path,
        "headless": False,
        "browser_channel": config.broker.webtrader_browser_channel,
        "action_timeout_seconds": config.broker.webtrader_timeout_seconds,
        "receipt_timeout_seconds": config.broker.webtrader_timeout_seconds,
    }
    assert state["identity_kwargs"] == {
        "expected_login": login,
        "expected_server": server,
    }
    assert login not in state["input_prompt"]
    assert server not in state["input_prompt"]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert login not in captured.out
    assert server not in captured.out
    assert json.loads(captured.out) == {
        "account_details_printed": False,
        "credentials_handled": False,
        "demo": True,
        "matched_mt5_identity": True,
        "status": "WEBTRADER_DEMO_VERIFIED",
    }


def test_webtrader_login_requires_webtrader_adapter_before_constructing_clients(
    tmp_path, capsys, monkeypatch
):
    path = tmp_path / "settings.json"
    config = AppConfig.default()
    save_config(replace(config, broker=replace(config.broker, adapter="mt5")), path)
    constructed: list[str] = []
    monkeypatch.setattr(cli, "load_environment_file", lambda path: True)
    monkeypatch.setattr(
        cli,
        "MetaTrader5ReadOnlyVerifier",
        lambda **kwargs: constructed.append("mt5"),
    )
    monkeypatch.setattr(
        cli,
        "PlaywrightWebTraderClicker",
        lambda **kwargs: constructed.append("browser"),
    )

    assert main(["--config", str(path), "webtrader-login"]) == 2

    captured = capsys.readouterr()
    assert "requires broker.adapter=xm_webtrader" in captured.err
    assert constructed == []


def test_webtrader_login_rejects_non_hedging_account_before_browser_start(
    tmp_path, capsys, monkeypatch
):
    path = tmp_path / "settings.json"
    save_config(AppConfig.default(), path)
    events, state, login, server = _install_webtrader_login_fakes(
        monkeypatch,
        margin_mode="RETAIL_NETTING",
    )

    assert main(["--config", str(path), "webtrader-login"]) == 2

    assert events == [
        "environment.load",
        "mt5.initialize",
        "mt5.discover_account",
        "mt5.shutdown",
    ]
    assert "clicker_kwargs" not in state
    captured = capsys.readouterr()
    assert "RETAIL_HEDGING" in captured.err
    assert login not in captured.err
    assert server not in captured.err


def test_webtrader_login_catches_identity_error_and_closes_both_clients(
    tmp_path, capsys, monkeypatch
):
    path = tmp_path / "settings.json"
    save_config(AppConfig.default(), path)
    events, _, login, server = _install_webtrader_login_fakes(
        monkeypatch,
        identity_error=WebTraderIdentityError(
            "visible WebTrader identity did not match expected Demo account"
        ),
    )

    assert main(["--config", str(path), "webtrader-login"]) == 2

    assert events[-2:] == ["clicker.shutdown", "mt5.shutdown"]
    captured = capsys.readouterr()
    assert "identity did not match" in captured.err
    assert login not in captured.out + captured.err
    assert server not in captured.out + captured.err


def test_webtrader_login_cancellation_is_fail_closed_and_cleans_up(
    tmp_path, capsys, monkeypatch
):
    path = tmp_path / "settings.json"
    save_config(AppConfig.default(), path)
    events, _, _, _ = _install_webtrader_login_fakes(
        monkeypatch,
        input_error=EOFError(),
    )

    assert main(["--config", str(path), "webtrader-login"]) == 2

    assert "clicker.inspect_identity" not in events
    assert events[-2:] == ["clicker.shutdown", "mt5.shutdown"]
    assert "manual WebTrader login was cancelled" in capsys.readouterr().err


def _flat_candles(count: int, *, end: datetime, minutes: int = 15) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            time_utc=end - timedelta(minutes=minutes * (count - 1 - index)),
            open=Decimal("100"),
            high=Decimal("100.1"),
            low=Decimal("99.9"),
            close=Decimal("100"),
        )
        for index in range(count)
    )


def _install_predict_fakes(monkeypatch, *, candles=(), fetch_error=None):
    events: list[str] = []
    state: dict[str, object] = {}

    class FakeCandleSource:
        def __init__(self, **kwargs):
            state["kwargs"] = kwargs

        def initialize(self):
            events.append("source.initialize")

        def fetch_candles(self, symbol, timeframe, count):
            events.append("source.fetch_candles")
            state["fetch_args"] = (symbol, timeframe, count)
            if fetch_error is not None:
                raise fetch_error
            return candles

        def shutdown(self):
            events.append("source.shutdown")

    monkeypatch.setattr(cli, "MetaTrader5CandleSource", FakeCandleSource)
    monkeypatch.setattr(cli, "load_integer_allowlist", lambda name: frozenset({9081726354}))
    monkeypatch.setattr(cli, "load_text_allowlist", lambda name: frozenset({"XMGlobal-Demo 17"}))
    return events, state


def test_predict_is_advisory_only_and_never_touches_order_execution(
    tmp_path, capsys, monkeypatch
):
    path = tmp_path / "settings.json"
    config = AppConfig.default()
    save_config(replace(config, broker=replace(config.broker, terminal_path="C:/mt5/terminal64.exe")), path)
    candles = _flat_candles(60, end=datetime.now(UTC) - timedelta(seconds=5))
    events, state = _install_predict_fakes(monkeypatch, candles=candles)

    assert main(["--config", str(path), "predict"]) == 0

    assert events == ["source.initialize", "source.fetch_candles", "source.shutdown"]
    assert state["fetch_args"] == ("GOLD", "M15", 300)
    policy = state["kwargs"]["policy"]
    assert policy.allowed_symbols == frozenset({"GOLD"})
    output = json.loads(capsys.readouterr().out)
    assert output["advisory_only"] is True
    assert output["connected_to_order_execution"] is False
    assert output["prediction"]["symbol"] == "GOLD"
    assert output["prediction"]["timeframe"] == "M15"
    assert output["prediction"]["state"] in {"BUY", "SELL", "NO_SIGNAL"}


def test_predict_requires_terminal_path(tmp_path, capsys):
    path = tmp_path / "settings.json"
    save_config(AppConfig.default(), path)

    assert main(["--config", str(path), "predict"]) == 2
    assert "terminal_path" in capsys.readouterr().err


def test_predict_shuts_down_the_candle_source_even_on_fetch_failure(
    tmp_path, capsys, monkeypatch
):
    path = tmp_path / "settings.json"
    config = AppConfig.default()
    save_config(replace(config, broker=replace(config.broker, terminal_path="C:/mt5/terminal64.exe")), path)
    events, _ = _install_predict_fakes(
        monkeypatch, fetch_error=BrokerUnavailableError("mt5 unavailable")
    )

    assert main(["--config", str(path), "predict"]) == 2

    assert events == ["source.initialize", "source.fetch_candles", "source.shutdown"]
    assert "mt5 unavailable" in capsys.readouterr().err
