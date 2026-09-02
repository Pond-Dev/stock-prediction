"""Strict loaders for non-persisted runtime allowlists and credentials."""

from __future__ import annotations

import os
from pathlib import Path


class EnvironmentError(ValueError):
    """Raised without including secret values in the error message."""


def load_environment_file(path: str | os.PathLike[str] | None) -> bool:
    """Load an ignored dotenv file without replacing process-level values."""

    if path is None:
        return False
    env_path = Path(path)
    if not env_path.exists():
        return False
    try:
        from dotenv import dotenv_values
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise EnvironmentError(
            "python-dotenv is required to load an env file; install the project first"
        ) from exc
    try:
        values = dotenv_values(env_path)
    except Exception as exc:
        raise EnvironmentError(f"could not parse environment file: {env_path}") from exc
    invalid_names = [
        str(name)
        for name, value in values.items()
        if not name or value is None
    ]
    if invalid_names:
        raise EnvironmentError(
            f"environment file has missing values for {', '.join(invalid_names)}"
        )
    for name, value in values.items():
        assert value is not None
        os.environ.setdefault(name, value)
    return True


def require_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise EnvironmentError(f"required environment variable is missing: {name}")
    return value.strip()


def load_integer_allowlist(name: str) -> frozenset[int]:
    raw = require_environment(name)
    values: set[int] = set()
    for item in raw.split(","):
        text = item.strip()
        try:
            value = int(text)
        except ValueError as exc:
            raise EnvironmentError(
                f"{name} must be a comma-separated list of positive integers"
            ) from exc
        if value <= 0:
            raise EnvironmentError(
                f"{name} must be a comma-separated list of positive integers"
            )
        values.add(value)
    if not values:
        raise EnvironmentError(f"{name} must not be empty")
    return frozenset(values)


def load_text_allowlist(name: str) -> frozenset[str]:
    raw = require_environment(name)
    values = frozenset(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise EnvironmentError(f"{name} must not be empty")
    return values
