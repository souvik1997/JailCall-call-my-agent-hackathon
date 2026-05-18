"""One-shot scraper — fill missing ``Email:`` fields in ``law_firms/*/firm.txt``.

Uses Browser Use to crawl each firm's website / intake URL, find a
contact email, and write it back into ``firm.txt``. Run once before the
demo; after a successful run, every firm in the corpus has an email and
``email_attorneys`` can dispatch to all of them (taking Browser Use out
of the runtime critical path).

Usage::

    uv run python -m jailcall.scrape_firm_emails --dry-run
    uv run python -m jailcall.scrape_firm_emails
    uv run python -m jailcall.scrape_firm_emails --limit 5 --parallelism 2

After it finishes, rebuild the Moss index so the new emails surface in
``moss_find_lawyers`` results::

    uv run python -m jailcall.build_index

Requires ``BROWSER_USE_API_KEY`` in ``.env``. Costs roughly $0.05 to $0.10
per firm at default settings; tune via ``BROWSER_USE_SCRAPE_MAX_COST_USD``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from browser_use_sdk.v3 import AsyncBrowserUse
from dotenv import load_dotenv

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
LAW_FIRMS_DIR: Final[Path] = ROOT / "law_firms"

_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
)

logger = logging.getLogger("jailcall.scrape_firm_emails")


@dataclass
class FirmRow:
    """One firm parsed from ``firm.txt``."""

    short_name: str
    path: Path
    name: str
    website: str
    intake_url: str
    email: str  # current value, empty if missing


def _parse_firm(path: Path) -> FirmRow:
    """Pick the small set of fields the scraper needs out of ``firm.txt``."""
    name = website = intake_url = email = ""
    for line in path.read_text().splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        v = value.strip()
        if key == "Name":
            name = v
        elif key == "Website":
            website = v
        elif key == "Intake URL":
            intake_url = v
        elif key == "Email":
            email = v
    return FirmRow(
        short_name=path.parent.name,
        path=path,
        name=name,
        website=website,
        intake_url=intake_url,
        email=email,
    )


def _write_email(path: Path, email: str) -> None:
    """Rewrite ``firm.txt`` so the ``Email:`` line has the new value."""
    lines = path.read_text().splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("Email:") and not replaced:
            out.append(f"Email: {email}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        # No Email: line in the file — append one.
        out.append(f"Email: {email}")
    _ = path.write_text("\n".join(out) + "\n")


def _read_emails_from_firm_txt() -> dict[str, str]:
    """Collect ``{short_name: email}`` by walking every ``firm.txt`` on disk."""
    emails: dict[str, str] = {}
    for d in sorted(LAW_FIRMS_DIR.iterdir()):
        if not d.is_dir():
            continue
        firm_txt = d / "firm.txt"
        if not firm_txt.exists():
            continue
        for line in firm_txt.read_text().splitlines():
            if line.startswith("Email:"):
                emails[d.name] = line.split(":", 1)[1].strip()
                break
    return emails


def _sync_emails_into_profile_tsv() -> int:
    """Copy every firm's ``Email:`` from ``firm.txt`` into ``firm_profiles.tsv``.

    ``jailcall.build_index`` reads firm metadata from the TSV (not from
    individual ``firm.txt`` files), so any ``firm.txt`` write that doesn't
    propagate to the TSV will be invisible to Moss after the next build.
    Running this sync at the end of a scrape keeps both in lockstep.

    Returns the count of TSV rows updated.
    """
    tsv = LAW_FIRMS_DIR / "firm_profiles.tsv"
    if not tsv.exists():
        logger.warning("firm_profiles.tsv not found; skipping TSV email sync")
        return 0

    emails = _read_emails_from_firm_txt()

    with tsv.open(newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if "EMAIL" not in fieldnames or "SHORT_NAME" not in fieldnames:
        logger.warning(
            "firm_profiles.tsv missing required columns (have %s); skipping sync",
            fieldnames,
        )
        return 0

    updated = 0
    for row in rows:
        short = row.get("SHORT_NAME", "")
        new_email = emails.get(short, "")
        if new_email and row.get("EMAIL", "") != new_email:
            row["EMAIL"] = new_email
            updated += 1

    if updated > 0:
        with tsv.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)
    return updated


def _needs_scrape(row: FirmRow, *, retry_noreply: bool) -> bool:
    """Whether ``row`` should be (re-)scraped on this run."""
    if not row.email:
        return True
    return retry_noreply and row.email.lower().startswith("noreply@")


def _find_missing_email_firms(
    limit: int | None,
    *,
    retry_noreply: bool = False,
) -> list[FirmRow]:
    """Walk law_firms/ and return rows that need a scrape pass.

    Default: only firms with a completely empty ``Email:``. With
    ``retry_noreply=True``, also re-scrapes firms whose current email is
    a ``noreply@<domain>`` placeholder — Browser Use is non-deterministic,
    so a second pass may discover a real address that the first missed.
    """
    rows: list[FirmRow] = []
    for d in sorted(LAW_FIRMS_DIR.iterdir()):
        if not d.is_dir():
            continue
        firm_txt = d / "firm.txt"
        if not firm_txt.exists():
            continue
        row = _parse_firm(firm_txt)
        if _needs_scrape(row, retry_noreply=retry_noreply):
            rows.append(row)
        if limit is not None and len(rows) >= limit:
            break
    return rows


def _build_task_prompt(firm: FirmRow, target_url: str) -> str:
    """Compose the Browser Use task instruction for one firm."""
    return "\n".join(
        [
            "You are scraping a law firm's website for a contact email address.",
            f"Target firm: {firm.name or firm.short_name}",
            f"Start URL: {target_url}",
            "",
            "Strategy:",
            "1. Open the start URL.",
            "2. If you don't see an email on the landing page, look for a 'Contact',"
            + " 'Contact Us', 'Get in touch', or footer section. Try clicking into those.",
            "3. Look for addresses like 'intake@', 'contact@', 'info@', 'admin@', or a"
            + " named attorney's address. Phone numbers and forms don't count —"
            + " you need an email.",
            "4. If after a thorough look you find no email, that's a valid outcome.",
            "",
            "Return ONLY the single best contact email address as your final output,"
            + " with no surrounding text or explanation. If no email exists on the site,"
            + " return exactly the literal string NONE (uppercase).",
            "",
            "Do not create an account, do not submit any form, do not interact beyond"
            + " navigating pages and reading them.",
        ],
    )


async def _scrape_one(
    *,
    client: AsyncBrowserUse,
    firm: FirmRow,
    max_cost_usd: float,
) -> str:
    """Run Browser Use on one firm. Returns the discovered email, or ``""``."""
    target = firm.intake_url or firm.website
    if not target:
        logger.warning("skip %s — no website or intake URL", firm.short_name)
        return ""
    task = _build_task_prompt(firm, target)
    try:
        result = await client.run(
            task,
            model=os.environ.get("BROWSER_USE_MODEL") or None,
            max_cost_usd=max_cost_usd,
        )
    except Exception:
        logger.exception("Browser Use scrape failed for %s", firm.short_name)
        return ""
    raw_output = getattr(result, "output", "") or ""
    output = str(raw_output).strip()
    if not output or output.upper() == "NONE":
        return ""
    match = _EMAIL_RE.search(output)
    return match.group(0) if match else ""


async def _scrape_all(
    firms: list[FirmRow],
    *,
    parallelism: int,
    dry_run: bool,
    max_cost_usd: float,
) -> dict[str, str]:
    """Scrape every firm in ``firms``. Returns ``{short_name: email}``."""
    if dry_run:
        for firm in firms:
            target = firm.intake_url or firm.website
            logger.info("[dry-run] would scrape %s from %s", firm.short_name, target)
        return {}

    semaphore = asyncio.Semaphore(parallelism)
    client = AsyncBrowserUse(timeout=180)
    results: dict[str, str] = {}

    async def go(firm: FirmRow) -> None:
        async with semaphore:
            email = await _scrape_one(client=client, firm=firm, max_cost_usd=max_cost_usd)
            results[firm.short_name] = email
            logger.info(
                "scraped %s → %s",
                firm.short_name,
                email or "(none found)",
            )

    _ = await asyncio.gather(*(go(f) for f in firms))
    return results


async def _amain(argv: list[str]) -> int:
    """Async entry point."""
    parser = argparse.ArgumentParser(
        description="Scrape contact emails for firms missing the Email: field.",
    )
    _ = parser.add_argument("--dry-run", action="store_true", help="List firms; do not scrape.")
    _ = parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N firms (useful to validate before a full run).",
    )
    _ = parser.add_argument(
        "--parallelism",
        type=int,
        default=3,
        help="Concurrent Browser Use sessions (default 3).",
    )
    _ = parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=float(os.environ.get("BROWSER_USE_SCRAPE_MAX_COST_USD", "1.00")),
        help="Per-session cost cap (default $1.00 or env override).",
    )
    _ = parser.add_argument(
        "--retry-noreply",
        action="store_true",
        help=(
            "Also re-scrape firms whose current email is a noreply@<domain> "
            "placeholder — Browser Use is non-deterministic, so a second pass "
            "may discover a real address."
        ),
    )
    args = parser.parse_args(argv)
    dry_run: bool = cast("bool", args.dry_run)
    limit: int | None = cast("int | None", args.limit)
    retry_noreply: bool = cast("bool", args.retry_noreply)
    parallelism: int = cast("int", args.parallelism)
    max_cost_usd: float = cast("float", args.max_cost_usd)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    _ = load_dotenv()

    firms = _find_missing_email_firms(limit, retry_noreply=retry_noreply)
    if not firms:
        logger.info("no firms missing email — nothing to do")
        return 0

    logger.info(
        "found %d firms missing email; parallelism=%d max_cost=$%.2f dry_run=%s",
        len(firms),
        parallelism,
        max_cost_usd,
        dry_run,
    )
    for f in firms:
        logger.info(
            "  - %s (%s)",
            f.short_name,
            f.intake_url or f.website or "(no URL)",
        )

    results = await _scrape_all(
        firms,
        parallelism=parallelism,
        dry_run=dry_run,
        max_cost_usd=max_cost_usd,
    )
    if dry_run:
        return 0

    wrote = 0
    misses: list[str] = []
    for firm in firms:
        email = results.get(firm.short_name, "")
        if email:
            _write_email(firm.path, email)
            wrote += 1
        else:
            misses.append(firm.short_name)

    logger.info("wrote %d emails", wrote)
    if misses:
        logger.info("could not find email for %d firm(s):", len(misses))
        for m in misses:
            logger.info("  - %s", m)

    # firm_profiles.tsv is what jailcall.build_index reads — keep it in
    # lockstep with the firm.txt files we just updated, otherwise Moss
    # will serve stale (empty) emails after the next build.
    synced = _sync_emails_into_profile_tsv()
    logger.info("synced %d firm rows into firm_profiles.tsv", synced)

    logger.info("now rebuild moss: uv run python -m jailcall.build_index --push")
    return 0


def main() -> int:
    """Sync entry point for ``python -m jailcall.scrape_firm_emails``."""
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
