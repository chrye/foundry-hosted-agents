# AGENTS.md

This project was built with the microsoft-foundry skill. Before working on or answering questions about foundry agents, read the microsoft-foundry skill first.

## What this repo is

A Sprint 1 POC proving how far Microsoft Foundry **hosted agents** can go as a multi-agent
system: a supervisor plus research and analysis specialists, agent-card discovery, and
text / data / file payloads (JSON/CSV/Excel over Responses; the tested Foundry A2A path
accepts text only).
See [README.md](README.md) for the measured platform behaviour and the Sprint 2 backlog.

## Conventions

- Infrastructure is owned by `infra/main.bicep` and deployed with `scripts/deploy-infra.ps1`;
  keep changes there idempotent (deterministic names, `guid()`-named role assignments).
- `src/_shared/` is the single source of truth for modules copied into every agent folder.
  Edit there, then run `./scripts/sync-shared.ps1` (CI can use `-Check`).
- After changing any agent's `pyproject.toml`, run `./scripts/lock-agents.ps1`. It normalises
  the registry and wheel/sdist URLs in `uv.lock` back to public PyPI, matching artifacts
  by exact SHA256. A private artifact URL can break `azd deploy` even when the registry
  looks public. Use `-Check` to verify public URLs and offline lock freshness.
- Keep the working `.azdignore` (`.env.example` only) unless packaging and deployment are
  revalidated. Previous additions broke the tested code-deployment workflow.
- Ground documentation claims in code and dated results. Separate configured behaviour,
  measured capabilities and backlog items; a passing expected-rejection test is not support.
- If part transport or workbook behaviour changes, update `scripts/prove_a2a_parts.py`,
  `scripts/prove_excel.py` and the relevant offline tests. Preserve failure exits.
