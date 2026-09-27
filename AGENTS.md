# AGENTS.md

This project was built with the microsoft-foundry skill. Before working on or answering questions about foundry agents, read the microsoft-foundry skill first.

## What this repo is

A Phase 1 POC proving how far Microsoft Foundry **hosted agents** can go as a multi-agent
system: a supervisor plus research and analysis specialists, agent-card discovery, and
text / data / file payloads. See [README.md](README.md) for the measured platform behaviour
and the Phase 2 backlog.

## Conventions

- Infrastructure is owned by `infra/main.bicep` and deployed with `scripts/deploy-infra.ps1`;
  keep changes there idempotent (deterministic names, `guid()`-named role assignments).
- `src/_shared/` is the single source of truth for modules copied into every agent folder.
  Edit there, then run `./scripts/sync-shared.ps1` (CI can use `-Check`).
- After changing any agent's `pyproject.toml`, run `./scripts/lock-agents.ps1`. It normalises
  the registry and wheel/sdist URLs in `uv.lock` back to public PyPI, matching artifacts
  by exact SHA256. A private artifact URL can break `azd deploy` even when the registry
  looks public. Use `-Check` to verify public URLs and offline lock freshness.
- Keep each agent's `.azdignore` to `.env.example`. Extra patterns break the code package.
- Claims about transport behaviour belong in the proof harness, not in prose. If you change
  how parts are carried, update `scripts/prove_a2a_parts.py` so it still exits non-zero when
  the claim stops holding.
