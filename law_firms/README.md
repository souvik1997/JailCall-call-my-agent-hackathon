# Bay Area Criminal Defense Firm Research Dataset

Generated from public records and public law-firm pages for JailCall research. The dataset is now firm-first because the demo needs fast routing from a caller's general charge category to reachable Bay Area criminal defense firms.

Primary demo files:

- `firm_profiles.tsv` — enriched routing table with contact info, counties, charge tags, representative cases, and source URLs.
- `moss_documents.jsonl` — one Moss-ready document per firm; this is the best input for `build_index.py`.
- `law_firms.tsv` — legacy four-column roster: `NAME`, `SHORT_NAME`, `WEBSITE`, `PHONE`.
- `cases.tsv` — offense-searchable representative cases with actual charge signals and descriptions.
- `offense_taxonomy.tsv` — normalized tags used by firm profiles and case rows.

Each firm directory contains `firm.txt`; firms with representative public-record cases also contain one or more `case-*/case.txt` files.

Primary case sources are CourtListener RECAP metadata for N.D. California federal criminal dockets and California appellate opinions for state/local-origin criminal cases. Firm-practice coverage and contact fields come from public firm pages and public legal-resource/directories when a direct firm page was not enough.

Contact fields are intentionally blank unless they could be tied back to the same firm with reasonable confidence. Some docket records are historical and attorneys may have changed firms, so stale or mismatched State Bar contact details were removed or left blank. Verify all records before production outreach.

## Offense-searchable corpus

`cases.tsv` is the useful lookup table for BailCall routing. It indexes the subset of cases where public CourtListener/RECAP docket metadata or California appellate opinion text gave an actual offense signal such as `fentanyl`, `money-laundering`, `insider-trading`, `robbery`, `burglary`, `firearm`, or `witness-dissuasion`.

Each `case-offense-* / case.txt` file includes:

- `Offense tags`
- `Actual charges / charge signals`
- `Charge confidence`
- `Crime description`
- Source URL and snippets from the public record

The broader `case-*` folders are still retained as raw public-record firm/case appearances. For JailCall routing, prefer `firm_profiles.tsv` or `moss_documents.jsonl`; use `cases.tsv` and `case-offense-*` as supporting evidence when the caller's charge category maps to a specific representative matter.

## Curation strategy

Do not optimize for the largest possible case count. For the hackathon demo, a smaller set of reachable firms with explicit charge tags is more useful than thousands of noisy docket entries. The recommended index path is:

1. Read `moss_documents.jsonl`.
2. Index one document per firm.
3. Query with the caller's general charge category, for example `DUI criminal defense attorney`.
4. Contact the top firms using `phone`, `email`, or `intake_url` metadata.

Run `python3 law_firms/curate_corpus.py` after adding staged data under `law_firms/_staging/*/firms.tsv` or `law_firms/_staging/*/cases.tsv`.
