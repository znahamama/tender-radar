# Prompt-injection cases

Five attack notices, each with a clean twin. **All text here is invented**: no real notice,
solicitation number or person. Department names are used only to make the notices look like
federal ones.

Each `<id>.yaml` holds one notice with a single `{ATTACK}` marker. The attack variant puts
`attack` there; the clean twin removes the marker, so the twins differ only by the attack
text (`tenders/tests/evals/test_injection.py` checks this). Both go through the real ingest
path (redaction, validation, classification) before the extractor sees them.

- `expected`: the gate answers for the notice as written. The attack changes none of the
  facts, so both twins have the same expected answers.
- `expected_place`: where personnel must work, when on-site (used for the expected status).
- `attack_targets`: the field values the attack tries to produce.

Run: `python backend/manage.py run_injection_eval --repeats 3` (report in
`eval/reports/injection-<date>.md` and `.json`). This is a probe, not a security guarantee.
