"""Scrape normalized per-firm website text for the JailCall law-firm dataset."""

# ruff: noqa: D103, E501, INP001, PLR2004, T201

from __future__ import annotations

import argparse
import csv
import re
import shutil
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Final
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

ROOT: Final[Path] = Path(__file__).resolve().parent
PROFILE_PATH: Final[Path] = ROOT / "firm_profiles.tsv"

USER_AGENT: Final[str] = (
    "Mozilla/5.0 (compatible; JailCallDatasetBot/1.0; +https://jailcall.local)"
)
MAX_RESPONSE_BYTES: Final[int] = 2_000_000
MAX_TEXT_CHARS_PER_PAGE: Final[int] = 50_000
TEXT_DIR_NAME: Final[str] = "site_text"

SKIP_EXTENSIONS: Final[tuple[str, ...]] = (
    ".7z",
    ".avi",
    ".css",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
)

LINK_KEYWORDS: Final[tuple[str, ...]] = (
    "criminal",
    "dui",
    "dwi",
    "domestic",
    "violence",
    "drug",
    "narcotic",
    "assault",
    "battery",
    "firearm",
    "gun",
    "weapon",
    "theft",
    "robbery",
    "burglary",
    "felony",
    "misdemeanor",
    "probation",
    "federal",
    "fraud",
    "white-collar",
    "case-results",
    "results",
    "practice",
    "contact",
)


@dataclass(frozen=True)
class Link:
    url: str
    text: str


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._current_href = ""
        self._current_link_text: list[str] = []
        self.title = ""
        self._in_title = False
        self.links: list[Link] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            attrs_dict = {name.lower(): value or "" for name, value in attrs}
            self._current_href = attrs_dict.get("href", "")
            self._current_link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._current_href:
            self.links.append(Link(self._current_href, clean(" ".join(self._current_link_text))))
            self._current_href = ""
            self._current_link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = clean(data)
        if not text:
            return
        if self._in_title:
            self.title = clean(f"{self.title} {text}")
        if self._current_href:
            self._current_link_text.append(text)
        self.text_parts.append(text)

    @property
    def text(self) -> str:
        return clean(" ".join(self.text_parts))


def clean(value: object) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text.replace("\t", " ")).strip()


def normalize_url(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    if re.match(r"https?://", value, re.IGNORECASE):
        return value
    return f"https://{value}"


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def url_variants(url: str) -> list[str]:
    url = normalize_url(url)
    if not url:
        return []
    parsed = urlparse(url)
    hosts = [parsed.netloc]
    if parsed.netloc.startswith("www."):
        hosts.append(parsed.netloc[4:])
    elif parsed.netloc:
        hosts.append(f"www.{parsed.netloc}")
    schemes = [parsed.scheme]
    schemes.append("http" if parsed.scheme == "https" else "https")

    variants: list[str] = []
    for scheme in dict.fromkeys(schemes):
        for host in dict.fromkeys(hosts):
            candidate = canonical_url(
                urlunparse((scheme, host, parsed.path or "/", "", parsed.query, ""))
            )
            if candidate not in variants:
                variants.append(candidate)
    return variants


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "page"


def host_key(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, root_url: str) -> bool:
    return host_key(url) == host_key(root_url)


def page_key(url: str) -> str:
    parsed = urlparse(url)
    return f"{host_key(url)}{parsed.path or '/'}?{parsed.query}"


def should_skip_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return True
    path = parsed.path.lower()
    return any(path.endswith(ext) for ext in SKIP_EXTENSIONS)


def read_profiles(limit: int | None) -> list[dict[str, str]]:
    with PROFILE_PATH.open(newline="", encoding="utf-8") as handle:
        rows = [{clean(k): clean(v) for k, v in row.items()} for row in csv.DictReader(handle, delimiter="\t")]
    return rows[:limit] if limit else rows


def fetch_page(url: str, timeout: float) -> tuple[int, str, str, list[Link], str]:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": USER_AGENT,
        },
    )
    context = ssl.create_default_context()
    with urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
        status = getattr(response, "status", 200)
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type.lower():
            return status, "", "", [], f"non-html content-type: {content_type}"
        raw = response.read(MAX_RESPONSE_BYTES)
        charset_match = re.search(r"charset=([^;]+)", content_type, re.IGNORECASE)
        encoding = charset_match.group(1).strip() if charset_match else "utf-8"
    html = raw.decode(encoding, errors="ignore")
    parser = PageParser()
    parser.feed(html)
    parser.close()
    return status, parser.title, parser.text, parser.links, ""


def text_file_name(index: int, url: str) -> str:
    parsed = urlparse(url)
    label = slugify(f"{parsed.netloc} {parsed.path}")
    return f"{index:02d}-{label[:80]}.txt"


def write_page_text_files(
    firm_dir: Path,
    documents: list[dict[str, str]],
) -> list[dict[str, str]]:
    text_dir = firm_dir / TEXT_DIR_NAME
    if text_dir.exists():
        shutil.rmtree(text_dir)
    text_dir.mkdir(parents=True, exist_ok=True)

    written: list[dict[str, str]] = []
    for index, document in enumerate(documents, start=1):
        filename = text_file_name(index, document["url"])
        path = text_dir / filename
        body = "\n".join(
            [
                f"Firm: {document.get('firm_name', '')}",
                f"URL: {document['url']}",
                f"Title: {document.get('title', '')}",
                "",
                document["text"],
                "",
            ]
        )
        path.write_text(body, encoding="utf-8")
        written.append(
            {
                "url": document["url"],
                "title": document.get("title", ""),
                "text_path": str(path.relative_to(ROOT)),
            }
        )
    return written


def rank_link(link: Link) -> int:
    haystack = f"{link.url} {link.text}".lower()
    return sum(3 if keyword in link.url.lower() else 1 for keyword in LINK_KEYWORDS if keyword in haystack)


def select_links(links: list[Link], *, base_url: str, seen: set[str], limit: int) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for link in links:
        href = clean(link.url)
        if not href or href.startswith(("#", "mailto:", "tel:", "sms:", "javascript:")):
            continue
        url = canonical_url(urljoin(base_url, href))
        if url in seen or should_skip_url(url) or not same_site(url, base_url):
            continue
        score = rank_link(Link(url, link.text))
        if score > 0:
            candidates.append((score, url))
    ranked = sorted(candidates, key=lambda item: (-item[0], item[1]))
    out: list[str] = []
    for _score, url in ranked:
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def index_firm(
    row: dict[str, str],
    *,
    max_pages: int,
    timeout: float,
) -> dict[str, object]:
    short_name = row["SHORT_NAME"]
    website = normalize_url(row.get("WEBSITE", ""))
    intake_url = normalize_url(row.get("INTAKE_URL", ""))
    queue = [
        variant
        for url in dict.fromkeys([website, intake_url])
        for variant in url_variants(url)
    ]
    seen: set[str] = set()
    successful_pages: set[str] = set()
    pages: list[dict[str, object]] = []
    documents: list[dict[str, str]] = []
    errors: list[str] = []

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen or should_skip_url(url):
            continue
        if page_key(url) in successful_pages:
            continue
        seen.add(url)
        try:
            status, title, text, links, error = fetch_page(url, timeout)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            continue
        if error:
            errors.append(f"{url}: {error}")
        successful_pages.add(page_key(url))
        page_text = text[:MAX_TEXT_CHARS_PER_PAGE]
        pages.append(
            {
                "url": url,
                "status": status,
                "title": title,
                "chars": len(text),
            }
        )
        if page_text:
            documents.append(
                {
                    "firm_name": row.get("NAME", ""),
                    "url": url,
                    "title": title,
                    "text": page_text,
                }
            )
        for link_url in select_links(links, base_url=url, seen=seen | set(queue), limit=max_pages - len(queue)):
            if len(queue) + len(pages) >= max_pages:
                break
            queue.append(link_url)

    firm_dir = ROOT / short_name
    firm_dir.mkdir(parents=True, exist_ok=True)
    text_files = write_page_text_files(firm_dir, documents)
    if not documents and not errors:
        errors.append("no usable normalized text extracted from fetched pages")
    if errors:
        (firm_dir / TEXT_DIR_NAME / "errors.txt").write_text(
            "\n".join(errors[:12]) + "\n",
            encoding="utf-8",
        )
    return {
        "id": short_name,
        "name": row.get("NAME", ""),
        "website": website,
        "intake_url": intake_url,
        "pages": pages,
        "text_files": text_files,
        "errors": errors[:8],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    rows = read_profiles(args.limit or None)
    records: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(
                index_firm,
                row,
                max_pages=args.max_pages,
                timeout=args.timeout,
            )
            for row in rows
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            record = future.result()
            records.append(record)
            print(
                f"[{index}/{len(futures)}] {record['id']} pages={len(record['pages'])} text_files={len(record['text_files'])} errors={len(record['errors'])}"
            )

    records.sort(key=lambda record: str(record["id"]))
    print(f"wrote per-firm normalized text for {len(records)} firms")


if __name__ == "__main__":
    main()
