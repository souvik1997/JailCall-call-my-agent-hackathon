"""Merge staged firm/case research into the JailCall law-firm corpus."""

# ruff: noqa: D103, E501, INP001, PLR2004, PLW2901, T201

from __future__ import annotations

import csv
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAGING = ROOT / "_staging"

CASE_COLUMNS = [
    "FIRM_NAME",
    "SHORT_NAME",
    "CASE_NAME",
    "JURISDICTION",
    "COURT",
    "DOCKET",
    "DATE_FILED",
    "OFFENSE_TAGS",
    "ACTUAL_CHARGES",
    "CONFIDENCE",
    "ATTORNEY",
    "DESCRIPTION",
    "SOURCE_URL",
    "CASE_PATH",
]

PROFILE_COLUMNS = [
    "NAME",
    "SHORT_NAME",
    "WEBSITE",
    "PHONE",
    "EMAIL",
    "INTAKE_URL",
    "COUNTIES",
    "PRIMARY_CHARGE_TAGS",
    "CASE_COUNT",
    "REPRESENTATIVE_CASES",
    "SOURCE_URLS",
    "PROFILE_TEXT",
]

SHORT_NAME_ALIASES = {
    "alanna-d-coopersmithe-attorney-at-law": "alanna-d-coopersmith-attorney-at-law",
    "beles-and-beles": "law-offices-of-beles-and-beles",
    "beles-and-beles-law-offices": "law-offices-of-beles-and-beles",
    "demetrius-costy": "demetrius-costy-law",
    "east-bay-defense": "alanna-d-coopersmith-attorney-at-law",
    "jayne-law-group": "jayne-law",
    "jayne-law-group-jayne-law-group": "jayne-law",
    "jpc-law": "law-office-of-john-p-campion",
    "lamano-law": "lamano-law-office",
    "law-office-of-adrienne-dell": "adrienne-dell",
    "law-office-of-allen-c-speare": "allen-speare",
    "law-office-of-andrew-cantor": "andrew-cantor",
    "law-office-of-javier-rios": "javier-rios",
    "law-office-of-vijay-dinakar": "vijay-dinakar",
    "law-offices-of-beles-and-beles-2": "law-offices-of-beles-and-beles",
    "law-offices-of-johnson-and-johnson": "johnson-and-johnson",
    "law-offices-of-marsanne-weese": "marsanne-weese",
    "law-offices-of-paula-canny": "paula-canny",
    "mowry-law-group": "mowry-law",
    "nieves-law": "the-nieves-law-firm",
    "nolan-barton-olmos-and-luciano-llp": "nbo-law",
    "offices-of-douglas-l-rappaport": "law-offices-of-douglas-l-rappaport",
    "rien-adams-and-cox-llp": "rien-adams-cox",
    "roberts-elliott-law-corp": "roberts-elliott",
    "the-law-office-of-adam-g-gasner-law-chambers-building": "gasner-criminal-law",
}

NAME_OVERRIDES = {
    "gasner-criminal-law": "Gasner Criminal Law",
}

BAY_AREA_COUNTIES = {
    "alameda",
    "contra costa",
    "marin",
    "napa",
    "san francisco",
    "san mateo",
    "santa clara",
    "solano",
    "sonoma",
}

COUNTY_ALIASES = {
    "albany": "alameda",
    "berkeley": "alameda",
    "oakland": "alameda",
    "bay area": "",
    "california": "",
    "northern california": "",
    "sacramento": "",
    "san francisco bay area": "",
    "san joaquin": "",
    "santa cruz": "",
}

TAG_ALIASES = {
    "criminal-defense": (),
    "domestic violence": ("domestic-violence",),
    "drug possession/sales": ("drug-possession", "drug-trafficking"),
    "homicide": ("murder",),
    "probation violation": ("probation-violation",),
    "reckless driving": ("reckless-driving",),
    "white-collar/fraud": ("white-collar-crime", "fraud"),
}

EXCLUDED_NAME_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bfederal defender\b",
        r"\bfederal public defender\b",
        r"\bpublic defender\b",
        r"\boffice of the federal public defender\b",
        r"\bdistrict attorney\b",
        r"\bunited states attorney\b",
        r"\bdepartment of justice\b",
    ]
]

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def clean(value: object) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text.replace("\t", " ")).strip()


def slugify(value: str) -> str:
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown-firm"


def canonical_short_name(value: str) -> str:
    value = clean(value)
    if value and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
        value = slugify(value)
    return SHORT_NAME_ALIASES.get(value, value)


def normalize_url(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    if re.match(r"https?://", value, re.IGNORECASE):
        return value
    return f"https://{value}"


def normalize_phone(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if value.startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return value


def normalize_tag_list(*values: str) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        for item in re.split(r"[,;|]", clean(value)):
            item = clean(item).lower()
            if not item:
                continue
            mapped = TAG_ALIASES.get(item, (item,))
            for tag in mapped:
                if tag and tag not in seen:
                    seen.add(tag)
                    merged.append(tag)
    return ", ".join(merged)


def has_dispatch_path(firm: dict[str, str]) -> bool:
    return bool(firm.get("INTAKE_URL") or firm.get("EMAIL"))


def has_usable_site_text(short_name: str) -> bool:
    site_text_dir = ROOT / short_name / "site_text"
    return any(
        path.name != "errors.txt" and path.stat().st_size > 0
        for path in site_text_dir.glob("*.txt")
    )


def is_excluded_entity(firm: dict[str, str]) -> bool:
    haystack = f"{firm.get('NAME', '')} {firm.get('SHORT_NAME', '')}"
    return any(pattern.search(haystack) for pattern in EXCLUDED_NAME_PATTERNS)


def is_runtime_profile(profile: dict[str, str]) -> bool:
    return (
        bool(profile.get("PRIMARY_CHARGE_TAGS"))
        and has_dispatch_path(profile)
        and has_usable_site_text(profile.get("SHORT_NAME", ""))
        and not is_excluded_entity(profile)
    )


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            {clean(k): clean(v) for k, v in row.items()}
            for row in csv.DictReader(handle, delimiter="\t")
        ]


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: clean(row.get(column, "")) for column in columns})


def parse_firm_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[clean(key).lower()] = clean(value)
    return data


def extracted_site_email(short_name: str) -> str:
    site_text_dir = ROOT / short_name / "site_text"
    if not site_text_dir.exists():
        return ""
    for text_file in sorted(site_text_dir.glob("*.txt")):
        if text_file.name == "errors.txt":
            continue
        text = text_file.read_text(encoding="utf-8", errors="ignore")
        for email in EMAIL_RE.findall(text):
            email = email.lower()
            if not email.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                return email
    return ""


def merge_value(current: str, incoming: str) -> str:
    current = clean(current)
    incoming = clean(incoming)
    return current or incoming


def merge_csv_list(*values: str) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        for item in re.split(r"[,;|]", clean(value)):
            item = clean(item).lower()
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return ", ".join(merged)


def normalize_counties(*values: str) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        for item in re.split(r"[,;|]", clean(value)):
            item = clean(item).lower()
            if not item:
                continue
            item = COUNTY_ALIASES.get(item, item)
            if item in BAY_AREA_COUNTIES and item not in seen:
                seen.add(item)
                merged.append(item)
    return ", ".join(merged)


def load_taxonomy_terms() -> dict[str, str]:
    return {
        normalize_tag_list(row.get("TAG", "")): clean(row.get("MATCH_TERMS", ""))
        for row in read_tsv(ROOT / "offense_taxonomy.tsv")
        if normalize_tag_list(row.get("TAG", ""))
    }


def caller_match_terms(tags: str, taxonomy_terms: dict[str, str]) -> str:
    terms: list[str] = []
    seen: set[str] = set()
    for tag in re.split(r"[,;|]", tags):
        tag = clean(tag).lower()
        if not tag:
            continue
        for term in [tag.replace("-", " "), *re.split(r"[,;|]", taxonomy_terms.get(tag, ""))]:
            term = clean(term).lower()
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
    return ", ".join(terms[:24])


def load_firms() -> dict[str, dict[str, str]]:
    firms: dict[str, dict[str, str]] = {}

    for row in read_tsv(ROOT / "law_firms.tsv"):
        short_name = canonical_short_name(row.get("SHORT_NAME", "")) or slugify(row.get("NAME", ""))
        if not short_name:
            continue
        firms[short_name] = {
            "NAME": NAME_OVERRIDES.get(short_name, clean(row.get("NAME"))),
            "SHORT_NAME": short_name,
            "WEBSITE": normalize_url(row.get("WEBSITE", "")),
            "PHONE": normalize_phone(row.get("PHONE", "")),
            "EMAIL": "",
            "INTAKE_URL": "",
            "COUNTIES": "",
            "PRIMARY_CHARGE_TAGS": "",
            "SOURCE_URLS": "",
        }

    for firm_file in ROOT.glob("*/firm.txt"):
        info = parse_firm_file(firm_file)
        short_name = canonical_short_name(info.get("short name") or firm_file.parent.name)
        firm = firms.setdefault(
            short_name,
            {
                "NAME": info.get("name", short_name.replace("-", " ").title()),
                "SHORT_NAME": short_name,
                "WEBSITE": "",
                "PHONE": "",
                "EMAIL": "",
                "INTAKE_URL": "",
                "COUNTIES": "",
                "PRIMARY_CHARGE_TAGS": "",
                "SOURCE_URLS": "",
            },
        )
        firm["NAME"] = NAME_OVERRIDES.get(short_name, merge_value(firm.get("NAME", ""), info.get("name", "")))
        firm["WEBSITE"] = merge_value(firm.get("WEBSITE", ""), normalize_url(info.get("website", "")))
        firm["PHONE"] = merge_value(firm.get("PHONE", ""), normalize_phone(info.get("phone", "")))
        firm["EMAIL"] = merge_value(firm.get("EMAIL", ""), info.get("email", ""))
        firm["INTAKE_URL"] = merge_value(firm.get("INTAKE_URL", ""), normalize_url(info.get("intake url", "")))
        firm["COUNTIES"] = normalize_counties(firm.get("COUNTIES", ""), info.get("counties", ""))

    for staged in STAGING.glob("*/firms.tsv"):
        for row in read_tsv(staged):
            name = clean(row.get("NAME"))
            short_name = canonical_short_name(row.get("SHORT_NAME", "")) or slugify(name)
            if not name or not short_name:
                continue
            firm = firms.setdefault(
                short_name,
                {
                    "NAME": name,
                    "SHORT_NAME": short_name,
                    "WEBSITE": "",
                    "PHONE": "",
                    "EMAIL": "",
                    "INTAKE_URL": "",
                    "COUNTIES": "",
                    "PRIMARY_CHARGE_TAGS": "",
                    "SOURCE_URLS": "",
                },
            )
            firm["NAME"] = NAME_OVERRIDES.get(short_name, name or firm.get("NAME", ""))
            firm["WEBSITE"] = merge_value(firm.get("WEBSITE", ""), normalize_url(row.get("WEBSITE", "")))
            firm["PHONE"] = merge_value(firm.get("PHONE", ""), normalize_phone(row.get("PHONE", "")))
            firm["EMAIL"] = merge_value(firm.get("EMAIL", ""), row.get("EMAIL", ""))
            firm["INTAKE_URL"] = merge_value(firm.get("INTAKE_URL", ""), normalize_url(row.get("INTAKE_URL", "")))
            firm["COUNTIES"] = normalize_counties(firm.get("COUNTIES", ""), row.get("COUNTIES", ""))
            firm["PRIMARY_CHARGE_TAGS"] = normalize_tag_list(
                firm.get("PRIMARY_CHARGE_TAGS", ""),
                row.get("PRIMARY_CHARGE_TAGS", ""),
            )
            firm["SOURCE_URLS"] = merge_csv_list(
                firm.get("SOURCE_URLS", ""), row.get("SOURCE_URL", "")
            )

    return firms


def load_cases() -> list[dict[str, str]]:
    deduped: dict[tuple[str, str, str, str], dict[str, str]] = {}
    sources = [ROOT / "cases.tsv", *STAGING.glob("*/cases.tsv")]
    for source in sources:
        for row in read_tsv(source):
            case = {
                "FIRM_NAME": clean(row.get("FIRM_NAME")),
                "SHORT_NAME": canonical_short_name(row.get("SHORT_NAME", ""))
                or slugify(row.get("FIRM_NAME", "")),
                "CASE_NAME": clean(row.get("CASE_NAME")),
                "JURISDICTION": clean(row.get("JURISDICTION")),
                "COURT": clean(row.get("COURT")),
                "DOCKET": clean(row.get("DOCKET")),
                "DATE_FILED": clean(row.get("DATE_FILED")),
                "OFFENSE_TAGS": normalize_tag_list(row.get("OFFENSE_TAGS", "")),
                "ACTUAL_CHARGES": clean(row.get("ACTUAL_CHARGES")),
                "CONFIDENCE": clean(row.get("CONFIDENCE")) or "medium",
                "ATTORNEY": clean(row.get("ATTORNEY")),
                "DESCRIPTION": clean(row.get("DESCRIPTION")),
                "SOURCE_URL": clean(row.get("SOURCE_URL")),
                "CASE_PATH": clean(row.get("CASE_PATH")),
            }
            if case["CASE_PATH"] and not case["CASE_PATH"].startswith(f"{case['SHORT_NAME']}/"):
                case["CASE_PATH"] = ""
            if not case["FIRM_NAME"] or not case["CASE_NAME"] or not case["OFFENSE_TAGS"]:
                continue
            key = (
                case["SHORT_NAME"],
                case["CASE_NAME"].lower(),
                case["DOCKET"].lower(),
                case["OFFENSE_TAGS"].lower(),
            )
            existing = deduped.get(key)
            if not existing:
                deduped[key] = case
                continue
            for column in CASE_COLUMNS:
                existing[column] = merge_value(existing.get(column, ""), case.get(column, ""))

    return sorted(
        deduped.values(),
        key=lambda row: (
            row["SHORT_NAME"],
            row["DATE_FILED"],
            row["CASE_NAME"],
            row["OFFENSE_TAGS"],
        ),
    )


def case_summary(case: dict[str, str]) -> str:
    tags = case.get("OFFENSE_TAGS", "")
    charges = case.get("ACTUAL_CHARGES", "")
    name = case.get("CASE_NAME", "")
    if charges:
        return f"{name} ({tags}: {charges})"
    return f"{name} ({tags})"


def build_profiles(
    firms: dict[str, dict[str, str]], cases: list[dict[str, str]]
) -> list[dict[str, str]]:
    cases_by_firm: dict[str, list[dict[str, str]]] = defaultdict(list)
    tags_by_firm: dict[str, Counter[str]] = defaultdict(Counter)
    taxonomy_terms = load_taxonomy_terms()

    for case in cases:
        short_name = case["SHORT_NAME"]
        cases_by_firm[short_name].append(case)
        for tag in re.split(r"[,;|]", case.get("OFFENSE_TAGS", "")):
            tag = clean(tag).lower()
            if tag:
                tags_by_firm[short_name][tag] += 1

    for short_name, tag_counts in tags_by_firm.items():
        firm = firms.setdefault(
            short_name,
            {
                "NAME": cases_by_firm[short_name][0]["FIRM_NAME"],
                "SHORT_NAME": short_name,
                "WEBSITE": "",
                "PHONE": "",
                "EMAIL": "",
                "INTAKE_URL": "",
                "COUNTIES": "",
                "PRIMARY_CHARGE_TAGS": "",
                "SOURCE_URLS": "",
            },
        )
        firm["PRIMARY_CHARGE_TAGS"] = normalize_tag_list(
            firm.get("PRIMARY_CHARGE_TAGS", ""),
            ", ".join(tag for tag, _count in tag_counts.most_common()),
        )

    profiles: list[dict[str, str]] = []
    for short_name, firm in sorted(
        firms.items(), key=lambda item: item[1].get("NAME", item[0]).lower()
    ):
        firm_cases = sorted(
            cases_by_firm.get(short_name, []),
            key=lambda row: row.get("DATE_FILED", ""),
            reverse=True,
        )
        representative = "; ".join(case_summary(case) for case in firm_cases[:5])
        source_urls = merge_csv_list(
            firm.get("SOURCE_URLS", ""),
            ", ".join(case.get("SOURCE_URL", "") for case in firm_cases[:5]),
        )
        case_tags = normalize_tag_list(
            ", ".join(tag for tag, _count in tags_by_firm.get(short_name, Counter()).most_common())
        )
        if case_tags:
            tags = normalize_tag_list(case_tags, firm.get("PRIMARY_CHARGE_TAGS", ""))
        else:
            tags = normalize_tag_list(firm.get("PRIMARY_CHARGE_TAGS", ""))
        match_terms = caller_match_terms(tags, taxonomy_terms)
        email = merge_value(firm.get("EMAIL", ""), extracted_site_email(short_name))
        profile_text = " ".join(
            part
            for part in [
                f"{firm.get('NAME', '')} is a Bay Area criminal defense firm.",
                f"Counties served: {firm.get('COUNTIES', '')}." if firm.get("COUNTIES") else "",
                f"Charge categories: {tags}." if tags else "",
                f"Caller case details to match: {match_terms}." if match_terms else "",
                f"Representative cases: {representative}." if representative else "",
                "Useful for JailCall routing when a caller describes matching criminal charges, arrest facts, or case details.",
            ]
            if part
        )
        profiles.append(
            {
                "NAME": firm.get("NAME", ""),
                "SHORT_NAME": short_name,
                "WEBSITE": firm.get("WEBSITE", ""),
                "PHONE": firm.get("PHONE", ""),
                "EMAIL": email,
                "INTAKE_URL": firm.get("INTAKE_URL", ""),
                "COUNTIES": firm.get("COUNTIES", ""),
                "PRIMARY_CHARGE_TAGS": tags,
                "CASE_COUNT": str(len(firm_cases)),
                "REPRESENTATIVE_CASES": representative,
                "SOURCE_URLS": source_urls,
                "PROFILE_TEXT": profile_text,
            }
        )
    return profiles


def write_moss_documents(profiles: list[dict[str, str]]) -> None:
    with (ROOT / "moss_documents.jsonl").open("w", encoding="utf-8") as handle:
        for profile in profiles:
            doc = {
                "id": profile["SHORT_NAME"],
                "text": profile["PROFILE_TEXT"],
                "metadata": {
                    "name": profile["NAME"],
                    "short_name": profile["SHORT_NAME"],
                    "website": profile["WEBSITE"],
                    "phone": profile["PHONE"],
                    "email": profile["EMAIL"],
                    "intake_url": profile["INTAKE_URL"],
                    "counties": profile["COUNTIES"],
                    "charge_tags": profile["PRIMARY_CHARGE_TAGS"],
                    "case_count": profile["CASE_COUNT"],
                    "source_urls": profile["SOURCE_URLS"],
                },
            }
            handle.write(json.dumps(doc, ensure_ascii=True, sort_keys=True) + "\n")


def write_firm_files(profiles: list[dict[str, str]]) -> None:
    for profile in profiles:
        firm_dir = ROOT / profile["SHORT_NAME"]
        firm_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f"Name: {profile['NAME']}",
            f"Short name: {profile['SHORT_NAME']}",
            f"Website: {profile['WEBSITE']}",
            f"Phone: {profile['PHONE']}",
            f"Email: {profile['EMAIL']}",
            f"Intake URL: {profile['INTAKE_URL']}",
            f"Counties: {profile['COUNTIES']}",
            f"Primary charge tags: {profile['PRIMARY_CHARGE_TAGS']}",
            f"Representative case count: {profile['CASE_COUNT']}",
            f"Source URLs: {profile['SOURCE_URLS']}",
        ]
        (firm_dir / "firm.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_case_files(cases: list[dict[str, str]]) -> None:
    def case_line(label: str, value: object) -> str:
        value = clean(value)
        return f"{label}: {value}" if value else f"{label}:"

    for case in cases:
        tags = "-".join(
            slugify(tag) for tag in re.split(r"[,;|]", case.get("OFFENSE_TAGS", "")) if clean(tag)
        )
        if case.get("CASE_PATH"):
            case_path = ROOT / case["CASE_PATH"]
            case_dir = case_path.parent
        else:
            suffix = slugify(
                " ".join(part for part in [case["CASE_NAME"], case.get("DOCKET", "")] if part)
            )
            case_dir = ROOT / case["SHORT_NAME"] / f"case-offense-{tags}-{suffix}"
            case_path = case_dir / "case.txt"
            case["CASE_PATH"] = str(case_path.relative_to(ROOT))
        case_dir.mkdir(parents=True, exist_ok=True)
        extra_lines: list[str] = []
        if case_path.exists():
            existing_lines = case_path.read_text(encoding="utf-8").splitlines()
            firm_index = next(
                (index for index, line in enumerate(existing_lines) if line.startswith("Firm:")),
                None,
            )
            notes_index = next(
                (
                    index
                    for index, line in enumerate(existing_lines)
                    if line.startswith("Notes:")
                ),
                len(existing_lines),
            )
            if firm_index is not None and notes_index > firm_index:
                extra_lines = existing_lines[firm_index + 1 : notes_index]
        lines = [
            case_line("Case name", case["CASE_NAME"]),
            case_line("Jurisdiction level", case["JURISDICTION"]),
            case_line("Docket / appellate number", case["DOCKET"]),
            case_line("Court", case["COURT"]),
            case_line("Date filed", case["DATE_FILED"]),
            case_line("Offense tags", case["OFFENSE_TAGS"]),
            case_line("Actual charges / charge signals", case["ACTUAL_CHARGES"]),
            case_line("Charge confidence", case["CONFIDENCE"]),
            case_line("Crime description", case["DESCRIPTION"]),
            case_line("Source URL", case["SOURCE_URL"]),
            case_line("Attorney listed", case["ATTORNEY"]),
            case_line("Firm", case["FIRM_NAME"]),
            *extra_lines,
            "Notes: This representative case supports firm routing. It favors usable charge/category matching over exhaustive case history.",
        ]
        case_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def prune_generated_directories(profiles: list[dict[str, str]], cases: list[dict[str, str]]) -> None:
    kept_firms = {profile["SHORT_NAME"] for profile in profiles}
    kept_case_paths = {case["CASE_PATH"] for case in cases if case.get("CASE_PATH")}

    for firm_dir in ROOT.iterdir():
        if not firm_dir.is_dir() or firm_dir.name.startswith("_"):
            continue
        if not (firm_dir / "firm.txt").exists():
            continue
        if firm_dir.name not in kept_firms:
            shutil.rmtree(firm_dir)
            continue
        for case_dir in firm_dir.glob("case-*"):
            case_file = case_dir / "case.txt"
            if case_file.exists() and str(case_file.relative_to(ROOT)) not in kept_case_paths:
                shutil.rmtree(case_dir)


def update_summary(profiles: list[dict[str, str]], cases: list[dict[str, str]]) -> None:
    tag_counts: Counter[str] = Counter()
    for case in cases:
        for tag in re.split(r"[,;|]", case.get("OFFENSE_TAGS", "")):
            tag = clean(tag).lower()
            if tag:
                tag_counts[tag] += 1

    summary = {
        "firms": len(profiles),
        "firm_directories": len(
            [path for path in ROOT.iterdir() if path.is_dir() and not path.name.startswith("_")]
        ),
        "case_files": len(list(ROOT.glob("*/case-*/case.txt"))),
        "federal_cases": sum(
            1 for row in cases if row.get("JURISDICTION", "").lower().startswith("federal")
        ),
        "state_appellate_local_origin_cases": sum(
            1 for row in cases if "state" in row.get("JURISDICTION", "").lower()
        ),
        "rows_with_phone": sum(1 for row in profiles if row.get("PHONE")),
        "rows_with_website": sum(1 for row in profiles if row.get("WEBSITE")),
        "rows_with_email": sum(1 for row in profiles if row.get("EMAIL")),
        "rows_with_intake_url": sum(1 for row in profiles if row.get("INTAKE_URL")),
        "representative_case_rows": len(cases),
        "firms_with_representative_cases": len({row["SHORT_NAME"] for row in cases}),
        "site_text_files": len(
            [
                path
                for path in ROOT.glob("*/site_text/*.txt")
                if path.name != "errors.txt"
            ]
        ),
        "firms_with_site_text": len(
            {
                path.parent.parent.name
                for path in ROOT.glob("*/site_text/*.txt")
                if path.name != "errors.txt"
            }
        ),
        "offense_tag_counts": dict(sorted(tag_counts.items())),
        "cases_tsv": "law_firms/cases.tsv",
        "firm_profiles_tsv": "law_firms/firm_profiles.tsv",
        "moss_documents_jsonl": "law_firms/moss_documents.jsonl",
        "offense_taxonomy_tsv": "law_firms/offense_taxonomy.tsv",
        "source_note": "Firm profiles are optimized for JailCall/Moss routing. Representative cases use public CourtListener/RECAP, California appellate opinions, and staged public web research when available.",
    }
    (ROOT / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    firms = load_firms()
    cases = load_cases()
    profiles = [profile for profile in build_profiles(firms, cases) if is_runtime_profile(profile)]
    kept_firms = {profile["SHORT_NAME"] for profile in profiles}
    cases = [case for case in cases if case["SHORT_NAME"] in kept_firms]

    write_case_files(cases)
    prune_generated_directories(profiles, cases)
    write_tsv(ROOT / "cases.tsv", CASE_COLUMNS, cases)
    write_tsv(ROOT / "firm_profiles.tsv", PROFILE_COLUMNS, profiles)
    write_tsv(
        ROOT / "law_firms.tsv",
        ["NAME", "SHORT_NAME", "WEBSITE", "PHONE"],
        profiles,
    )
    write_moss_documents(profiles)
    write_firm_files(profiles)
    update_summary(profiles, cases)

    print(f"profiles={len(profiles)}")
    print(f"cases={len(cases)}")
    print(f"with_phone={sum(1 for row in profiles if row.get('PHONE'))}")
    print(f"with_email={sum(1 for row in profiles if row.get('EMAIL'))}")
    print(f"with_intake_url={sum(1 for row in profiles if row.get('INTAKE_URL'))}")


if __name__ == "__main__":
    main()
