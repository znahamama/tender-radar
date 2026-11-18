# Eval report: test split, 2026-11-18

## Protocol

- **Versions:** LLM `claude-haiku-5-5` (effort low, max_tokens 2000) with prompts `v2` (`def2a6603f1c…`, the site’s prompt), `v3` (`12c49a099a67…`); schema `1` (`4fab443b3ee8…`); keyword baseline `rules-v1` (`04590c85d129…`); eligibility rules v3 (`69f130cd4bb6…`). Full hashes in the JSON.
- **Cases:** seeded selection (seed 20261116, 2026-11-16), **n = 20 test** cases. Labels: hand-labelled blind (no model output seen), checked by check_labels. 11 marked unsure (test-01, test-05, test-08, test-10, test-11, test-12, test-14, test-15, test-17, test-19, test-20).
- **Status:** expected status derived from the gate labels with rules v3, the tender's notice_kind and sa_required_from_metadata, for profile “Harbourline Software (fictional)” (SAs: none; clearance none/none).
- **Compared after post-validation** (grounding), i.e. what the site shows. The SA field is the extracted value; the metadata override only enters the status.
- **Primary run per LLM system** (scored below): LLM v2: the cached site extraction; LLM v3: an eval-only run (EvalExtraction repeat 0, never shown on the site).
- **Run:** `eval-test-20261118T051712Z`, 3 repeat(s) per case and LLM system; commit `3cce2a90ec`.

> **n is tiny.** With 20 cases one case is 5 points; the 95% Wilson intervals below are wide and mostly overlap. Use this split to read errors, not to rank systems.

> **Repeats stopped early:** repeat 3, test-19, prompt v3: per-run call cap reached (150 calls). Cases with fewer runs are listed under Consistency; every case has its scored (primary) run.

> **Re-rendered** from the stored run data on 2026-11-18 with report code at commit `3cce2a90ec` (uncommitted changes): no API call, no new extraction, the scored values are those of the run above.

## Summary: correct per field

| Field | LLM v2 | 95% CI | LLM v3 | 95% CI | Baseline | 95% CI | LLM v2 excl. unsure (n=9) | LLM v3 excl. unsure (n=9) | Baseline excl. unsure (n=9) |
|---|---|---|---|---|---|---|---|---|---|
| criteria_location | 17/20 | 64–95% | 18/20 | 70–97% | 17/20 | 64–95% | 7/9 | 8/9 | 7/9 |
| supply_arrangement_required | 10/20 | 30–70% | 12/20 | 39–78% | 13/20 | 43–82% | 5/9 | 8/9 | 9/9 |
| supplier_origin_restriction | 17/20 | 64–95% | 19/20 | 76–99% | 10/20 | 30–70% | 7/9 | 8/9 | 3/9 |
| clearance.corporate | 20/20 | 84–100% | 20/20 | 84–100% | 20/20 | 84–100% | 9/9 | 9/9 | 9/9 |
| clearance.personnel | 19/20 | 76–99% | 19/20 | 76–99% | 19/20 | 76–99% | 9/9 | 9/9 | 9/9 |
| work_location.on_site | 17/20 | 64–95% | 15/20 | 53–89% | 12/20 | 39–78% | 9/9 | 8/9 | 7/9 |
| land_claims_applies | 15/20 | 53–89% | 18/20 | 70–97% | 18/20 | 70–97% | 6/9 | 9/9 | 8/9 |
| documents_behind_login | 17/20 | 64–95% | 20/20 | 84–100% | 14/20 | 48–85% | 8/9 | 9/9 | 6/9 |
| **All fields** | **132/160** (82%) |  | **141/160** (88%) |  | **123/160** (77%) |  | **60/72** (83%) | **68/72** (94%) | **58/72** (81%) |

_Pooled “all fields” counts are not independent (8 fields × the same cases)._

## Across all runs (scored run, mean and range)

Scored = the primary run (the one every other table uses), as correct/n with its 95% Wilson interval. Mean (min–max) = accuracy over every run of the system (primary + repeats), each run over the cases it covers. The baseline is deterministic (one run). Cases per run: LLM v2: primary 20, repeat 1 20, repeat 2 20, repeat 3 19; LLM v3: primary 20, repeat 1 20, repeat 2 20, repeat 3 18.

| Field | LLM v2 scored | 95% CI | LLM v2 mean (min–max) | LLM v3 scored | 95% CI | LLM v3 mean (min–max) | Baseline scored | 95% CI |
|---|---|---|---|---|---|---|---|---|
| criteria_location | 17/20 | 64–95% | 82% (79%–85%) | 18/20 | 70–97% | 86% (83%–90%) | 17/20 | 64–95% |
| supply_arrangement_required | 10/20 | 30–70% | 42% (32%–50%) | 12/20 | 39–78% | 63% (60%–70%) | 13/20 | 43–82% |
| supplier_origin_restriction | 17/20 | 64–95% | 84% (75%–89%) | 19/20 | 76–99% | 93% (89%–95%) | 10/20 | 30–70% |
| clearance.corporate | 20/20 | 84–100% | 100% (100%–100%) | 20/20 | 84–100% | 100% (100%–100%) | 20/20 | 84–100% |
| clearance.personnel | 19/20 | 76–99% | 99% (95%–100%) | 19/20 | 76–99% | 99% (95%–100%) | 19/20 | 76–99% |
| work_location.on_site | 17/20 | 64–95% | 72% (63%–85%) | 15/20 | 53–89% | 68% (65%–75%) | 12/20 | 39–78% |
| land_claims_applies | 15/20 | 53–89% | 85% (75%–90%) | 18/20 | 70–97% | 90% (89%–90%) | 18/20 | 70–97% |
| documents_behind_login | 17/20 | 64–95% | 90% (85%–95%) | 20/20 | 84–100% | 98% (95%–100%) | 14/20 | 48–85% |
| **All fields** | 132/160 |  | 82% (81%–82%) | 141/160 |  | 87% (86%–88%) | 123/160 |  |
| **Status** | 18/20 | 70–97% | 87% (84%–90%) | 18/20 | 70–97% | 88% (83%–90%) | 17/20 | 64–95% |

## Dev (n=10) and test (n=20) side by side

Scored runs only. dev: run `eval-dev-20261118T050705Z`; test: run `eval-test-20261118T051712Z`. Dev was used to design prompt v3; test was not.

| Field | LLM v2 dev | LLM v2 test | LLM v3 dev | LLM v3 test | Baseline dev | Baseline test |
|---|---|---|---|---|---|---|
| criteria_location | 8/10 | 17/20 | 8/10 | 18/20 | 8/10 | 17/20 |
| supply_arrangement_required | 4/10 | 10/20 | 9/10 | 12/20 | 9/10 | 13/20 |
| supplier_origin_restriction | 9/10 | 17/20 | 10/10 | 19/20 | 6/10 | 10/20 |
| clearance.corporate | 10/10 | 20/20 | 9/10 | 20/20 | 9/10 | 20/20 |
| clearance.personnel | 10/10 | 19/20 | 9/10 | 19/20 | 9/10 | 19/20 |
| work_location.on_site | 9/10 | 17/20 | 7/10 | 15/20 | 6/10 | 12/20 |
| land_claims_applies | 10/10 | 15/20 | 10/10 | 18/20 | 10/10 | 18/20 |
| documents_behind_login | 7/10 | 17/20 | 9/10 | 20/20 | 6/10 | 14/20 |
| **All fields** | **67/80** (84%) | **132/160** (82%) | **71/80** (89%) | **141/160** (88%) | **63/80** (79%) | **123/160** (77%) |
| _wrong (total)_ | 2 | 5 | 3 | 4 | 5 | 8 |
| _missed (total)_ | 9 | 15 | 6 | 12 | 8 | 17 |
| _overclaimed (total)_ | 2 | 8 | 0 | 3 | 4 | 12 |
| **Status agrees** | 6/10 | 18/20 | 7/10 | 18/20 | 6/10 | 17/20 |

## Error types

Missed: the label is known, the system said unknown. Overclaimed: the label is unknown, the system gave a value (the worst kind: it states something the notice doesn't say). Criteria_location has no unknown value, so it is only correct or wrong (the baseline always says see_solicitation).

**LLM v2**

| Field | correct | wrong | missed | overclaimed | system unknown |
|---|---|---|---|---|---|
| criteria_location | 17 | 3 | 0 | 0 | 0/20 |
| supply_arrangement_required | 10 | 0 | 9 | 1 | 12/20 |
| supplier_origin_restriction | 17 | 0 | 0 | 3 | 12/20 |
| clearance.corporate | 20 | 0 | 0 | 0 | 18/20 |
| clearance.personnel | 19 | 0 | 0 | 1 | 18/20 |
| work_location.on_site | 17 | 0 | 3 | 0 | 9/20 |
| land_claims_applies | 15 | 2 | 1 | 2 | 16/20 |
| documents_behind_login | 17 | 0 | 2 | 1 | 18/20 |
| **total** | **132** | **5** | **15** | **8** | |

**LLM v3**

| Field | correct | wrong | missed | overclaimed | system unknown |
|---|---|---|---|---|---|
| criteria_location | 18 | 2 | 0 | 0 | 0/20 |
| supply_arrangement_required | 12 | 1 | 6 | 1 | 9/20 |
| supplier_origin_restriction | 19 | 0 | 1 | 0 | 16/20 |
| clearance.corporate | 20 | 0 | 0 | 0 | 18/20 |
| clearance.personnel | 19 | 0 | 0 | 1 | 18/20 |
| work_location.on_site | 15 | 0 | 4 | 1 | 9/20 |
| land_claims_applies | 18 | 1 | 1 | 0 | 18/20 |
| documents_behind_login | 20 | 0 | 0 | 0 | 17/20 |
| **total** | **141** | **4** | **12** | **3** | |

**Baseline**

| Field | correct | wrong | missed | overclaimed | system unknown |
|---|---|---|---|---|---|
| criteria_location | 17 | 3 | 0 | 0 | 0/20 |
| supply_arrangement_required | 13 | 0 | 7 | 0 | 11/20 |
| supplier_origin_restriction | 10 | 2 | 0 | 8 | 7/20 |
| clearance.corporate | 20 | 0 | 0 | 0 | 18/20 |
| clearance.personnel | 19 | 0 | 1 | 0 | 20/20 |
| work_location.on_site | 12 | 0 | 7 | 1 | 12/20 |
| land_claims_applies | 18 | 2 | 0 | 0 | 17/20 |
| documents_behind_login | 14 | 1 | 2 | 3 | 16/20 |
| **total** | **123** | **8** | **17** | **12** | |

Totals side by side:

| System | correct | wrong | missed | overclaimed |
|---|---|---|---|---|
| LLM v2 | 132 | 5 | 15 | 8 |
| LLM v3 | 141 | 4 | 12 | 3 |
| Baseline | 123 | 8 | 17 | 12 |

## Status (can-bid decision)

Rows: expected (from the labels). Columns: the system's status.

**LLM v2**: agrees on 18/20 (95% CI 70–97%); excluding unsure 8/9.

| expected \ got | blocked | check_sol. | no_blockers |
|---|---|---|---|
| blocked | 5 | 0 | 0 |
| check_sol. | 0 | 13 | 0 |
| no_blockers | 0 | 2 | 0 |

- False blocks (system blocked, labels don't): none
- Missed blocks (labels block, system didn't): none

**LLM v3**: agrees on 18/20 (95% CI 70–97%); excluding unsure 8/9.

| expected \ got | blocked | check_sol. | no_blockers |
|---|---|---|---|
| blocked | 5 | 0 | 0 |
| check_sol. | 0 | 13 | 0 |
| no_blockers | 0 | 2 | 0 |

- False blocks (system blocked, labels don't): none
- Missed blocks (labels block, system didn't): none

**Baseline**: agrees on 17/20 (95% CI 64–95%); excluding unsure 7/9.

| expected \ got | blocked | check_sol. | no_blockers |
|---|---|---|---|
| blocked | 4 | 1 | 0 |
| check_sol. | 0 | 13 | 0 |
| no_blockers | 0 | 2 | 0 |

- False blocks (system blocked, labels don't): none
- Missed blocks (labels block, system didn't): test-16

## LLM v3 vs LLM v2: primary-run answers that changed

Fields (gates and criteria_location): **15 fixed**, **6 broken**, 1 changed but still not correct. ⚠ = labeller marked the case unsure.

| Case | Field | Label | LLM v2 | LLM v3 | Change |
|---|---|---|---|---|---|
| test-01 ⚠ | documents_behind_login | `no` | `unknown` (missed) | `no` (correct) | fixed |
| test-02 | supply_arrangement_required | `no` | `unknown` (missed) | `no` (correct) | fixed |
| test-03 | supply_arrangement_required | `no` | `unknown` (missed) | `no` (correct) | fixed |
| test-03 | supplier_origin_restriction | `none` | `none` (correct) | `unknown` (missed) | broken |
| test-04 | land_claims_applies | `unknown` | `no` (overclaimed) | `unknown` (correct) | fixed |
| test-06 | land_claims_applies | `unknown` | `no` (overclaimed) | `unknown` (correct) | fixed |
| test-07 | supply_arrangement_required | `no` | `unknown` (missed) | `no` (correct) | fixed |
| test-07 | land_claims_applies | `no` | `yes` (wrong) | `no` (correct) | fixed |
| test-07 | documents_behind_login | `unknown` | `yes` (overclaimed) | `unknown` (correct) | fixed |
| test-08 ⚠ | supply_arrangement_required | `unknown` | `no` (overclaimed) | `unknown` (correct) | fixed |
| test-09 | supply_arrangement_required | `no` | `unknown` (missed) | `no` (correct) | fixed |
| test-11 ⚠ | work_location.on_site | `yes` | `yes` (correct) | `unknown` (missed) | broken |
| test-13 | supplier_origin_restriction | `unknown` | `canadian_and_trading_partners` (overclaimed) | `unknown` (correct) | fixed |
| test-13 | work_location.on_site | `unknown` | `unknown` (correct) | `no` (overclaimed) | broken |
| test-14 ⚠ | supply_arrangement_required | `no` | `no` (correct) | `unknown` (missed) | broken |
| test-16 | supply_arrangement_required | `unknown` | `unknown` (correct) | `no` (overclaimed) | broken |
| test-17 ⚠ | supply_arrangement_required | `no` | `unknown` (missed) | `yes` (wrong) | other |
| test-17 ⚠ | supplier_origin_restriction | `unknown` | `canadian_and_trading_partners` (overclaimed) | `unknown` (correct) | fixed |
| test-18 | criteria_location | `partly_stated` | `stated_in_notice` (wrong) | `partly_stated` (correct) | fixed |
| test-18 | supplier_origin_restriction | `unknown` | `canadian_and_trading_partners` (overclaimed) | `unknown` (correct) | fixed |
| test-19 ⚠ | supply_arrangement_required | `no` | `no` (correct) | `unknown` (missed) | broken |
| test-20 ⚠ | documents_behind_login | `no` | `unknown` (missed) | `no` (correct) | fixed |

## Consistency (LLM, run to run)

Runs per case and system: the primary run + the repeats = 3/4. Cases with the same answer in every run:

| Field | LLM v2 stable | LLM v3 stable | LLM v2 flips | LLM v3 flips |
|---|---|---|---|---|
| criteria_location | 18/20 | 18/20 | test-14, test-18 | test-14, test-18 |
| supply_arrangement_required | 14/20 | 16/20 | test-02, test-05, test-13, test-14, test-16, test-19 | test-08, test-10, test-16, test-17 |
| supplier_origin_restriction | 15/20 | 18/20 | test-02, test-07, test-13, test-18, test-19 | test-02, test-03 |
| clearance.corporate | 20/20 | 20/20 | - | - |
| clearance.personnel | 19/20 | 19/20 | test-20 | test-20 |
| work_location.on_site | 13/20 | 17/20 | test-02, test-03, test-09, test-11, test-14, test-18, test-20 | test-02, test-09, test-18 |
| land_claims_applies | 16/20 | 19/20 | test-04, test-05, test-06, test-07 | test-17 |
| documents_behind_login | 18/20 | 19/20 | test-07, test-20 | test-20 |
| status | 19/20 | 19/20 | test-14 | test-14 |

LLM v2 flips (label, then each run: primary first):

- test-14 criteria_location: label `see_solicitation`; runs `see_solicitation`, `partly_stated`, `see_solicitation`, `partly_stated`
- test-18 criteria_location: label `partly_stated`; runs `stated_in_notice`, `stated_in_notice`, `see_solicitation`, `stated_in_notice`
- test-02 supply_arrangement_required: label `no`; runs `unknown`, `no`, `no`, `no`
- test-05 supply_arrangement_required: label `no`; runs `unknown`, `no`, `unknown`, `unknown`
- test-13 supply_arrangement_required: label `no`; runs `no`, `no`, `unknown`, `unknown`
- test-14 supply_arrangement_required: label `no`; runs `no`, `unknown`, `unknown`, `unknown`
- test-16 supply_arrangement_required: label `unknown`; runs `unknown`, `unknown`, `no`, `no`
- test-19 supply_arrangement_required: label `no`; runs `no`, `unknown`, `unknown`, `unknown`
- test-02 supplier_origin_restriction: label `none`; runs `none`, `canadian_and_trading_partners`, `none`, `none`
- test-07 supplier_origin_restriction: label `unknown`; runs `unknown`, `canadian_and_trading_partners`, `unknown`, `unknown`
- test-13 supplier_origin_restriction: label `unknown`; runs `canadian_and_trading_partners`, `unknown`, `canadian_and_trading_partners`, `unknown`
- test-18 supplier_origin_restriction: label `unknown`; runs `canadian_and_trading_partners`, `canadian_and_trading_partners`, `canadian_and_trading_partners`, `unknown`
- test-19 supplier_origin_restriction: label `unknown`; runs `unknown`, `canadian_and_trading_partners`, `unknown`, `canadian_and_trading_partners`
- test-20 clearance.personnel: label `unknown`; runs `reliability`, `unknown`, `unknown`
- test-02 work_location.on_site: label `no`; runs `no`, `unknown`, `no`, `unknown`
- test-03 work_location.on_site: label `no`; runs `no`, `no`, `unknown`, `unknown`
- test-09 work_location.on_site: label `no`; runs `no`, `unknown`, `no`, `unknown`
- test-11 work_location.on_site: label `yes`; runs `yes`, `unknown`, `unknown`, `unknown`
- test-14 work_location.on_site: label `yes`; runs `yes`, `yes`, `yes`, `unknown`
- test-18 work_location.on_site: label `no`; runs `no`, `unknown`, `unknown`, `no`
- test-20 work_location.on_site: label `yes`; runs `unknown`, `yes`, `unknown`
- test-04 land_claims_applies: label `unknown`; runs `no`, `unknown`, `unknown`, `unknown`
- test-05 land_claims_applies: label `yes`; runs `unknown`, `yes`, `unknown`, `yes`
- test-06 land_claims_applies: label `unknown`; runs `no`, `unknown`, `unknown`, `unknown`
- test-07 land_claims_applies: label `no`; runs `yes`, `unknown`, `unknown`, `unknown`
- test-07 documents_behind_login: label `unknown`; runs `yes`, `yes`, `unknown`, `unknown`
- test-20 documents_behind_login: label `no`; runs `unknown`, `unknown`, `no`
- test-14 status: label `check_solicitation`; runs `check_solicitation`, `no_blockers_stated`, `check_solicitation`, `no_blockers_stated`

LLM v3 flips (label, then each run: primary first):

- test-14 criteria_location: label `see_solicitation`; runs `see_solicitation`, `see_solicitation`, `see_solicitation`, `partly_stated`
- test-18 criteria_location: label `partly_stated`; runs `partly_stated`, `see_solicitation`, `stated_in_notice`, `partly_stated`
- test-08 supply_arrangement_required: label `unknown`; runs `unknown`, `unknown`, `unknown`, `no`
- test-10 supply_arrangement_required: label `no`; runs `unknown`, `no`, `unknown`, `unknown`
- test-16 supply_arrangement_required: label `unknown`; runs `no`, `unknown`, `no`, `unknown`
- test-17 supply_arrangement_required: label `no`; runs `yes`, `yes`, `unknown`, `unknown`
- test-02 supplier_origin_restriction: label `none`; runs `none`, `none`, `none`, `unknown`
- test-03 supplier_origin_restriction: label `none`; runs `unknown`, `unknown`, `canadian_and_trading_partners`, `unknown`
- test-20 clearance.personnel: label `unknown`; runs `reliability`, `unknown`, `unknown`
- test-02 work_location.on_site: label `no`; runs `no`, `no`, `unknown`, `no`
- test-09 work_location.on_site: label `no`; runs `no`, `unknown`, `no`, `unknown`
- test-18 work_location.on_site: label `no`; runs `no`, `unknown`, `unknown`, `unknown`
- test-17 land_claims_applies: label `yes`; runs `no`, `no`, `no`, `unknown`
- test-20 documents_behind_login: label `no`; runs `no`, `unknown`, `unknown`
- test-14 status: label `check_solicitation`; runs `check_solicitation`, `check_solicitation`, `check_solicitation`, `no_blockers_stated`

## Grounding

Primary run of each system.

| System | gates reset to unknown (ungrounded evidence) | details dropped | failed extractions |
|---|---|---|---|
| LLM v2 | 8 | 0 | 0 |
| LLM v3 | 0 | 1 | 0 |
| Baseline | 0 | 0 | 0 |

Gates reset to unknown across all repeats: LLM v2 37; LLM v3 6.

## Cost, tokens and latency (API calls made by this run)

| Purpose, prompt | calls (ok) | tokens in | tokens out | cost USD | latency median / mean / max |
|---|---|---|---|---|---|
| eval v2 | 59 (59) | 438,222 | 40,603 | $0.064151 | 3.2 / 3.3 / 4.3 s |
| eval v3 | 78 (78) | 614,906 | 52,805 | $0.087926 | 3.2 / 3.3 / 4.4 s |
| extract v2 | 13 (13) | 99,629 | 9,514 | $0.014725 | 3.8 / 3.5 / 4.3 s |

Primary runs already stored before this run (not counted here): LLM v2 7/20, LLM v3 0/20. `extract`: a site extraction made now where missing. `eval`: eval-only primary runs and the repeats. The baseline makes no API call and costs nothing.

## Per-case disagreements

Every field where any system differs from the label, with the evidence quotes. ⚠ = labeller marked the case unsure.

### test-01 · `cb-808-71346390` · against_sa ⚠ unsure

> Labeller's note: PSIB set-aside limits bidding to Indigenous businesses, which fits no origin value; left unknown rather than inferring canadian_only

Status: expected **blocked**, LLM v2 **blocked**, LLM v3 **blocked**, Baseline **blocked**.

- **clearance.personnel**: label `secret` · LLM v2 `secret` (correct) · LLM v3 `secret` (correct) · Baseline `unknown` (missed)
  - label: “Facility Security Clearance (FSC) at the level of SECRET Minimum Resource Security Required: Personnel security screening at the level of SECRET”
  - LLM v2: “Minimum Corporate Security Required: Facility Security Clearance (FSC) at the level of SECRET”
  - LLM v3: “Minimum Resource Security Required: Personnel security screening at the level of SECRET”
  - Baseline: “Minimum Corporate Security Required: Facility Security Clearance (FSC) at the level of SECRET”
- **documents_behind_login**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `no` (correct) · Baseline `unknown` (missed)
  - label: “RFP documents will be e-mailed directly by the Contracting Authority to the Qualified Supply Arrangement Holders”
  - LLM v2: _none_
  - LLM v3: “RFP documents will be e-mailed directly by the Contracting Authority to the Qualified Supply Arrangement Holders”
  - Baseline: _none_

### test-02 · `WS5120581462-Doc5145686894` · sa_or_so_request

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `no` (correct) · Baseline `no` (correct)
  - label: “Request for Supply Arrangement”
  - LLM v2: _none_ (its evidence was not found in the notice, so the gate was reset to unknown)
  - LLM v3: “Request for Supply Arrangement”
  - Baseline: “Request for Supply Arrangement”
- **supplier_origin_restriction**: label `none` · LLM v2 `none` (correct) · LLM v3 `none` (correct) · Baseline `canadian_and_trading_partners` (wrong)
  - label: “Canadian Suppliers, Suppliers of an Applicable Trading Partner and Suppliers of a Non-Trading Partner are eligible for this solicitation of offers”
  - LLM v2: “Barring any other conditions or international sanctions, Canadian Suppliers, Suppliers of an Applicable Trading Partner and Suppliers of a Non-Trading Partner are eligible for this solicitation of offers.”
  - LLM v3: “Canadian Suppliers, Suppliers of an Applicable Trading Partner and Suppliers of a Non-Trading Partner are eligible for this solicitation of offers.”
  - Baseline: “Eligibility for individual Requests for Proposals against the Supply Arrangement may be limited to Canadian Suppliers and Suppliers of an Applicable Trading Partner.”

### test-03 · `WS5467865775-Doc5532753609` · sa_or_so_request

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `no` (correct) · Baseline `no` (correct)
  - label: “Request for Standing Offer”
  - LLM v2: _none_
  - LLM v3: “Request for Standing Offer”
  - Baseline: “Request for Standing Offer”
- **supplier_origin_restriction**: label `none` · LLM v2 `none` (correct) · LLM v3 `unknown` (missed) · Baseline `canadian_and_trading_partners` (wrong)
  - label: “Canadian Suppliers, Suppliers of an Applicable Trading Partner and Suppliers of a Non-Trading Partner, are eligible for this solicitation of offers”
  - LLM v2: “Canadian Suppliers, Suppliers of an Applicable Trading Partner and Suppliers of a Non-Trading Partner, are eligible for this solicitation of offers”
  - LLM v3: _none_
  - Baseline: “World Trade Organization Agreement on Government Procurement (WTO GPA)”

### test-04 · `WS5967256651-Doc5967309031` · regular

Status: expected **blocked**, LLM v2 **blocked**, LLM v3 **blocked**, Baseline **blocked**.

- **supplier_origin_restriction**: label `unknown` · LLM v2 `unknown` (correct) · LLM v3 `unknown` (correct) · Baseline `canadian_and_trading_partners` (overclaimed)
  - label: _none_
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: “Canada-Korea Free Trade Agreement (CKFTA)”
- **land_claims_applies**: label `unknown` · LLM v2 `no` (overclaimed) · LLM v3 `unknown` (correct) · Baseline `unknown` (correct)
  - label: _none_
  - LLM v2: “trade_agreements: Canada-Korea Free Trade Agreement (CKFTA)”
  - LLM v3: _none_
  - Baseline: _none_
- **documents_behind_login**: label `unknown` · LLM v2 `unknown` (correct) · LLM v3 `unknown` (correct) · Baseline `yes` (overclaimed)
  - label: _none_
  - LLM v2: _none_
  - LLM v3: “Proposals must be submitted through SAP Ariba by the date and time specified in the RFP.”
  - Baseline: “General Information: Proposals must be submitted through SAP Ariba by the date and time specified in the RFP.”

### test-05 · `SSC-26-00033871:T` · regular ⚠ unsure

> Labeller's note: ITQ is a pre-qualification, not a supply arrangement or standing offer; labelled no by analogy to the request-to-become-a-holder clarification

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `unknown` (missed) · Baseline `unknown` (missed)
  - label: “failure to meet the requirements of this ITQ does not preclude a supplier from participating in the later RFP stage”
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: _none_
- **supplier_origin_restriction**: label `unknown` · LLM v2 `unknown` (correct) · LLM v3 `unknown` (correct) · Baseline `canadian_and_trading_partners` (overclaimed)
  - label: _none_
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: “Comprehensive and Progressive Agreement for Trans-Pacific Partnership (CPTPP)”
- **work_location.on_site**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `unknown` (missed) · Baseline `no` (correct)
  - label: “GD”
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: “GD”
- **land_claims_applies**: label `yes` · LLM v2 `unknown` (missed) · LLM v3 `unknown` (missed) · Baseline `yes` (correct)
  - label: “Comprehensive Land Claim Agreement (CLCA)”
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: “Comprehensive Land Claim Agreement (CLCA)”

### test-06 · `cb-0-11990877` · regular

Status: expected **no_blockers_stated**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **criteria_location**: label `partly_stated` · LLM v2 `see_solicitation` (wrong) · LLM v3 `see_solicitation` (wrong) · Baseline `see_solicitation` (wrong)
- **land_claims_applies**: label `unknown` · LLM v2 `no` (overclaimed) · LLM v3 `unknown` (correct) · Baseline `unknown` (correct)
  - label: _none_
  - LLM v2: “trade_agreements: Canadian Free Trade Agreement (CFTA)”
  - LLM v3: _none_
  - Baseline: _none_

### test-07 · `cb-364-13128756` · sa_or_so_request

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `no` (correct) · Baseline `no` (correct)
  - label: “Request for Supply Arrangement”
  - LLM v2: _none_ (its evidence was not found in the notice, so the gate was reset to unknown)
  - LLM v3: “Request for Supply Arrangement”
  - Baseline: “Request for Supply Arrangement”
- **supplier_origin_restriction**: label `unknown` · LLM v2 `unknown` (correct) · LLM v3 `unknown` (correct) · Baseline `canadian_and_trading_partners` (overclaimed)
  - label: _none_
  - LLM v2: _none_ (its evidence was not found in the notice, so the gate was reset to unknown)
  - LLM v3: _none_
  - Baseline: “Canada-Chile Free Trade Agreement (CCFTA)”
- **land_claims_applies**: label `no` · LLM v2 `yes` (wrong) · LLM v3 `no` (correct) · Baseline `yes` (wrong)
  - label: “excludes locations in areas subject to Comprehensive Land Claims Agreements”
  - LLM v2: “excluding any locations in areas subject to any of the Comprehensive Land Claim Agreement (CLCA)”
  - LLM v3: “excludes locations in areas subject to Comprehensive Land Claims Agreements”
  - Baseline: “to locations throughout Canada, excluding any locations in areas subject to any of the Comprehensive Land Claim Agreement (CLCA), equal to or above the Canada Korea Free Trade Agreement (CKFTA) threshold.”
- **documents_behind_login**: label `unknown` · LLM v2 `yes` (overclaimed) · LLM v3 `unknown` (correct) · Baseline `yes` (overclaimed)
  - label: _none_
  - LLM v2: “All suppliers that want to submit a response to this RFSA must enroll and establish an account in the Supplier Module of the PWGSC Centralized Professional Services System (CPSS) e-Portal”
  - LLM v3: _none_
  - Baseline: “• A registration checklist for Ariba SAP to help understand what is needed before creating an account, and;”

### test-08 · `SSC-26-00034395:T` · regular ⚠ unsure

> Labeller's note: RFI, not a bid solicitation: no evaluation exists, so no criteria_location value fits; see_solicitation as nearest

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `unknown` · LLM v2 `no` (overclaimed) · LLM v3 `unknown` (correct) · Baseline `unknown` (correct)
  - label: _none_
  - LLM v2: “This RFI is for information-gathering purposes only and is not a bid solicitation.”
  - LLM v3: _none_
  - Baseline: _none_
- **supplier_origin_restriction**: label `unknown` · LLM v2 `unknown` (correct) · LLM v3 `unknown` (correct) · Baseline `canadian_and_trading_partners` (overclaimed)
  - label: _none_
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: “Canada-Ukraine Free Trade Agreement (CUFTA)”
- **work_location.on_site**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `unknown` (missed) · Baseline `no` (correct)
  - label: “GD”
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: “GD”

### test-09 · `WS5940666474-Doc5953314939` · sa_or_so_request

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `no` (correct) · Baseline `no` (correct)
  - label: “Request for Standing Offer”
  - LLM v2: _none_ (its evidence was not found in the notice, so the gate was reset to unknown)
  - LLM v3: “Request for Standing Offer”
  - Baseline: “Request for Standing Offer”

### test-10 · `WS5054542893-Doc5767404666` · sa_or_so_request ⚠ unsure

> Labeller's note: Request for standing offers, but notice_type says Request for Proposal, so evidence is the description's RFSO text, not the whole notice_type

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `unknown` (missed) · Baseline `unknown` (missed)
  - label: “This request for standing offers (RFSO) cancels and supersedes”
  - LLM v2: _none_ (its evidence was not found in the notice, so the gate was reset to unknown)
  - LLM v3: _none_
  - Baseline: _none_

### test-11 · `cb-435-94227930` · regular ⚠ unsure

> Labeller's note: On-site across Canada, but bidders may bid per region, including Atlantic.

Status: expected **no_blockers_stated**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **criteria_location**: label `partly_stated` · LLM v2 `see_solicitation` (wrong) · LLM v3 `see_solicitation` (wrong) · Baseline `see_solicitation` (wrong)
- **work_location.on_site**: label `yes` · LLM v2 `yes` (correct) · LLM v3 `unknown` (missed) · Baseline `unknown` (missed)
  - label: “installation, service, and maintenance) on an “as and when requested” basis for its Integrated Security System (ISS) at CRA locations across Canada”
  - LLM v2: “at CRA locations across Canada”
  - LLM v3: _none_
  - Baseline: _none_

### test-12 · `cb-181-49859681` · regular ⚠ unsure

> Labeller's note: notice_type is Invitation to Qualify but the text reads as a construction tender with no pre-qualification wording; no follows test-05

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `unknown` (missed) · Baseline `unknown` (missed)
  - label: “Invitation to Qualify”
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: _none_
- **work_location.on_site**: label `yes` · LLM v2 `yes` (correct) · LLM v3 `yes` (correct) · Baseline `unknown` (missed)
  - label: “complete waterline upgrades at Prosser Rock in St. John’s, NL”
  - LLM v2: “complete waterline upgrades at Prosser Rock in St. John’s, NL.”
  - LLM v3: “complete waterline upgrades at Prosser Rock in St. John’s, NL.”
  - Baseline: _none_

### test-13 · `WS3971158100-Doc3971158168` · sa_or_so_request

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supplier_origin_restriction**: label `unknown` · LLM v2 `canadian_and_trading_partners` (overclaimed) · LLM v3 `unknown` (correct) · Baseline `canadian_and_trading_partners` (overclaimed)
  - label: _none_
  - LLM v2: “Canada-European Union Comprehensive Economic and Trade Agreement (CETA)”
  - LLM v3: _none_
  - Baseline: “Canada-Honduras Free Trade Agreement”
- **work_location.on_site**: label `unknown` · LLM v2 `unknown` (correct) · LLM v3 `no` (overclaimed) · Baseline `no` (overclaimed)
  - label: _none_
  - LLM v2: _none_
  - LLM v3: “The SLSA procurement vehicle will not be used to procure IT professional services”
  - Baseline: “GD”
- **documents_behind_login**: label `unknown` · LLM v2 `unknown` (correct) · LLM v3 `unknown` (correct) · Baseline `yes` (overclaimed)
  - label: _none_
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: “RFSA document can be found on the following link: http://discovery.ariba.com/rfx/16043971”

### test-14 · `cb-280-13103447` · regular ⚠ unsure

> Labeller's note: Docs: told to obtain from the Government of Canada tendering service, but other sources are allowed at own risk, so not portal-only (as dev-07)

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `no` (correct) · LLM v3 `unknown` (missed) · Baseline `unknown` (missed)
  - label: “Invitation to Qualify”
  - LLM v2: “Bidders that consider themselves qualified to provide this Work and intend to submit bids on this project”
  - LLM v3: _none_
  - Baseline: _none_
- **work_location.on_site**: label `yes` · LLM v2 `yes` (correct) · LLM v3 `yes` (correct) · Baseline `unknown` (missed)
  - label: “replacement of the existing diesel-powered pump at Newman Sound Pump House”
  - LLM v2: “the replacement of the existing diesel-powered pump at Newman Sound Pump House”
  - LLM v3: “the replacement of the existing diesel-powered pump at Newman Sound Pump House”
  - Baseline: _none_
- **documents_behind_login**: label `no` · LLM v2 `no` (correct) · LLM v3 `no` (correct) · Baseline `yes` (wrong)
  - label: “Bidders that elect to base their bids on documents obtained from other sources do so at their own risk”
  - LLM v2: “Bidders that consider themselves qualified to provide this Work and intend to submit bids on this project should obtain documents from the Government Electronic Tendering Service”
  - LLM v3: “Bidders that consider themselves qualified to provide this Work and intend to submit bids on this project should obtain documents from the Government Electronic Tendering Service”
  - Baseline: “PARKS CANADA IS NOT CURRENTLY ACCEPTING BIDS VIA SAP ARIBA, YOU MUST SUBMIT YOUR BID VIA FAX OR EMAIL AS INDICATED IN THE SOLICITATION DOCUMENT.”

### test-15 · `cb-362-77803083` · regular ⚠ unsure

> Labeller's note: notice_type is Invitation to Qualify but the text reads as a construction tender with no pre-qualification wording; no follows test-05 and test-12

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `unknown` (missed) · Baseline `unknown` (missed)
  - label: “Invitation to Qualify”
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: _none_
- **work_location.on_site**: label `yes` · LLM v2 `yes` (correct) · LLM v3 `yes` (correct) · Baseline `unknown` (missed)
  - label: “furnishing of all plant, labour, equipment and material for wharf repairs at Port Au Choix, Newfoundland”
  - LLM v2: “The work consists of the furnishing of all plant, labour, equipment and material for wharf repairs at Port Au Choix, Newfoundland”
  - LLM v3: “wharf repairs at Port Au Choix, Newfoundland”
  - Baseline: _none_

### test-16 · `cb-181-22166699` · regular

Status: expected **blocked**, LLM v2 **blocked**, LLM v3 **blocked**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `unknown` · LLM v2 `unknown` (correct) · LLM v3 `no` (overclaimed) · Baseline `unknown` (correct)
  - label: _none_
  - LLM v2: _none_
  - LLM v3: “Request for Proposal”
  - Baseline: _none_
- **supplier_origin_restriction**: label `unknown` · LLM v2 `unknown` (correct) · LLM v3 `unknown` (correct) · Baseline `canadian_and_trading_partners` (overclaimed)
  - label: _none_
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: “Canada-Chile Free Trade Agreement (CCFTA)”
- **work_location.on_site**: label `yes` · LLM v2 `yes` (correct) · LLM v3 `yes` (correct) · Baseline `unknown` (missed)
  - label: “One machine will be delivered and installed at the 202nd Workshop Depot (202 WD) in Montréal, Quebec”
  - LLM v2: “One machine will be delivered and installed at the 202nd Workshop Depot (202 WD) in Montréal, Quebec”
  - LLM v3: “One machine will be delivered and installed at the 202nd Workshop Depot (202 WD) in Montréal, Quebec”
  - Baseline: _none_

### test-17 · `PW-_NCS-030-11940` · sa_or_so_request ⚠ unsure

> Labeller's note: Land claims: header says 'Comprehensive Land Claim Agreement: No' but body says subject to CLCAs. On-site: SA set-up, no place stated (as dev-04).

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `yes` (wrong) · Baseline `unknown` (missed)
  - label: “All interested suppliers may submit a bid”
  - LLM v2: _none_ (its evidence was not found in the notice, so the gate was reset to unknown)
  - LLM v3: “Request for Proposal”
  - Baseline: _none_
- **supplier_origin_restriction**: label `unknown` · LLM v2 `canadian_and_trading_partners` (overclaimed) · LLM v3 `unknown` (correct) · Baseline `canadian_only` (overclaimed)
  - label: _none_
  - LLM v2: “Trade Agreement: CETA / WTO-AGP / CPTPP / CFTA / FTAs with Peru / Colombia / Panama / Korea”
  - LLM v3: _none_
  - Baseline: “PWGSC, the requirement will be subject to a preference for Canadian goods and/or services or will be limited to Canadian goods and/or services.”
- **land_claims_applies**: label `yes` · LLM v2 `no` (wrong) · LLM v3 `no` (wrong) · Baseline `no` (wrong)
  - label: “The proposed requirement is subject to Comprehensive Land Claims Agreement(s) (CLCAs)”
  - LLM v2: “Comprehensive Land Claim Agreement: No”
  - LLM v3: “Comprehensive Land Claim Agreement: No”
  - Baseline: “Comprehensive Land Claim Agreement: No”

### test-18 · `cb-233-12818571` · against_sa

Status: expected **blocked**, LLM v2 **blocked**, LLM v3 **blocked**, Baseline **blocked**.

- **criteria_location**: label `partly_stated` · LLM v2 `stated_in_notice` (wrong) · LLM v3 `partly_stated` (correct) · Baseline `see_solicitation` (wrong)
- **supplier_origin_restriction**: label `unknown` · LLM v2 `canadian_and_trading_partners` (overclaimed) · LLM v3 `unknown` (correct) · Baseline `unknown` (correct)
  - label: _none_
  - LLM v2: “trade_agreements: Canadian Free Trade Agreement (CFTA)”
  - LLM v3: _none_
  - Baseline: _none_

### test-19 · `cb-563-51417133` · regular ⚠ unsure

> Labeller's note: notice_type is Invitation to Qualify but the text reads as a construction tender with no pre-qualification wording; no follows test-05, test-12 and test-15

Status: expected **check_solicitation**, LLM v2 **check_solicitation**, LLM v3 **check_solicitation**, Baseline **check_solicitation**.

- **supply_arrangement_required**: label `no` · LLM v2 `no` (correct) · LLM v3 `unknown` (missed) · Baseline `unknown` (missed)
  - label: “Invitation to Qualify”
  - LLM v2: “notice_type: Invitation to Qualify”
  - LLM v3: _none_
  - Baseline: _none_
- **work_location.on_site**: label `yes` · LLM v2 `yes` (correct) · LLM v3 `yes` (correct) · Baseline `unknown` (missed)
  - label: “replace existing floating dock infrastructure at the Charlottetown harbour facility in Charlottetown, Labrador”
  - LLM v2: “The purpose of this project is to replace existing floating dock infrastructure at the Charlottetown harbour facility in Charlottetown, Labrador.”
  - LLM v3: “replace existing floating dock infrastructure at the Charlottetown harbour facility in Charlottetown, Labrador.”
  - Baseline: _none_

### test-20 · `cb-673-2167068` · against_sa ⚠ unsure

> Labeller's note: Work is Remote but resources must travel to DND sites; labelled on_site yes since personnel must be physically present at times

Status: expected **blocked**, LLM v2 **blocked**, LLM v3 **blocked**, Baseline **blocked**.

- **supplier_origin_restriction**: label `unknown` · LLM v2 `unknown` (correct) · LLM v3 `unknown` (correct) · Baseline `canadian_and_trading_partners` (overclaimed)
  - label: _none_
  - LLM v2: _none_
  - LLM v3: _none_
  - Baseline: “Canada-Ukraine Free Trade Agreement (CUFTA)”
- **clearance.personnel**: label `unknown` · LLM v2 `reliability` (overclaimed) · LLM v3 `reliability` (overclaimed) · Baseline `unknown` (correct)
  - label: “Supplier Security Clearance required: DOS - Reliability”
  - LLM v2: “Supplier Security Clearance required: DOS - Reliability”
  - LLM v3: “Supplier Security Clearance required: DOS - Reliability”
  - Baseline: “Supplier Security Clearance required: DOS - Reliability”
- **work_location.on_site**: label `yes` · LLM v2 `unknown` (missed) · LLM v3 `unknown` (missed) · Baseline `unknown` (missed)
  - label: “The resources will be required to travel to DND sites within Canada”
  - LLM v2: _none_
  - LLM v3: “The resources will be required to travel to DND sites within Canada.”
  - Baseline: _none_
- **documents_behind_login**: label `no` · LLM v2 `unknown` (missed) · LLM v3 `no` (correct) · Baseline `unknown` (missed)
  - label: “Request for Proposal (RFP) documents will be e-mailed directly, from the DND Point of Contact (DND POC), to the Qualified Supply Arrangement Holders”
  - LLM v2: _none_ (its evidence was not found in the notice, so the gate was reset to unknown)
  - LLM v3: “Request for Proposal (RFP) documents will be e-mailed directly, from the DND Point of Contact (DND POC)”
  - Baseline: _none_
