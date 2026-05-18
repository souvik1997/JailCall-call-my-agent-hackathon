# Bay Area Criminal Defense Firm Research Dataset

Generated from public records and public law-firm pages for JailCall research. The dataset is firm-first because the runtime flow is: caller describes case details, the agent queries Moss, and the top results must be reachable Bay Area criminal defense firms.

Primary demo files:

- `firm_profiles.tsv` — enriched routing table with contact info, counties, charge tags, representative cases, and source URLs.
- `moss_documents.jsonl` — one compact firm summary per firm; useful for inspection and fallback.
- `law_firms.tsv` — legacy four-column roster: `NAME`, `SHORT_NAME`, `WEBSITE`, `PHONE`.
- `cases.tsv` — representative criminal cases with actual charge signals and descriptions.
- `offense_taxonomy.tsv` — normalized tags used by firm profiles and case rows.

Each kept firm directory contains `firm.txt`; firms with representative criminal-case examples also contain one or more `case-*/case.txt` files. Per-firm website scrape output lives next to the firm record:

- `site_text/*.txt` — normalized page text with HTML tags removed, ready for embedding/chunking.
- `site_text/errors.txt` — fetch failures for that firm, when a site blocked, timed out, or returned no usable text.

Primary case sources are CourtListener RECAP metadata for N.D. California federal criminal dockets, California appellate opinions for state/local-origin criminal cases, and firm-published criminal case-result pages. Firm-practice coverage and contact fields come from public firm pages and public legal-resource/directories when a direct firm page was not enough.

Rows without charge tags or a dispatch path are pruned from the runtime corpus. For this demo, a useful Moss hit is a firm with criminal-case language plus an email, intake URL, or website that Browser Use can act on.

## Representative cases

`cases.tsv` is not a second runtime corpus. It is supporting evidence used to enrich the firm documents with concrete criminal-case language. It keeps only the subset of cases where a public docket, opinion, or firm case-results page gave an actual offense signal such as `DUI`, `fentanyl`, `money-laundering`, `robbery`, `burglary`, `firearm`, or `domestic-violence`.

Each `case-offense-* / case.txt` file includes:

- `Offense tags`
- `Actual charges / charge signals`
- `Charge confidence`
- `Crime description`
- Source URL and snippets from the public record

For JailCall routing, build the Moss index with `uv run python -m jailcall.build_index`. The builder reads `firm.txt`, `site_text/*.txt`, and representative cases, then attaches the firm's contact metadata to every indexed chunk. Use `cases.tsv` and `case-offense-*` files only when you need to inspect why a firm document contains a particular criminal charge tag.

## Curation strategy

Do not optimize for the largest possible firm count. For the hackathon demo, a smaller set of reachable criminal-defense firms with explicit charge tags and caller-language match terms is more useful than a broad roster. The recommended index path is:

1. Run `uv run python -m jailcall.build_index` to validate the chunked firm documents.
2. Run `uv run python -m jailcall.build_index --push` when `MOSS_PROJECT_ID`, `MOSS_PROJECT_KEY`, and `MOSS_INDEX_NAME` are set.
3. Query with the caller's charge category or short case phrase, for example `DUI`, `drug possession with fentanyl`, or `domestic violence`.
4. De-dupe Moss hits by firm and contact the top firms using `phone`, `email`, or `intake_url` metadata.

Run `python3 law_firms/curate_corpus.py` after adding staged data under `law_firms/_staging/*/firms.tsv` or `law_firms/_staging/*/cases.tsv`.

Run `python3 law_firms/scrape_firm_sites.py --max-pages 4 --workers 24 --timeout 8` to refresh the per-firm normalized website text. The scraper writes per-firm files only; downstream embedding should read `law_firms/*/site_text/*.txt` directly.
