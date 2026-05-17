# Federal White Collar Staging Sources

This staging file was flushed after the research turn was stopped. It is not an exhaustive 25-60 row pass. It contains only rows that were already confirmed with actual charge descriptions and defense counsel or firm from public docket, GovInfo, CourtListener/RECAP-derived local corpus, DOJ, or firm-profile sources.

## Included rows

- Rows in `cases.tsv`: 16
- Unique dockets: 8
- Date-filed range: 2024-01-17 through 2026-05-12
- Primary categories represented: wire fraud, securities fraud, insider trading, money laundering, health care fraud, mail fraud, customs-duty fraud/smuggling, obstruction

## Source notes

- CourtListener direct REST access required authentication during this run. For CourtListener/RECAP-backed rows, I used the existing local `law_firms/` CourtListener/RECAP corpus files and retained their public CourtListener docket URLs in `cases.tsv`.
- GovInfo public court PDFs were used where they directly showed defense counsel in the criminal docket.
- DOJ Northern District of California press releases and indictment PDFs were used for actual charge narratives where docket metadata alone was thin.
- DocketAlarm was used once as a public docket mirror for the Schatt/Podulka defense filing that listed retained defense counsel; DOJ pages supplied the charge narrative.

## URLs consulted

- United States v. Schatt et al public defense filing: https://www.docketalarm.com/cases/California_Northern_District_Court/3--24-cr-00243/USA_v._Schatt_et_al/59/
- DOJ Cred LLC charge release: https://www.justice.gov/usao-ndca/pr/former-ceo-cfo-and-cco-cred-llc-charged-alleged-multi-million-dollar-cryptocurrency
- DOJ Cred LLC sentencing release: https://www.justice.gov/usao-ndca/pr/former-ceo-and-cfo-cryptocurrency-lender-cred-llc-sentenced-multiple-years-prison-wire
- DOJ Cred LLC victim notification page: https://www.justice.gov/usao-ndca/us-v-daniel-schatt-and-joseph-podulka-24-cr-00243-wha-and-us-v-james-alexander-24-cr
- United States v. Nadimpalli GovInfo judgment: https://www.govinfo.gov/content/pkg/USCOURTS-cand-3_24-cr-00021/pdf/USCOURTS-cand-3_24-cr-00021-0.pdf
- DOJ SKAEL/Nadimpalli release: https://www.justice.gov/usao-ndca/pr/founder-and-former-ceo-artificial-intelligence-start-skael-charged-securities-fraud
- HWG Jonathan Baum profile: https://hwglaw.com/members/jonathan-m-baum/
- United States v. Shafi GovInfo defense filing: https://www.govinfo.gov/content/pkg/USCOURTS-cand-4_25-cr-00258/pdf/USCOURTS-cand-4_25-cr-00258-1.pdf
- DOJ IRL/Shafi release: https://www.justice.gov/usao-ndca/pr/former-silicon-valley-ceo-charged-fraud-and-obstruction-justice
- DOJ Shafi indictment PDF: https://www.justice.gov/usao-ndca/media/1412251/dl?inline=
- United States v. Hudson CourtListener docket: https://www.courtlistener.com/docket/71720407/united-states-v-hudson/
- United States v. Done Global, Inc CourtListener docket: https://www.courtlistener.com/docket/72049122/united-states-v-done-global-inc/
- DOJ Done Global conviction release: https://www.justice.gov/usao-ndca/pr/and-clinical-president-digital-health-company-convicted-100m-adderall-distribution-and
- United States v. SEALED CourtListener docket: https://www.courtlistener.com/docket/71271062/united-states-v-sealed/
- United States v. Fries CourtListener docket: https://www.courtlistener.com/docket/73332454/united-states-v-fries/
- United States v. Pan CourtListener docket: https://www.courtlistener.com/docket/72123703/united-states-v-pan/
- DOJ Pan customs-duty evasion release: https://www.justice.gov/usao-ndca/pr/bay-area-businessmen-chinese-national-and-three-companies-charged-scheme-evade
