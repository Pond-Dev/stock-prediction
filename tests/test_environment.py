import pytest

from tgxm.environment import (
    EnvironmentError,
    load_environment_file,
    load_integer_allowlist,
    load_text_allowlist,
    require_environment,
)


def test_environment_loader_never_echoes_secret_value(monkeypatch):
    monkeypatch.setenv("TEST_IDS", "123,not-an-id")

    with pytest.raises(EnvironmentError) as captured:
        load_integer_allowlist("TEST_IDS")

    assert "not-an-id" not in str(captured.value)
    assert "TEST_IDS" in str(captured.value)


def test_allowlists_are_trimmed_and_deduplicated(monkeypatch):
    monkeypatch.setenv("TEST_IDS", "123, 456,123")
    monkeypatch.setenv("TEST_SERVERS", "Demo-A, Demo-B,Demo-A")

    assert load_integer_allowlist("TEST_IDS") == frozenset({123, 456})
    assert load_text_allowlist("TEST_SERVERS") == frozenset({"Demo-A", "Demo-B"})


def test_missing_variable_is_named_but_not_given_a_value(monkeypatch):
    monkeypatch.delenv("TEST_MISSING", raising=False)
    with pytest.raises(EnvironmentError, match="TEST_MISSING"):
        require_environment("TEST_MISSING")


def test_missing_env_file_is_optional(tmp_path):
    assert load_environment_file(tmp_path / "not-created.env") is False
