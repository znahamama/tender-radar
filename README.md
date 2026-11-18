# NL Public Tender Radar

Eligibility triage for Canadian federal tenders: can a small St. John's IT firm even bid?

**Live:** https://h70n8j46yk.execute-api.us-east-2.amazonaws.com/
(the first load after a quiet period can take about 9 s while the Lambda starts; a fix is planned).
The site runs prompt v3 as of 2026-11-18: its 90 open notices were re-extracted for about $0.09.

## The problem

Federal tender notices often hide hard blockers: bidding limited to existing supply-arrangement
holders, security clearances, or on-site work outside Newfoundland and Labrador. These are
written as free text, often only in attached documents. Keyword alerts can match "security
clearance" but can't tell "you can't bid" from "the notice doesn't say".

## What it does

1. **Ingest:** a daily job downloads the CanadaBuys open-data CSVs and keeps notices delivered
   in NL plus national or NL-scoped IT notices.
2. **Redact:** contact names, emails and phone numbers are removed before anything is stored.
3. **Extract:** an LLM reads each redacted notice and fills six gates (supply arrangement,
   supplier origin, clearance, on-site work location, land claims, documents behind a login),
   each with an exact quote from the notice as evidence. Code checks every quote against the
   notice; a value whose quote isn't there is reset to `unknown`.
4. **Decide:** plain Python rules (not the LLM) compare the gates with the profile of a
   **fictional** firm, Harbourline Software (no supply arrangements, no clearances), and give
   one of three statuses:
   - **blocked**: the notice states a requirement the firm can't meet (a supply arrangement
     it doesn't hold, a corporate or Secret-level clearance, on-site work outside NL);
   - **check solicitation**: no blocker found, but the notice says its criteria are in the
     solicitation documents;
   - **no blockers stated**: the notice states its criteria and none block; gates it doesn't
     mention are listed as "not stated".

The LLM never makes the decision. It only extracts quoted facts; the status comes from
versioned, unit-tested rules. Where CanadaBuys metadata already says a notice is "against a
supply arrangement", that metadata overrides whatever the LLM extracted.

## Architecture

```mermaid
flowchart LR
    subgraph Daily run
        S[EventBridge Scheduler<br/>06:15 St. John's] --> W[Worker Lambda]
        W -->|download CSV| C[(CanadaBuys<br/>open data)]
        W --> R[Redaction]
        R --> A[Anthropic API<br/>Claude Haiku 5.5]
        R --> DB[(Neon Postgres)]
        A --> DB
    end
    subgraph Public site
        U[Browser] --> H[API Gateway<br/>HTTP API]
        H --> L[API Lambda<br/>Django]
        L --> DB
    end
```

- **Backend:** Python 3.12, Django 5.2, server-rendered pages (no JavaScript on public pages).
- **LLM:** `claude-haiku-5-5`, prompt v3, structured outputs, effort low, max 2000 tokens.
- **Database:** Neon Postgres (free tier), pooled connections from Lambda.
- **Infra:** AWS Lambda (arm64 container images, ECR), API Gateway HTTP API, EventBridge
  Scheduler, SSM Parameter Store, CloudWatch Logs. All Terraform; no VPC, NAT or RDS.
- **CI:** GitHub Actions: ruff, Django checks, pytest against a throwaway Postgres,
  gitleaks over the full history, `terraform fmt`/`validate`.

## Evaluation

The question: does the LLM extract the gates better than a keyword baseline, and does the
final status match what a human would decide?

**Test set: 20 blind cases, scored once** at commit `3cce2a9` on 2026-11-18, for prompt v3
(on the site now), prompt v2 and the keyword baseline. Values are after the grounding check,
i.e. what the site shows. "Scored" is the cached run every other number uses; the LLM was also
run 3 more times per case, and v3's mean and range are over all its runs.

| Field (correct of 20) | v3 scored | v3 mean (min–max) | v2 scored | Baseline |
|---|---|---|---|---|
| criteria_location | 18 | 86% (83–90%) | 17 | 17 |
| supply_arrangement_required | 12 | 63% (60–70%) | 10 | 13 |
| supplier_origin_restriction | 19 | 93% (89–95%) | 17 | 10 |
| clearance.corporate | 20 | 100% (100–100%) | 20 | 20 |
| clearance.personnel | 19 | 99% (95–100%) | 19 | 19 |
| work_location.on_site | 15 | 68% (65–75%) | 17 | 12 |
| land_claims_applies | 18 | 90% (89–90%) | 15 | 18 |
| documents_behind_login | 20 | 98% (95–100%) | 17 | 14 |
| **All fields** | **141/160** | 87% (86–88%) | **132/160** | **123/160** |
| All fields, excluding unsure cases (9 cases) | 68/72 | | 60/72 | 58/72 |

- **Status (can-bid decision):** v3 **18/20**, v2 **18/20**, baseline **17/20** agree with
  the labels (95% interval for 18/20: 70–97%).
- **False blocks: 0** for all three. **Both LLM prompts caught 5 of 5 blocked notices**; the
  baseline missed one (test-16), which landed on "check solicitation".
- **Overclaims** (a value the notice doesn't state): v3 **3**, v2 **8**, baseline **12**.
  Wrong / missed: v3 4 / 12, v2 5 / 15, baseline 8 / 17.
- **Consistency:** across runs, the status was the same in 19/20 cases for both prompts.
- **Cost:** the test run made 150 API calls for about $0.17. The baseline is free.

**Protocol** ([docs/eval-protocol.md](docs/eval-protocol.md))

- **Frozen before labelling:** prompt `v2`, schema `1`, the keyword baseline `rules-v1` and
  eligibility rules v3 are pinned by hash in a test, so any change fails CI. Changes after
  labelling must ship as new versions.
- **Seeded selection:** 30 notices picked by a script (seed 20261116) with minimum quotas for
  the hard cases (supply arrangements, clearances, land claims, NL vs national, open vs
  expired): 10 dev, 20 test.
- **Blind hand labels:** each gate labelled from the redacted notice with a written guide,
  without seeing any model output. The expected status is derived from the labels by the same
  rules, not labelled directly. 5 of 10 dev and 11 of 20 test cases are marked unsure.
- **Dev designed v3, test scored it once.** Prompt v3 was written from the 10 dev cases only,
  each change traced to a dev error or a label-guide clarification. The test set was then
  scored once, with v2, v3 and the baseline together; nothing is tuned on it afterwards.
- **One v3 change was removed before scoring.** Its "Invitation to Qualify" wording traced to a
  test case rather than to dev or the guide, so it was taken out and v3 re-pinned first.
- **Repeats:** the per-run call cap stopped the repeats 3 calls short (test-19 and test-20 have
  3 runs instead of 4). Every case has its scored run from all three systems.

**Dev set (n=10, used to design v3):** all fields v3 71/80, v2 67/80, baseline 63/80; status
7/10, 6/10, 6/10 ([eval/reports/dev-v3-2026-11-18.md](eval/reports/dev-v3-2026-11-18.md)).

**What this does and doesn't show.**

- With n = 20, one case is 5 points and the per-field 95% intervals are wide (18/20 is
  70–97%). The test set shows where each system fails better than it ranks them.
- v3's supply-arrangement gain on dev (4/10 → 9/10) didn't carry over: on test it scored
  12/20, below the baseline's 13/20.
- On-site work is v3's weakest gate: 15/20 scored, mean 68% across runs, below v2's 17/20.
- LLM answers vary from run to run, which is why the mean and range are reported next to the
  scored run.

Full report: [eval/reports/test-final-2026-11-18.md](eval/reports/test-final-2026-11-18.md)
(per-case disagreements with evidence quotes for v3, v2 and the baseline; dev and test side by
side; same data in `.json`).

## Findings

- **v3 overclaims less and quotes better than v2.** On the test set it stated something the
  notice doesn't say 3 times against v2's 8, and none of its scored answers had to be reset by
  the grounding check (v2: 8). All fields: 141/160 against 132/160.
- **Most notices push their criteria into attached documents.** On the production run of
  2026-11-16, 72 of 88 open notices were "check solicitation" (14 blocked, 2 no blockers
  stated), and 8 of the 10 dev notices were labelled `see_solicitation`. For these, "check solicitation" is the honest answer, and the
  tool's value is narrowing the list rather than clearing notices.
- **The LLM is weak on supply arrangements.** 4/10 against the baseline's 9/10. It left the
  gate unknown even when the notice type itself said "Request for Standing Offer" or "Request
  for Supply Arrangement", and twice its quote failed the grounding check. This gate accounts
  for 2 of its 3 missed blocks (dev-02, dev-09) and is the main target for prompt v3.
- **The baseline overclaims supplier origin from trade agreements.** It read any named trade
  agreement (CPTPP, WTO GPA, bilateral FTAs) as an origin restriction: 4 overclaims, where the
  LLM made 1.
- **The baseline misses on-site work written in plain language** ("snow clearing services at
  the St.John’s Research and Development Centre"): 3 misses where the LLM was right.
- **Exclusion wording fools both systems.** On notice `cb-8448-42897985` (seen on the live
  detail page; not an eval case, and excluded from selection for that reason), both the LLM
  and the baseline read "excludes locations in areas subject to Comprehensive Land Claims
  Agreements" as land claims *applying*. The label guide treats this wording as `no`.
  This is one observation, not an eval metric.
- **Run-to-run variance shows up as value↔unknown flips.** Every gate that changed between runs
  moved between a value and `unknown`; none flipped between two values (e.g. yes↔no). Status
  flips follow from those (dev-02 went blocked ↔ check solicitation as its supply-arrangement
  gate flipped).

## Safety and cost

- **Contacts are redacted before storage**, so the database and the LLM only ever see redacted
  text. An independent audit command (`audit_redaction`) scans stored titles and descriptions
  for every contact in the raw CSVs, using its own detectors rather than the redactor's, and
  reports only counts and reference numbers.
- **Notice text is treated as untrusted:** it sits inside `<notice>` delimiters with angle
  brackets escaped, the prompt says it's data, not instructions (and asks the model to flag
  injection attempts), the output must match a JSON schema, and every evidence quote must appear
  in the notice or the gate is reset to `unknown` (6 gates were reset in the dev run).
- **Strict CSP:** public pages load one same-origin stylesheet and no scripts
  (`default-src 'none'`), with `frame-ancestors 'none'`.
- **No long-lived keys:** Lambdas use IAM roles, deploys use short-lived `aws login`
  sessions, and CI holds no AWS credentials.
- **Secrets live in SSM SecureStrings** created by hand; Terraform knows only their names, so
  the values never reach Terraform state or the repo.
- **LLM spend is capped** per run (50 calls by default) and per day ($0.15 on the deployed
  worker), checked before every API call.
- **Cost:** total LLM spend to date is under $1 across all dev, production and eval runs. AWS is estimated at $0–1/month (mostly free tier); Neon is on its free tier.
  See the cost table in [infra/README.md](infra/README.md).

## Limits

- One fictional firm profile; no real users or client data.
- Federal notices from CanadaBuys only; no provincial or municipal sources.
- No attachment parsing, so most notices end at "check solicitation".
- LLM output varies run to run (status stable in 19/20 test cases).
- Small evaluation: 20 test cases scored once, plus 10 dev cases; per-field intervals are wide.
- Indigenous set-asides (PSIB) are a real blocker that the six gates don't model.

## Run it locally

Needs Python 3.12 and a Postgres database. Put `DATABASE_URL` and `DJANGO_SECRET_KEY` in a
`.env` at the repo root (and `ANTHROPIC_API_KEY` only if you want real LLM calls).

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cd backend
python manage.py migrate
python manage.py ingest_canadabuys --url-defaults --save   # download + redact + store
python manage.py extract_tenders --status Open --baseline  # free; drop --baseline for the LLM
python manage.py assess_tenders
python manage.py runserver
cd .. && python -m pytest
```

Deploying to AWS: see [infra/README.md](infra/README.md).

## Data licence

Contains information licensed under the
[Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada).

Harbourline Software is a fictional firm used only as the eligibility profile.

## Roadmap

- Prompt-injection test cases.
- Fix the cold start on the first page load.
