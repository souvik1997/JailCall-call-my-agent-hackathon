# Federal Street-Crime Staging Sources

Scope: staged Bay Area / N.D. Cal. federal criminal rows collected before research was stopped on 2026-05-17. This file and `cases.tsv` are intentionally limited to `law_firms/_staging/federal_street_crime/`.

Primary sources used:

- CourtListener RECAP search API results for N.D. Cal. criminal dockets, limited to 2024-01-01 through 2026-05-17.
- CourtListener public docket pages linked in `cases.tsv`.
- CourtListener RECAP PDFs already surfaced by search results, including criminal complaints, a government detention memorandum, and a defense sentencing memorandum.
- Existing read-only local corpus files under `law_firms/` were used only as context/provenance checks. No existing corpus files were modified.

Selection rules:

- Kept rows where the case was federal, N.D. Cal., recent, and had an actual charge description from RECAP docket text or an available RECAP PDF.
- Prioritized drug trafficking, fentanyl, methamphetamine, firearms/felon-in-possession, and child exploitation. A few candidate robbery/assault/carjacking hits were seen, but not staged unless counsel and charge text were both clear enough before the stop request.
- Included defense firm/counsel when CourtListener returned counsel metadata or the RECAP filing identified defense counsel. Public defender offices are included where private counsel was not available.
- Rows marked `medium` have exact charge text but a less certain attorney-to-firm pairing from CourtListener search metadata.

Known limits:

- Broad research was stopped before reaching the requested 25-60 row target.
- Some high-signal RECAP hits lacked defense counsel metadata or exact charge text in the visible result and were left out.
- CourtListener object endpoints required authentication, so unauthenticated RECAP search results and public PDF URLs were used.
- CourtListener web pages intermittently returned CloudFront 403 to direct `curl`; the search API and storage PDF URLs were still usable.

Counts:

- `cases.tsv` rows excluding header: 11
- Distinct dockets: 8
- High-confidence rows: 10
- Medium-confidence rows: 1
