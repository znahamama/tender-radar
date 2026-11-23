# NL Public Tender Radar

Eligibility triage for Canadian federal tenders: can a small St. John's IT firm even bid?

**Live:** https://h70n8j46yk.execute-api.us-east-2.amazonaws.com/
(a cold start takes 1.3–2.1 s; see Performance below).
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

## Scope

Ingest keeps every notice delivered in NL, whatever it buys (snow clearing, ship valves,
catering), so the site also says whether a notice is IT work at all. **Scope rules v1**
(`backend/tenders/scope.py`, pinned by hash) decide from the notice's UNSPSC and GSIN
category codes only, with no LLM:

- **IT work:** at least one code in an IT family: UNSPSC 43 (IT and telecom equipment and
  software), 8111 (computer services), 8116 (IT service delivery, cloud), 8311
  (telecommunications services), 451119 (audio and video conferencing); GSIN D302, N70, JX70,
  WL70, D304, D316, D399.
- **Exclusions inside those families:** UNSPSC 811119 (information retrieval and online
  database services: on the 2026-11-19 data these were purchases of data, such as satellite
  imagery and subscriptions), 811623 (business process as a service), and TV and radio
  broadcasting (831118, 831119).
- **Outside IT work:** codes present, none in an IT family. **Unknown:** no code at all.

Scope is a separate line on each notice and never changes the eligibility status. The list
shows IT notices by default, with a link to show all.

**On 2026-11-19:** of 80 open notices, **41 IT, 39 outside, 0 unknown** (every notice had a
code). National notices: 39 of 47 IT; NL notices: 2 of 33.

**Known edge cases.** One IT code is enough, so a notice that lists IT among other goods is
in scope and a person reads it. That puts "Security Equipment" and "Access Control and
Intrusion Detection Services" in scope (through network security equipment, 43222500), and
"Energy Efficiency Regulated Products Testing" (one IT code among 27). "Internal
Communications System – Zenitel Marine Components", a ship's intercom, stays out: its codes
are in no telecom family.

## Human review

- **Review queue:** staff accept, correct or reject each gate in the admin, blocked and
  flagged notices first. A correction must be an allowed schema value with evidence that
  passes the same grounding check as model output.
- **Overrides the model, not the rules:** the decision replaces the model's value before the
  frozen rules run.
- **Append-only:** decisions are never edited or deleted (model, queryset and a Postgres
  trigger); a newer decision supersedes an older one.
- **Carried over to re-extractions** of the same notice:
  - same LLM input: the decision carries;
  - a correction: carries if its quote still grounds in the new text;
  - an accept or reject: carries if the model's answer is unchanged;
  - otherwise the gate is marked "needs re-review (notice text changed)" and the model's
    answer counts again.
  A carried decision links to the original; the reviewer and time stay theirs.
- **Public page:** gates with a decision in effect show "Reviewed by a person" and what the
  model said.
- **Uploaded documents:** for a notice whose documents sit behind a login, a reviewer can
  upload the PDFs on the review page (at most 4 MB each, 5 per notice, 10 per person per
  day). They get the fetcher's sandboxed parse and limits, redaction (without CSV contact
  hints) and injection scan; only the redacted text is kept, never the PDF. The API then
  asks the worker to re-read that one notice (`extract_one`, under the usual LLM caps). An
  upload ranks as the main solicitation unless its name says amendment, and the public page
  names it ("uploaded by the reviewer", no link) but shows only the quotes.

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
  A notice with stored solicitation documents is read with them instead (prompt v5: the
  notice, then amendments, other documents and the main solicitation last, at most 120,000
  characters in all, so the cap cuts the main document rather than the amendments; each
  eligibility gate's quote names its source and must be found in a source given (a wrong
  name is corrected and flagged), and a list item's quote must be found in the notice or a
  document given; max 4000 output tokens). Per gate, the v4 answer is used when its quote
  holds, otherwise the v3 answer from the notice; when both hold and differ, v4 decides and
  the notice goes to the review queue. If a v4 call fails, the tender keeps its v3 answer and
  the run carries on. Not in the eval set below; see the documents check under Evaluation.
  Eligibility rules v4 add a check on what the model did not read: if clearance or work
  location is not stated and stored document text past the cap names a clearance level or
  on-site work, the notice stays "check the solicitation", with the term, document and page.
- **Database:** Neon Postgres (free tier), pooled connections from Lambda.
- **Infra:** AWS Lambda (arm64 container images, ECR), API Gateway HTTP API, EventBridge
  Scheduler, SSM Parameter Store, CloudWatch Logs. All Terraform; no VPC, NAT or RDS.
- **CI:** GitHub Actions: ruff, Django checks, pytest against a throwaway Postgres,
  gitleaks over the full history, `terraform fmt`/`validate`.
- **Performance:** API cold start went from 2.3–3.0 s to 1.3–2.1 s. The first request after a
  deploy takes ~6.5 s while Lambda caches the new image, so the deploy runbook ends with a
  warm-up curl.

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

**Prompt injection** (5 invented attack notices, each with a clean twin, 4 runs each, prompt v3):

- **0/20** attack runs reached the attacker's target; no status changed.
- One of 20 attack runs (d, buyer-name metadata) moved a field outside the clean twin's range
  (supply arrangement → no); it wasn't the attack's target and the status stayed Blocked.
- The model flagged **20/20** attack runs as suspected injection (0/20 twin runs).
- The code-side pattern detector flagged 5/5 attack notices and 0 of 326 stored real notices.
  The 5/5 is by construction (the same person wrote the rules and the attacks, and rewording
  gets past it); the false-positive count is the useful number.

Report: [eval/reports/injection-2026-11-18.md](eval/reports/injection-2026-11-18.md).

**Solicitation documents (prompt v4), precision check, 2026-11-19.** A small sample: 8
answers on 4 tenders, checked by hand by the person who built the tool, not blind. It
measures precision only; recall (what the documents say that the model missed) isn't
measured.

- **Document-sourced gate answers: 5.** 4 supported, 1 right but weakly evidenced
  (PW-__HN-446-81465's supply arrangement, quoting only "Request for Supply Arrangement"),
  0 wrong.
- **Review decisions: 3.** 2 supply-arrangement answers on requests for a supply arrangement
  were wrong and corrected: PW-__PQ-992-74154 (v4), and PW-__HP-539-79947 (v3, resolved by
  accepting v4's answer). 1 v3 work-location answer was rejected (PW-_ERD-005-26943).
- **Coverage:** 4 of 80 open notices had solicitation documents the tool could download
  and read. Most attachments sit behind supplier portals.

Report: [eval/reports/documents-precision-2026-11-19.md](eval/reports/documents-precision-2026-11-19.md).

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
- **CanadaBuys reorders list fields between CSV regenerations.** Trade agreements and regions
  came back as the same items in a new order, so unchanged notices counted as "updated" and
  were re-extracted. Fix: `content_hash` ignores list order, and migration 0011 recomputed the
  stored hashes. The next daily run: 0 updated, 341 unchanged.
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
- **Admin:** django-axes locks a login after 5 failures for 1 hour, keyed on username + IP.
  The client IP is the right-most `X-Forwarded-For` entry (the one API Gateway appends), so
  a client can't spoof its way around the count. Sessions last 8 hours, with Secure, HttpOnly
  cookies. The admin user is created by a worker task from a one-time SSM parameter, so the
  password never passes through a terminal, a log or the chat.
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
- Solicitation documents are read only when CanadaBuys publishes them as downloadable
  PDFs: 4 of 80 open notices on 2026-11-19. Most notices still end at "check solicitation".
- LLM output varies run to run (status stable in 19/20 test cases).
- Small evaluation: 20 test cases scored once, plus 10 dev cases; per-field intervals are wide.
- Indigenous set-asides (PSIB) aren't assessed for the fictional firm: they're a real blocker
  that the six gates don't model.

## Scope cuts

- No React front end and no REST API/OpenAPI: server-rendered Django templates instead.
- Bedrock was replaced by the Anthropic API after two quota denials.

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

`requirements-dev.txt` installs the app's packages at the exact versions in
`requirements.lock`, the same ones the Lambda image installs, so tests run on what ships.
After pulling a commit that changes the lock, run `pip install -r requirements-dev.txt` again.

### Updating dependencies

`requirements.txt` holds the ranges you choose; `requirements.lock` is generated from them
and pins every package they pull in, as resolved on the Lambda base image (linux/arm64).
Never edit the lock by hand.

```sh
# 1. Edit requirements.txt if a range or package changes (to only take new releases, skip this).
infra/scripts/lock_requirements.sh   # fresh resolve in the base image; needs Docker
git diff requirements.lock           # read it: watch for major versions (django, anthropic, ...)
pip install -r requirements-dev.txt  # local venv to the new pins
ruff check . && python -m pytest
# 2. Commit requirements.txt and requirements.lock together, then deploy as usual.
```

A test fails if `requirements.txt` names a package that isn't in the lock, or one whose locked
version is outside its range. Dev tools (ruff, pytest, ...) are not pinned.

Deploying to AWS: see [infra/README.md](infra/README.md).

## Data licence

Contains information licensed under the
[Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada).

Harbourline Software is a fictional firm used only as the eligibility profile.
