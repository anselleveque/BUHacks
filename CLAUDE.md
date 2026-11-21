# CLAUDE.md

@AGENTS.md

## Claude Code specifics
This repo is worked on by Claude Code and Codex together. The shared rules above (imported from `AGENTS.md`) apply to you. Additionally:

- **Role:** you are the planner/architect and reviewer by default. For non-trivial tasks, plan first (plan mode), then implement. Break large work into tasks that Codex can take in parallel, and write them in `docs/HANDOFF.md` with clear acceptance criteria.
- **Start of session:** read `docs/HANDOFF.md` and `git status` / `git log -5` to see what Codex or teammates changed.
- **End of session:** update `docs/HANDOFF.md` (done / next / files touched / how to verify / known issues).
- **Reviewing Codex's work:** check its diff against the task's acceptance criteria, run the tests, and fix or flag problems rather than silently rewriting.
- **Context hygiene:** use subagents for broad searches; keep the main conversation focused on the current task.
- **Do not edit `AGENTS.md` rules casually.** Changes to shared rules need the team's OK since Codex reads the same file.
