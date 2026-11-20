# Solicitation documents: precision check, 2026-11-19

Prompt **v4** (notice plus solicitation documents; pin `50f72e98…53634a7c15`), schema 2,
claude-haiku-5-5, effort low, max_tokens 4000. Production daily run of 2026-11-19 (18:05
UTC, commit `83fd010`), assessed with commit `35b61a0` (quote-source correction and the
per-gate v3/v4 combine). The four v4 calls cost about $0.017 (the run's 12 calls cost
$0.0262 in all; the log has no per-call split).

**This is a small sample: 8 answers on 4 tenders, checked by hand by the person who built
the tool, not blind. It measures precision only (are the answers given right?). Recall,
meaning what the documents say that the model missed, isn't measured.** There are no
labels for these tenders, and they aren't in the eval sets.

## Coverage

4 of the 80 open notices (open and kept, as `fetch_documents` counts them, on the
2026-11-19 production data) had at least one solicitation document the tool could read.

| Document rows on open notices | Count |
|---|---|
| Read (ok, text stored) | 21, on 4 notices |
| Not on the public CanadaBuys document host (`skipped_host`) | 116 |
| Over the per-notice limit of 8 documents (`over_tender_limit`) | 17 |
| Too many pages | 3 |
| HTTP error | 1 |
| Not a PDF | 1 |

32 of the 80 open notices list any attachment URL at all. Most documents sit behind the
supplier portals, which this tool doesn't log in to.

| Tender | Documents read | Cut by the 120,000-character cap |
|---|---|---|
| PW-__HN-446-81465 | 2 of 2 | no |
| PW-__PQ-992-74154 | 4 of 9 | yes |
| PW-_ERD-005-26943 | 2 of 2 | no |
| PW-__HP-539-79947 | 1 of 8 | yes |

## Gate answers sourced from a document: 5

| Tender | Gate | Answer | Source | Check |
|---|---|---|---|---|
| PW-__HN-446-81465 | Clearance | none / none | EBSU000 | supported |
| PW-__HN-446-81465 | Land claims | no | EBSU000 (source corrected) | supported |
| PW-__HN-446-81465 | Supply arrangement | no | EBSU000 (source corrected) | right, weakly evidenced: the quote is only "Request for Supply Arrangement" |
| PW-__HP-539-79947 | Supplier origin | Canadian and trade-agreement partners | EBSU000 (source corrected) | supported |
| PW-__HP-539-79947 | Clearance | none / none | EBSU000 | supported |

**4 supported, 1 right but weakly evidenced, 0 wrong.**

"Source corrected" means the model named the notice as the quote's source, but the quote is
in the document. In the v4 run these three quotes were dropped as ungrounded. Since
`35b61a0`, a quote found in another source the model was given is kept, with its source
corrected and flagged.

## Review decisions: 3

| Tender | Gate | Answer under review | Decision |
|---|---|---|---|
| PW-__PQ-992-74154 | Supply arrangement | v4: yes | wrong, corrected. The notice is a request for a supply arrangement (how firms qualify), not a competition among holders. |
| PW-__HP-539-79947 | Supply arrangement | v3: yes; v4: no (flagged `versions_disagree`) | v3 wrong. Resolved by accepting v4's "no": a request for a supply arrangement. |
| PW-_ERD-005-26943 | Work location | v3: not on-site (filled in where v4 had no quote) | rejected. The quote ("not a bid solicitation process…") doesn't speak to where the work is done. |

Both wrong supply-arrangement answers were on requests for a supply arrangement. That is the
gate the eval already found weakest (README, Findings).

## What this does and doesn't show

- 8 answers is too few for a rate. The point is that no document-sourced gate answer was
  wrong, and that the errors found were in supply arrangements and in a v3 answer carried
  forward by the combine.
- Precision only. Nobody read the 21 documents end to end to list what the model should
  have found, so misses are unknown.
- Coverage limits the value more than accuracy does: documents could be read for 4 of 80
  open notices.
