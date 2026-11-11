# NL Public Tender Radar

Ingests public CanadaBuys tender notices, redacts contact info, has an LLM extract 6 fields,
and code computes eligibility for a fictional small IT firm in St. John's.
Public data only; no real user or client data.

## Layout
- `backend/`: Django project `config`, app `tenders`. `frontend/` and `infra/` come later.
- Run `manage.py` from `backend/`. Git, requirements and `.env` live at the repo root.

## Rules
- Always use the venv: `source .venv/bin/activate`, then `python -m pytest`
  (conda's base pytest has hijacked this shell before).
- Secrets: never read, print, cat or edit `.env`. Never put secrets in code, tests, CI or
  commits. CI uses a throwaway Postgres service, never Neon.
- Settings: `DATABASE_URL_DIRECT` is used locally if set, otherwise `DATABASE_URL` (pooled,
  Lambda). Keep `CONN_MAX_AGE=0` and `DISABLE_SERVER_SIDE_CURSORS=True` (Neon PgBouncer).
- Budget: free tiers, under $10/month. Don't add paid services or new infra without asking.
- Before saying a task is done: run `ruff check .` and `python -m pytest`, and show
  `git status`.
- Never commit or push. Stage changes, show git status, suggest a commit message.
