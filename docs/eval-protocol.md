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

`python backend/manage.py run_eval --split dev [--repeats N]` writes
`eval/reports/<split>-<date>.md` and `.json` (same data). It uses the cached LLM extraction for
the frozen config (extracting it through the normal path and caps if missing) and the keyword
baseline, both compared after post-validation. `--repeats N` adds N uncached LLM runs per case,
stored as `EvalExtraction` rows under the run's tag (LlmCall purpose `eval`), never as an
`Extraction`, so the site never shows them. Scoring: `tenders/evals/scoring.py`.
