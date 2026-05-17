"""Environment configuration helpers for JailCall."""

from __future__ import annotations

import os


def require_env(name: str) -> str:
    """Return the value of an environment variable.

    Args:
        name: Name of the environment variable to read.

    Returns:
        The non-empty value of the variable.

    Raises:
        RuntimeError: If the variable is unset or empty.
    """
    value = os.environ.get(name)
    if not value:
        msg = f"Missing required environment variable: {name}"
        raise RuntimeError(msg)
    return value
