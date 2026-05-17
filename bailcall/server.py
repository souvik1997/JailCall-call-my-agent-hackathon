"""BailCall webhook server entry point.

Run with either:

* ``uv run python -m bailcall.server`` (uses the constants below), or
* ``uv run uvicorn bailcall.server:app --host 127.0.0.1 --port 5321 --reload``.
"""

from __future__ import annotations

from typing import Final

import uvicorn
from fastapi import FastAPI

HOST: Final[str] = "127.0.0.1"
PORT: Final[int] = 5321

app = FastAPI(title="BailCall")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Return health status for monitoring probes."""
    return {"status": "ok"}


def main() -> None:
    """Run the dev server on ``HOST:PORT``."""
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
