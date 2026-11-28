# Agent guide

Instructions for coding agents (Claude Code, Codex and others) and anyone new to the repo. Read
[README.md](README.md) first for what Truckroll is.

## Commands

| Where | Command |
|---|---|
| `csms/`, `control/` | `uv run pytest` (tests), `uvx ruff check src tests` (lint) |
| repo root | `docker compose up -d --build` (whole stack), `docker compose up -d --build --no-deps control` (one service), `docker compose down` |
| `site/` | `python -I site/capture.py` (screenshots from the live system) |

A change is done when lint and tests pass in every service it touches, and the stack still starts.

## Rules

- **The incident ledger is the eval's ground truth.** Nothing the triage agent can read (its context, its
  allow-listed queries) may include the ledger, scenario names or scenario events. Tests in
  `control/tests/test_agent_eval.py` guard this; a leak means the eval restarts from zero (LOG.md, 2026-11-25).
- **The incident engine knows nothing about scenarios.** Detection works only from what an operator would see.
- **Questions state their default.** Any new risky action goes through `decisions.py` with a deadline and a
  default; agent suggestions use the confidence rule in `incidents.py` (`AGENT_AUTO_APPROVE_PCT`). If you change
  it, update `docs/agent.md` and the README.
- **Timers are wall-clock and persisted.** State survives restarts (`persist.py`); store timestamps as wall time.
- **Only our services get rebuilt on deploy.** Rebuilding the simulator cuts every session.
- **No secrets in git.** Keys come from env vars (`.env`, gitignored). If a key appears in a chat or a file, stop
  and ask for it to be rotated.
- **Low voltage only.** Nothing connects to mains or a real EV.

## Logging what went wrong

`LOG.md` is part of the project. When a bug is found live, or a theory turns out wrong, add a short entry: what
happened, the real cause, the fix. Mistakes by the coding agent count too.

## Copy on the write-up page (`site/truckroll/index.html`)

No em dashes, curly apostrophes, and short stop-words glued to the next word with a non-breaking space.
