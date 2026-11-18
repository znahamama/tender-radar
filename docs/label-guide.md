# Label guide

Files: `eval/cases/dev/dev-01.yaml` … `dev-10.yaml`, `eval/cases/test/test-01.yaml` …
`test-20.yaml`. Each opens with the notice (metadata and the full redacted description) in a
comment block. Don't edit that block: `check_labels` reads the notice text from it. Replace
every `TODO`. There is no status field: the harness derives it from your gates with
eligibility rules v3.

## The rule

**"no" (or "none") only if the notice explicitly says so. Silent means "unknown".** Never
infer from what notices like this usually require, from the buyer, or from outside knowledge.

## Fields (definitions as in prompt v2, plus two clarifications)

- **criteria_location**: where the evaluation criteria are: `stated_in_notice`,
  `partly_stated`, or `see_solicitation` (only in the solicitation documents).
- **supply_arrangement_required**: `yes` if bidding is limited to holders of a supply
  arrangement or standing offer. That includes a notice_type of "RFP against Supply
  Arrangement" (quote that whole value) and text naming TBIPS, TSPS, ProServices, SBIPS or a
  similar pre-qualified method. `name` only if the notice names the arrangement.
  A notice inviting suppliers to apply for a new supply arrangement or standing offer is not
  limited to existing holders. *Clarification:* label such a request to **become** a holder
  `no`, with the whole notice_type value as evidence (e.g. `"Request for Supply
  Arrangement"`, `"Request for Standing Offer"`), and `name: null`.
- **supplier_origin_restriction**: `canadian_only` if limited to Canadian
  suppliers/goods/services; `canadian_and_trading_partners` if open to Canadian suppliers and
  those covered by named trade agreements; `none` if the notice states it is open to all
  suppliers.
- **clearance**: `corporate` is the organization's clearance: a Facility Security Clearance
  (FSC) or a Designated Organization Screening (DOS). A DOS is `reliability`; an FSC takes its
  stated level (`secret`, `top_secret`). `personnel` is the screening of the supplier's staff:
  Reliability Status is `reliability`, then `secret`, `top_secret`. "Security requirement:
  none" means both `none`. If both are stated, quote the stricter one. Write `none`, not `no`.
- **work_location**: `on_site` is `yes` only if the supplier's personnel must physically work
  at a location. Where goods are delivered, stored or operated is not on-site work. A
  goods-only notice with no installation or services is `no` (quote the text, or the
  procurement_categories value, that shows it). `place` is where personnel must work, copied
  from the title or description, never from the regions values; null if not said.
- **land_claims_applies**: whether a comprehensive land claims agreement applies (a
  trade_agreements value naming one counts). *Clarification:* "excludes locations in areas
  subject to Comprehensive Land Claims Agreements" means `no`.
- **documents_behind_login**: whether solicitation documents are obtained only through a
  portal, registration or login, and which `portal` (as written in the notice).

## Evidence

- Any value other than `unknown` needs evidence; an `unknown` gate keeps `evidence: null`.
- Evidence is an **exact copy** of the notice text: at least 3 words (and 15 characters) from
  the title, a metadata value or the description, or one **whole** metadata value (e.g.
  `"RFP against Supply Arrangement"`, `GD`). No paraphrase, no `...`, no field names, not the
  URL. Case, spacing and curly vs straight quotes don't matter.
- Write evidence and details in double quotes: `evidence: "Bidders must hold a valid ..."`
  (without the dots). If the text has a `"`, use single quotes and double any `'` inside.
- Bare `yes` / `no` / `unknown` are fine for values.

## Unsure

Set `unsure: true` only for a genuinely ambiguous case, with a one-line `notes` saying why.
Still give your best value for every gate.

## Rulings applied during labelling

These rulings were made while labelling the dev and test sets and applied to all 30 cases;
they were written down after labelling finished, and no label was changed.

| Situation | Label | Example |
|---|---|---|
| Trade agreements only listed in metadata, no sentence on who may bid | origin `unknown` | dev-02 |
| "open only to Canadian suppliers and to Suppliers of an Applicable Trading Partner" | origin `canadian_and_trading_partners` | dev-01, test-09 |
| "...and Suppliers of a Non-Trading Partner are eligible" | origin `none` | test-02, test-03 |
| Indigenous set-aside (PSIB) | not an origin restriction: origin `unknown`, noted | test-01 |
| "Security Requirement: None" | corporate `none`, personnel `none` | dev-02 |
| "Supplier Security Clearance required: DOS - Reliability" | corporate `reliability`, personnel `unknown` | test-20 |
| "There are security requirements" with no level | clearance `unknown` | test-11 |
| Request for SA/SO, or an Invitation to Qualify to become pre-qualified | SA `no` | dev-04, test-05, test-14 |
| Construction notice typed "Invitation to Qualify" with no pre-qualification wording | SA `no`, unsure | test-12, test-15, test-19 |
| ITQ results / qualified-supplier list, or "only pre-qualified suppliers may bid" | SA `yes` | dev-09, test-20 |
| notice_type contradicts the text (e.g. typed RFP but the text is an RFSO) | follow the text, unsure | test-10, test-17 |
| Goods plus installation or services at a named place | on_site `yes`, with that place | dev-03, test-16 |
| Goods only, no installation stated | on_site `no`, evidence `GD` or the goods sentence | dev-01, test-09 |
| SA set-up listing services, no place or on-site requirement stated | on_site `unknown` | dev-04, test-17 |
| "required to travel to [sites]", even if the region is Remote | on_site `yes`, unsure | test-20 |
| Portal URL only, or a portal used only for **submitting** bids | documents `unknown` | dev-01, dev-05, test-09, test-13 |
| "MANDATORY ... obtain documents through [platform]" | documents `yes` | dev-08 |
| "documents obtained from other sources do so at their own risk" | documents `no` | dev-07, test-14 |
| Documents e-mailed directly to invited holders | documents `no` | dev-02, test-01, test-20 |
| "excludes locations in areas subject to CLCAs" | land claims `no` | test-07 |
| Notice contradicts itself on land claims (header "No", body "subject to CLCAs") | follow the specific body text, unsure | test-17 |
| Indigenous Participation Plan without a named CLCA | land claims `unknown` | dev-07, test-14 |
| RFI or results notice with no evaluation | criteria `see_solicitation` as nearest, unsure | dev-09, test-08 |

## Process

1. Label the dev files **in order**, dev-01 to dev-10. Run
   `python backend/manage.py check_labels` after each file and fix what it lists.
2. Until all 10 dev files are done, don't open admin, the site or any extraction for these
   notices.
3. Once you have seen any results, don't edit earlier files. A real mistake gets a note in
   the eval log, not a silent fix.
4. Label all 20 test files the same way before any test scoring, without looking at their
   outputs.

## Worked examples (invented notices)

**A.** notice_type "Request for Supply Arrangement". Description: "Canada invites suppliers to
qualify for the Northern Data Services supply arrangement. This procurement excludes locations
in areas subject to Comprehensive Land Claims Agreements. Bid documents are available on SAP
Business Network after free registration."

```yaml
criteria_location: see_solicitation
gates:
  supply_arrangement_required: {value: no, name: null, evidence: "Request for Supply Arrangement"}
  supplier_origin_restriction: {value: unknown, evidence: null}
  clearance: {corporate: unknown, personnel: unknown, evidence: null}
  work_location: {on_site: unknown, place: null, evidence: null}
  land_claims_applies: {value: no, evidence: "excludes locations in areas subject to Comprehensive Land Claims Agreements"}
  documents_behind_login: {value: yes, portal: "SAP Business Network", evidence: "Bid documents are available on SAP Business Network after free registration"}
unsure: false
notes: ""
```

Supply arrangement is `no`: the notice invites firms to qualify, so bidding is not limited to
existing holders.

**B.** procurement_categories "SRV". Description: "The Contractor will provide a database
administrator to work on-site at the Mount Pearl data centre three days per week. The
Contractor must hold a valid Facility Security Clearance (FSC) at the level of Secret.
Evaluation criteria: experience (40 points) and approach (60 points)."

```yaml
criteria_location: stated_in_notice
gates:
  supply_arrangement_required: {value: unknown, name: null, evidence: null}
  supplier_origin_restriction: {value: unknown, evidence: null}
  clearance: {corporate: secret, personnel: unknown, evidence: "The Contractor must hold a valid Facility Security Clearance (FSC) at the level of Secret"}
  work_location: {on_site: yes, place: "Mount Pearl data centre", evidence: "to work on-site at the Mount Pearl data centre"}
  land_claims_applies: {value: unknown, evidence: null}
  documents_behind_login: {value: unknown, portal: null, evidence: null}
unsure: false
notes: ""
```

Personnel stays `unknown`: the notice states only the corporate FSC.
