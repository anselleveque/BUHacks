# Contributing

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11 or newer
pip install -r requirements-dev.txt
cp .env.example .env                                  # optional keys, see below
python3 -m foodapp.build_db                           # builds data/foodapp.db
python3 -m foodapp.server --port 8000 --allow-now     # open http://127.0.0.1:8000/?now=23:45&demo=1
```

## Tests
```bash
python3 -m pytest -q
```
The suite needs no network and no API keys, and CI (`.github/workflows/tests.yml`) runs it on every push and pull request. Run it before you push; add a test with every fix.

## Who works on what
Read `AGENTS.md` first. It is the shared rulebook for people and coding agents (Claude Code, Codex):
- Claude Code plans, reviews and does multi-file work; Codex takes well-scoped tasks. People decide when plans disagree.
- One owner per file at a time: claim files in the "Claimed files" table of `docs/HANDOFF.md` before editing and remove the claim when done.
- One branch per agent and task (`claude/<task>`, `codex/<task>`), preferably in its own git worktree (`git worktree add ../<name> -b <branch>`). Commit small and often, rebase on `main` before merging, never overwrite someone else's work.
- Keep the demo path working, stay in scope, match the existing code style, and note unrelated problems in `docs/HANDOFF.md` instead of fixing them.
- When you stop, add a dated entry to `docs/HANDOFF.md`: done, next, files touched, how to verify, known issues.

## No secrets in git
API keys (`MBTA_API_KEY`, `ORS_API_KEY`, `MAPTILER_API_KEY`) live only in `.env`, which is git-ignored. Keep `.env.example` current with empty values, never print or commit a key, and do not commit `data/user.db`, `data/uploads/` or personal data. Use a GitHub noreply address as your git commit email (see `scripts/publish_public.sh`).
