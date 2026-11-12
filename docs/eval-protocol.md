# Eval protocol

## Frozen versions (2026-11-11, before any labelling)

| Extractor | Version | Pinned hash (sha256) |
|---|---|---|
| LLM (claude-haiku-5-5, effort low, max_tokens 2000) | prompt `v2` | system prompt `def2a660…c9d3038` |
| | schema `1` | `ExtractionData` JSON schema `4fab443b…898e83e` |
| Keyword baseline | `rules-v1` | fingerprint of `keyword.py` + `eligibility/places.py` `04590c85…a4a9f76441` |

`tenders/tests/extraction/test_frozen.py` pins the full hashes, so any change fails CI.
`places.py` is part of the baseline fingerprint: changing the gazetteer changes the baseline.

Any change after labelling is a **new version** (prompt `v3`, `rules-v2`, …). Don't edit a
frozen version. Score each new version on the test split, report it under its own version
name, and never let it overwrite a frozen version's result.

## Cases

- 30 hand-labelled open notices: **10 dev**, **20 test**. Fix the split and record it before
  any scoring.
- Use dev for reading errors and designing changes. Score test only, and never use it to tune.
- Labels hold reference numbers and gate values only, never notice text.

## Metrics (for the LLM and the baseline, on the same cases)

1. **Per gate:** for each of the six gates (clearance split into corporate and personnel),
   count matches with the label, including "unknown", and report the unknown rate.
2. **Can-bid decision:** Assessment status (blocked / check_solicitation /
   no_blockers_stated) against the labelled decision, as a count table. Record the eligibility
   `RULES_VERSION` with each result.
3. **Grounding:** count `ungrounded` and `ungrounded_detail` flags, and how many gates fell
   back to unknown because of them.
4. **Redaction audit count:** contact-info leaks the independent redaction audit finds in the
   30 cases. Target 0.
