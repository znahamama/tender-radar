# Reviewed pool: candidate labels from human review

Written by `python backend/manage.py export_review_labels`, one file per extraction that a
person corrected in the admin review queue. **A separate pool:** these files are not in
`eval/cases/manifest.json`, so `run_eval` never scores them and `check_labels` lists them as
"not in the manifest (ignored)". A notice that is a dev or test case is never exported here.

Same format as the dev/test label files (`docs/label-guide.md`). Only a person's
corrections are filled in. Gates whose latest decision accepted or rejected the model's
answer, or that weren't reviewed, are `TODO`, because label files hold no model output.
Check a file with the label checker before using it. Before any of these become eval
data, fix and record a new split in a new manifest, as for dev/test
(`docs/eval-protocol.md`).
