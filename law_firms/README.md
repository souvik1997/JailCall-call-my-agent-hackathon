# Bay Area Criminal Defense Firm Research Dataset

Generated from public records and public law-firm pages for JailCall research. The dataset is firm-first because the runtime flow is: caller describes case details, the agent queries Moss, and the top results must be reachable Bay Area criminal defense firms.

Primary demo files:

- `firm_profiles.tsv` — enriched routing table with contact info, counties, charge tags, representative cases, and source URLs.
- `moss_documents.jsonl` — one Moss-ready document per firm; this is the best input for `build_index.py`.
- `law_firms.tsv` — legacy four-column roster: `NAME`, `SHORT_NAME`, `WEBSITE`, `PHONE`.
- `cases.tsv` — offense-searchable representative cases with actual charge signals and descriptions.
- `offense_taxonomy.tsv` — normalized tags used by firm profiles and case rows.

Each kept firm directory contains `firm.txt`; firms with representative criminal-case examples also contain one or more `case-*/case.txt` files.

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

For JailCall routing, index `moss_documents.jsonl`. Use `cases.tsv` and `case-offense-*` files only when you need to inspect why a firm document contains a particular criminal charge tag.

## Curation strategy

Do not optimize for the largest possible firm count. For the hackathon demo, a smaller set of reachable criminal-defense firms with explicit charge tags and caller-language match terms is more useful than a broad roster. The recommended index path is:

1. Read `moss_documents.jsonl`.
2. Index one document per firm.
3. Query with the caller's freeform case details, for example `I got arrested for DUI after a traffic stop` or `drug possession with fentanyl`.
4. Contact the top firms using `phone`, `email`, or `intake_url` metadata.

Run `python3 law_firms/curate_corpus.py` after adding staged data under `law_firms/_staging/*/firms.tsv` or `law_firms/_staging/*/cases.tsv`.
