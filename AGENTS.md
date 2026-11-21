# AGENTS.md

Shared instructions for every AI coding agent in this repo (Codex, Claude Code). This is the single source of truth; `CLAUDE.md` imports it. Keep it short and edit it as the project evolves.

## Project
- **What:** <one-sentence description of the hackathon project>
- **Event:** BU hackathon. Time is the scarcest resource: favor a working demo over polish.
- **Stack:** <e.g. Next.js + TypeScript / FastAPI + Python / etc.>
- **Demo goal:** <the one flow that must work on stage>

## Commands
Fill these in as soon as they exist. Agents should run them rather than guess.
- Install: `<cmd>`
- Dev server: `<cmd>`
- Test: `<cmd>`
- Lint/format: `<cmd>`

## Working rules (both agents)
1. **Demo first.** Build the thinnest end-to-end path, then widen. Don't refactor or add abstractions without a concrete need.
2. **Small, verifiable steps.** Run the app or tests after each change. Never report something as working without running it.
3. **Stay in scope.** Do only the task you were assigned. Note unrelated problems in `docs/HANDOFF.md` instead of fixing them.
4. **No secrets in git.** Keys live in `.env` (gitignored); keep `.env.example` current. Never print or commit keys.
5. **Ask before** adding heavy dependencies, changing the data model, deleting files, or force-pushing.
6. **Match existing style.** Follow the conventions already in the code.

## Running in tandem: ownership and coordination
Two agents editing the same files causes conflicts, so split by area.

| Area | Default owner |
|---|---|
| Planning, architecture, multi-file refactors, debugging, code review | Claude Code |
| Well-scoped feature tasks, boilerplate, tests, scripts, parallel side tasks | Codex |

Adjust this table to the team's actual split. Rules:
- **One owner per file at a time.** Before editing, check `docs/HANDOFF.md` for the "Claimed files" section; add your claim, remove it when done.
- **Separate branches or worktrees.** Each agent works on its own branch (`claude/<task>`, `codex/<task>`). Prefer `git worktree add ../<name> -b <branch>` so agents never share a working directory.
- **Commit small and often** with clear messages. Rebase on `main` before opening a PR or merging; resolve conflicts, don't overwrite the other agent's work.
- **Hand off through `docs/HANDOFF.md`.** When you stop, write: what you did, what's next, files touched, how to run/verify, known issues. Read it at the start of every session.
- **Humans decide.** If two agents' plans disagree, stop and ask the team rather than picking silently.

## Definition of done
- Runs locally without errors on the demo path.
- Tests/lint pass (or the failure is documented in HANDOFF).
- `README.md` run instructions still accurate.
- Handoff note updated.
