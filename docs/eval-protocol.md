# Eval protocol

## Frozen versions (2026-11-11, before any labelling)

| Extractor | Version | Pinned hash (sha256) |
|---|---|---|
| LLM (claude-haiku-5-5, effort low, max_tokens 2000) | prompt `v2` | system prompt `def2a660…c9d3038` |
| | schema `1` | `ExtractionData` JSON schema `4fab443b…898e83e` |
| Keyword baseline | `rules-v1` | fingerprint of `keyword.py` + `eligibility/places.py` `04590c85…a4a9f76441` |

Also frozen on 2026-11-16, before any labelling:

| Component | Version | Pinned hash (sha256) |
|---|---|---|
| Eligibility rules | `RULES_VERSION` `3` | source of `eligibility/rules.py` + `eligibility/places.py` `69f130cd…1f02b63` |

Pinned 2026-11-18, **after** labelling, from the dev split only (see "Prompt v3 changes"),
before any test scoring:

| Extractor | Version | Pinned hash (sha256) |
|---|---|---|
| LLM (same model and settings as v2) | prompt `v3` | system prompt `12c49a09…bf129039` |

v3 was first pinned as `accc6f93…2fbac32d` and re-pinned the same day, before any test
scoring, after the "Invitation to Qualify" wording was removed (change 6 below). v2 stays
pinned and selectable; the site keeps using v2 (`PROMPT_VERSION`) until v3 has been
scored on the test split. The final test run scores v2, v3 and the baseline together.

Site switched to prompt v3 on 2026-11-18, after the single test run.

`tenders/tests/extraction/test_frozen.py` pins the full hashes, so any change fails CI.
`places.py` is part of the baseline fingerprint and of the rules hash: changing the gazetteer
changes both.

Any change after labelling is a **new version** (prompt `v3`, `rules-v2`, …). Don't edit a
frozen version. Score each new version on the test split, report it under its own version
name, and never let it overwrite a frozen version's result.

## Cases

- 30 hand-labelled notices, open and expired: **10 dev**, **20 test**. Fix the split and
  record it before any scoring.
- Use dev for reading errors and designing changes. Score test only, and never use it to tune.
- Label files hold no model output (no extraction values, flags, statuses or baseline
  results). Each carries the redacted notice text as a YAML comment, for the labeller and for
  `check_labels`; the labels themselves are gate values, criteria_location, unsure and notes.
- The expected status is not labelled: the harness derives it from the gate labels with
  eligibility rules v3. How to label: `docs/label-guide.md`. Check:
  `python backend/manage.py check_labels`.
- Rulings made during labelling (situation → label → example case) are in the table under
  "Rulings applied during labelling" in `docs/label-guide.md`.

### Selection (recorded 2026-11-16)

- Script `eval/select_cases.py`, seed **20261116**, manifest `eval/cases/manifest.json`
  (references, split, the strata each case belongs to and the quota it filled, each notice's
  `content_hash`).
- Pool: kept Tender rows (any status) first seen before 2026-11-17T00:00:00+00:00, with a
  redacted description of 120+ words and at most 30000 characters (so nothing is truncated),
  minus the exclusions: 173 notices, fingerprint `0c9b3cf1…d01edb3` (full value in the
  manifest).
- Excluded: `cb-8448-42897985` (outputs already seen). Prompt v2's worked examples are
  invented, so no notice is excluded for them.
- Strata from metadata and keyword matches on the title and description only, never model
  output. Minimums: against_sa 4, sa_or_so_request 3, NL region scope 8, national 8, text
  mentions security/clearance/reliability 5, text mentions land claims 3, Open 10, Expired 10.
  The split is the seeded search that keeps every stratum on both sides, closest to 1/3 dev.
- Re-running the script with the same pool reproduces the manifest; if the pool changed it
  writes nothing and says so. It never overwrites an existing label file.

## Metrics (for the LLM and the baseline, on the same cases)

1. **Per gate:** for each of the six gates (clearance split into corporate and personnel),
   count matches with the label, including "unknown", and report the unknown rate.
2. **Can-bid decision:** Assessment status (blocked / check_solicitation /
   no_blockers_stated) against the decision derived from the gate labels with rules v3, as a
   count table. Record the eligibility `RULES_VERSION` with each result.
3. **Grounding:** count `ungrounded` and `ungrounded_detail` flags, and how many gates fell
   back to unknown because of them.
4. **Redaction audit count:** contact-info leaks the independent redaction audit finds in the
   30 cases. Target 0.

## Harness

`python backend/manage.py run_eval --split dev [--repeats N] [--prompts v2,v3]
[--report-tag TAG]` writes `eval/reports/<split>[-<tag>]-<date>.md` and `.json` (same data).
Each pinned prompt (default: all, `v2,v3`) is scored as its own system (`llm_v2`, `llm_v3`)
with the same model settings, next to the keyword baseline, all compared after
post-validation.

- The site's prompt (v2) uses the cached site extraction (made through the normal path and
  caps if missing), as before.
- Any other prompt (v3) gets an eval-only primary run: an `EvalExtraction` with repeat 0,
  never an `Extraction` (the site shows the newest extraction per model, so a v3
  `Extraction` would replace v2 on the site). A later run reuses the first ok repeat-0 row
  for the same config and notice text instead of re-rolling it.
- `--repeats N` adds N uncached runs per case and prompt, stored as `EvalExtraction` rows
  tagged `<run tag>-<prompt>` (LlmCall purpose `eval`).

The report compares all systems: per-field table, accuracy across all runs (mean and
range), error types, status confusion table, the primary-run answers that changed between v2
and v3, consistency and grounding. `python backend/manage.py render_eval <report.json>
[--compare <other.json>]` re-scores a stored run and rewrites its report in place without any
API call; `--compare` adds a side-by-side view (e.g. dev next to test).
Scoring: `tenders/evals/scoring.py`.

## Prompt v3 changes

Made 2026-11-18 from the **dev split only**: the 10 dev labels, the dev report
`eval/reports/dev-2026-11-17.md`, and the raw v2 outputs for the dev cases (cached extraction
plus the 3 repeats of run `eval-dev-20261117T200253Z`, before and after grounding). No test
label, test extraction or test run was opened.

A change was allowed only if it traces to (a) a cause found in that dev diagnosis or (b) one of
the two clarifications in the label guide's "Fields" section, which were written before test
labelling. The "Rulings applied during labelling" table was **not** used: some rulings came
from test notices. Model, effort, max_tokens, schema, the grounding check (`validate.py`) and
the two invented worked examples are unchanged from v2.

Causes: **(a)** the v2 definition was misread or left the case open; **(b)** a valid answer
was dropped by grounding because the quote was missing, inexact, joined or not from the notice.

| # | v3 change | Cause | Provenance |
|---|---|---|---|
| 1 | Every value other than `unknown`, **including `no` and `none`**, needs evidence. | (b) | dev-04 SA: correct `no` with no evidence in 4/4 runs, reset to unknown. dev-07 docs: same, 3/4 runs. dev-01 SA (2 repeats), dev-10 SA (1 repeat). |
| 2 | Copy quotes and the SA name, place and portal **character for character**, including symbols such as `#`, `(SA)`, `/`. | (b) | dev-03: SA name written "PWGSC Supply Arrangement E60PQ-140003/D", dropping "(SA) #" (cached, rep 3). dev-10: "Supply and delivery" is not in the notice (cached). |
| 3 | Prefer a **whole metadata value or a short exact phrase**; never **join** text from different places. | (b) | dev-07: place joined from the title and an address line (cached). dev-06: a mandatory requirement joined across a bullet break (rep 1). dev-08, dev-02: partial trade_agreements lists with the field name (cached, rep 2, rep 3). |
| 4 | Quote only the notice, **never the instructions or examples**. | (b) | dev-02: SA `yes` quoted "RFP against Supply Arrangement", which is the prompt's own example; the notice_type is "Request for Proposal". Reset to unknown, so a missed block (cached, rep 3). |
| 5 | SA `yes` also when the text says **only pre-qualified suppliers or SA holders may bid**. | (a) | dev-02 (label evidence "Only suppliers who are pre-qualified and have been issued a supply arrangement are eligible to bid"). |
| 6 | SA `no` for a request to **become** a holder (Request for Supply Arrangement, Request for Standing Offer), with the whole notice_type value as evidence and `name` null. | guide clarification; (a), (b) | Label guide "Fields" clarification. dev-01, dev-04, dev-10: label `no`; v2 said unknown or `no` without evidence. **Removed:** the first v3 draft also listed "an Invitation to Qualify that invites suppliers to become pre-qualified". That wording traced to a test case (test-05), not to dev or the guide (whose examples are Request for Supply Arrangement and Request for Standing Offer only), so it was taken out and v3 re-pinned before any test scoring. No dev notice is an ITQ. |
| 7 | Any other notice_type (such as "Request for Proposal") says nothing about SA on its own: `unknown` unless the text says who may bid. | (a) | dev-08: `no` quoting "Request for Proposal" (cached, rep 2, rep 3; label unknown, overclaimed). dev-05 rep 3 the same; dev-05/06/07 repeats gave `no` without evidence. |
| 8 | Origin: a trade_agreements value alone does not say who may bid: `unknown` unless a sentence does. | (a) | dev-09: `canadian_and_trading_partners` from "WTO GPA" (cached; label unknown, overclaimed). dev-05 rep 1, dev-07 rep 3 the same; dev-08 cached and rep 3 only caught by grounding. |
| 9 | Land claims `no` for "excludes locations in areas subject to Comprehensive Land Claims Agreements"; a trade_agreements value naming none, or silence, is `unknown`. | guide clarification; (a) | First part: the guide clarification (no dev error). Second part: dev-02 rep 2 and dev-05 rep 1 gave `no` from a trade_agreements list or with no evidence (label unknown; only caught by grounding). |
| 10 | Documents `no` when the notice says the documents reach bidders another way, such as being **sent directly to the invited suppliers**. | (a) | dev-02: label `no` ("The RFQ document will be sent directly to the eligible SLSA SA Holder…"); v2 said unknown in 4/4 runs. |

Not changed, on purpose:

- **criteria_location** (dev-05, dev-06: label `partly_stated`; v2 `see_solicitation`, dev-05
  flips across all three values). The notices list "Essential Outcomes" a solution must meet.
  Neither v2 nor the label guide says whether such outcomes count as evaluation criteria, and
  the cases are not marked unsure. A fix would mean writing a new labelling rule from two dev
  labels, so it is left for a decision.
- **Unsure judgement calls (c):** dev-09 SA `yes` and documents `no` (ITQ results notice),
  dev-10 on-site `yes` (rests on a CLCA footnote). No change was made for them.
- **dev-02 on-site** (label `no`; 2 of 4 runs said unknown with no quote): no cause found
  beyond run-to-run noise.

Dev result: `eval/reports/dev-v3-2026-11-18.md`. With n = 10 the dev numbers can't rank v2
against v3; the comparison that counts is the test split.

The report is from run `eval-dev-20261118T050705Z` (v3 re-pinned as `12c49a09…`). The
first v3 draft (`accc6f93…`, with the ITQ wording) was run once on dev as
`eval-dev-20261118T045657Z`: 74/80 fields against 71/80 now. Its report was overwritten; its
EvalExtraction rows are kept. The gap is run-to-run noise in quoting, not the wording change:
in both runs v3 gave the same values on dev-02 and dev-03, and the new run lost them only
because their quotes failed grounding (below). No dev notice is an ITQ.

Seen on dev with v3 (recorded, not acted on; any fix would be a v4):

- Quoting still drops correct answers. dev-02 primary: clearance `none` quoted as "None"
  (1 word), on-site `no` as "Supply and delivery" (not in the notice); dev-03 primary: on-site
  `yes` quoted with words skipped. In the first v3 run, dev-02's on-site quotes were 2 words
  ("Requirement Description", "Adobe Software"). The "short exact phrase" rule (change 3)
  seems to push quotes below the 3-word minimum.
- Change 4 is not complete: dev-02 primary and rep 2 still quoted the prompt's "RFP against
  Supply Arrangement", so its SA `yes` was reset and the block missed.
- Change 7 mostly holds: 1 of 6 dev-05/dev-08 repeats answered SA `no` quoting "Request for
  Proposal" (4 of 6 in the first v3 run).

## Test split scored once

Test split scored once on 2026-11-18, at commit `3cce2a90ec24cbec2948dafe363b7ef8d0326cff`.
No prompt, baseline, rules or label changes after this point are evaluated on these 20 cases.

- Run `eval-test-20261118T051712Z`: `run_eval --split test --prompts v2,v3 --repeats 3
  --report-tag final`, `LLM_MAX_CALLS_PER_RUN=150` for that command only. Checks before the
  run: clean working tree, v2/v3/baseline pins matched `test_frozen.py`, `check_labels`
  30/30.
- Report: `eval/reports/test-final-2026-11-18.md` and `.json`.
- The call cap stopped the repeats 3 calls short: 13 test notices had no cached v2 site
  extraction, so 13 extract calls came out of the 150. Missing: repeat 3 of test-19 (v3) and
  test-20 (v2 and v3). Every case has its scored run from all three systems; those two cases
  have 3 runs instead of 4 in the consistency and mean/range tables. The run was not repeated
  or topped up.
- The report was then re-rendered from the stored run JSON (`render_eval`, no API call) to
  add the across-runs mean and range and the dev/test side-by-side view requested for this
  report. Every score from the original rendering is unchanged (checked field by field).

## Prompt-injection eval (2026-11-18)

Separate from the 30 labelled cases and never mixed into their scores. Five **invented**
attack notices, each with a clean twin that differs only by the attack text
(`eval/injection/`, see its README): an instruction override, a fake `SYSTEM:` line, a tag
break-out, an attack in the buyer name, and a polite request to "AI assistants". Run on the
site prompt (v3) with the frozen model settings:

`python backend/manage.py run_injection_eval --repeats 3` (4 uncached runs per notice, LlmCall
purpose `eval_injection`; no Tender, Extraction or EvalExtraction row is written). Report:
`eval/reports/injection-<date>.md` and `.json`. `--rescore <json>` re-scores a stored run
with no API call.

Code-side pattern detector (`tenders/ingest/injection.py`): a fixed rule set that runs at
ingest, before any LLM call, and adds `injection_pattern` to the notice's `data_quality`. It
changes no gate or status. The detail page shows a neutral caution when a notice has
`injection_pattern` or its extraction has `injection_suspected`. False positives are counted
on a seeded sample of 50 stored notices (seed 20261118) and on all stored notices. Counts,
rules and references only; no text.

First run: `eval/reports/injection-2026-11-18.md`. Nothing frozen was changed. A fix for an
attack that works would be a new prompt version (v4).

## Prompt v5 and rules v4 (2026-11-22, site only, not scored)

| Component | Version | Pinned hash (sha256) |
|---|---|---|
| LLM, notices read with documents (schema `2`) | prompt `v5` | system prompt `69ac8cb9…e19816c9e` |
| Eligibility rules | `RULES_VERSION` `4` | source of `rules.py` + `places.py` + `unread.py` `29d78d32…b026f381` |

- Prompt v5 is v4 with the documents in a new order (amendments, other documents, main
  solicitation last, so the 120k cap cuts the main document instead of dropping
  amendments). Two sentences change (`prompt._V5_CHANGES`). Like v4 it only reads notices
  with stored documents, so the notice-only eval can't score it.
- Rules v4 = rules v3, unchanged, plus the unread-text tripwire (`eligibility/unread.py`):
  clearance or work location not stated, and stored document text the model did not read
  names a clearance term or on-site work, makes no_blockers_stated check_solicitation, with
  a warning. Never a blocker. The site assesses with v4; `run_eval` keeps scoring with v3
  (`FROZEN_VERSIONS`).
- Provenance: both came from a read-only review of the 16 open IT notices with documents on
  2026-11-22. Two of them are labelled cases: **test-11** (cb-435-94227930) and dev-04
  (cb-181-76348100). So v4 is not blind on test-11, and per "Test split scored once" it is
  not evaluated on the test split.
- Offline re-score from the stored outputs of `test-final-2026-11-18` (177 outputs) and
  `dev-v3-2026-11-18` (90), no API call: every status is the same under v4 as under v3. The
  local database has no stored documents for any of the 30 cases, so the tripwire has no text
  to read there. With the documents prod has for test-11 (rebuilt from public sources), its
  label's expected status would become check_solicitation (clearance unknown in the label;
  the RFP and amendments say there is a security requirement). Its 9 outputs were already
  check_solicitation.

## Reviewed pool (candidate labels from human review)

A separate pool, never mixed into dev or test. In the admin review queue a person can
accept, correct or reject each gate of an extraction. `python backend/manage.py
export_review_labels` writes each extraction with a correction as a candidate label file in
`eval/cases/reviewed/`: same comment block and fields as the dev/test files, and readable by
the same checker. It is not in `manifest.json`, so `run_eval` never scores it and
`check_labels` lists its files as ignored. A notice that is a dev or test case is never
exported.

Only corrections become label values (a correction passed the same schema and grounding
checks as model output). Label files hold no model output, so accepted, rejected and
unreviewed gates are written as `TODO`, with a comment naming the decision. Before any of
these become eval data: label the TODOs independently, then fix and record a split in a new
manifest, as for dev/test.
