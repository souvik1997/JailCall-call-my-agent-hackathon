"""Pretty-print eval scenarios as a play-script.

The source format (``evals/transcripts.jsonl``) is dense JSONL — one
1.5KB line per scenario. Easy for machines, hard for humans. This
viewer formats one or more scenarios as a readable script so you can
skim them top-to-bottom and check whether the test cases actually
match how the agent should behave.

Usage::

    uv run python -m evals.show                       # all scenarios
    uv run python -m evals.show happy_path_dui        # one
    uv run python -m evals.show happy_path_dui refuses_consent
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = ROOT / "evals" / "transcripts.jsonl"

# Locked beginMessage from SPEC.md → Voice script → Opening.
BEGIN_MESSAGE = (
    "You've reached JailCall. I am not a lawyer and this call may be "
    "recorded by the facility. Do not tell me what happened or any "
    "details about your case. I can help contact a criminal defense "
    "attorney on your behalf right now. Would you like me to do that?"
)


def _wrap(text: str, indent: int) -> str:
    """Indent multi-line text; wraps each line with leading spaces."""
    pad = " " * indent
    return "\n".join(pad + line for line in text.splitlines())


def _fmt_state(state: dict[str, Any]) -> str:
    """Render a state dict as ``key=value, key=value``."""
    return ", ".join(f"{k}={v!r}" for k, v in state.items())


def _print_turn(turn: dict[str, Any], i: int) -> None:
    """Print one turn block: caller line + every agent expectation."""
    caller = str(turn.get("caller", ""))
    expect = turn.get("expect") or {}

    print(f"  TURN {i + 1}")
    print(f'    CALLER: "{caller}"')

    intent = expect.get("intent")
    if intent:
        print(f"    AGENT intent:        {intent}")

    must_inc = expect.get("agent_must_include")
    if isinstance(must_inc, list) and must_inc:
        joined = ", ".join(f'"{p}"' for p in must_inc)
        print(f"    AGENT must say:      {joined}")

    must_not = expect.get("agent_must_not_include")
    if isinstance(must_not, list) and must_not:
        joined = ", ".join(f'"{p}"' for p in must_not)
        print(f"    AGENT must NOT say:  {joined}")

    tool_calls = expect.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        print(f"    AGENT tool calls:    {', '.join(tool_calls)}")
    elif isinstance(tool_calls, list):
        print("    AGENT tool calls:    (none)")

    state_after = expect.get("state_after")
    if isinstance(state_after, dict) and state_after:
        print(f"    State after:         {_fmt_state(state_after)}")


def _print_dispatch(dispatch: object, final_state: object) -> None:
    """Print the post-call dispatch block (or 'none' if intentionally null)."""
    if isinstance(dispatch, dict):
        print("    Dispatch (post-call browser/email work):")
        required = dispatch.get("required") or []
        any_of = dispatch.get("any_of") or []
        forbidden = dispatch.get("forbidden") or []
        if isinstance(required, list) and required:
            print(f"      required:   {', '.join(str(x) for x in required)}")
        if isinstance(any_of, list) and any_of:
            for group in any_of:
                if isinstance(group, list):
                    print(f"      any_of:     {' OR '.join(str(x) for x in group)}")
        if isinstance(forbidden, list) and forbidden:
            print(f"      forbidden:  {', '.join(str(x) for x in forbidden)}")
    elif dispatch is None and final_state:
        print("    Dispatch:       none (call ends before any tool calls)")


def _print_scenario(scn: dict[str, Any]) -> None:
    """Print one scenario as a play-script block."""
    sid = str(scn.get("id", "<unknown>"))
    desc = str(scn.get("description", ""))
    tags = scn.get("tags") or []

    print()
    print(f"═══ {sid} ═══")
    if desc:
        print(_wrap(desc, indent=0))
    if isinstance(tags, list) and tags:
        print(f"tags: {', '.join(str(t) for t in tags)}")

    print()
    print("  [opening]")
    print(f'    AGENT: "{BEGIN_MESSAGE}"')

    turns = scn.get("turns")
    if isinstance(turns, list):
        for i, t in enumerate(turns):
            if isinstance(t, dict):
                print()
                _print_turn(t, i)

    final_state = scn.get("final_state")
    if isinstance(final_state, dict) and final_state:
        print()
        print("  [hangup]")
        print(f"    Final state:    {_fmt_state(final_state)}")

    _print_dispatch(scn.get("expected_dispatch"), final_state)


def _load(path: Path) -> list[dict[str, Any]]:
    """Parse the JSONL source into a list of scenario dicts."""
    out: list[dict[str, Any]] = []
    with path.open() as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"line {line_no}: invalid JSON: {exc}", file=sys.stderr)
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def main() -> int:
    """Entry point for ``python -m evals.show``."""
    parser = argparse.ArgumentParser(description="Pretty-print eval scenarios.")
    _ = parser.add_argument(
        "ids",
        nargs="*",
        help="One or more scenario ids to show (default: all)",
    )
    args = parser.parse_args()
    wanted: list[str] = args.ids

    scenarios = _load(SCENARIOS_PATH)
    if wanted:
        wanted_set = set(wanted)
        scenarios = [s for s in scenarios if s.get("id") in wanted_set]
        missing = wanted_set - {str(s.get("id")) for s in scenarios}
        if missing:
            print(f"unknown ids: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    for scn in scenarios:
        _print_scenario(scn)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
