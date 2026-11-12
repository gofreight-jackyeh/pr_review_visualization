# `.pr-explorer.yml` — repository configuration (optional)

Place it at the repository root. PR Review Explorer reads it from the PR's **base** commit (so a PR that edits it
takes effect once merged) and uses it for both sides of the diff. Only the keys below are read; anything else is
reported in the system-map stage message and ignored.

```yaml
# Interfaces clients depend on (spec §8 outer ring). Config wins over automatic detection.
outer:
  paths:                 # files whose output leaves the system (outbound formats), or whose routes are client-facing
    - orders/notifications.py
    - integrations/partner/**
  routes:                # URL globs of Django / DRF routes that are public even without an /api prefix
    - /partner/**
  packages:              # package names treated as published even if marked private
    - shop-sdk
exclude_outer:           # drop automatically detected outer interfaces
  paths: [legacy/**]
  routes: [/api/internal/**]
  packages: [shop-dev-tools]
```

- Globs: `*` matches within one path segment, `**` any depth (`**/x` also matches `x`); routes match with or without the trailing slash.
- Lists may be block (`- item`) or inline (`[a, b]`); a bare list directly under `outer:` means `paths`.
- Interfaces declared here are marked `detected_by: config`; their evidence cites the line in this file.

What is detected without configuration (`backend/src/pr_explorer/domain/services/rings.py`):

- **HTTP API** — DRF views (APIView / ViewSet / `@api_view`) and routes under `/api` or with an `api` path segment
  (`API_PREFIXES`, `API_SEGMENT`); contract = allowed HTTP methods + serializer fields (read-only / required).
- **Web app routes** — Next.js `pages/` and `app/**/page.*`, SvelteKit `src/routes/**/+page.svelte`, Nuxt `pages/`,
  and `path: "…"` entries in router files (file name contains "route").
- **Published packages** — `package.json` not marked `"private": true` (or with `publishConfig`), `pyproject.toml` /
  `setup.cfg` without the `Private :: Do Not Upload` classifier; contract = top-level exports of the entry module.
- **Outbound formats** — paths matching `OUTBOUND_PATTERNS` (EDI, webhooks, e-mail / notification templates).

## Documented rules (no configuration)

Repository documents are read from the PR's **base** commit too (`backend/src/pr_explorer/infrastructure/knowledge/local_docs.py`),
so a PR cannot rewrite the rules that judge it:

- **Instruction files** — `CLAUDE.md` and `AGENTS.md` at any depth (a nested one applies to its directory), `.claude/CLAUDE.md`
  and `.claude/rules/**/*.md`. Every bullet, numbered item and normative line counts as a rule (YAML front matter is skipped).
  No other dot-directory is read. Claude may raise a *needs confirmation* hypothesis when a changed file seems to depart
  from one, citing the rule's `file:line`.
- **Other docs** — `README`, `CONTRIBUTING`, `CONVENTIONS`, `STYLEGUIDE`, `ARCHITECTURE`, `docs/**`, ADRs: only lines under
  convention-like headings or saying must / should / never.
- A PR that adds, edits or deletes an instruction file gets a static finding (`documented_rules_changed`).
- Instruction rules are chosen per PR, with their own budget (40 rules / 12,000 characters, plus up to 60 scoped ones):
  a nested `CLAUDE.md` / `AGENTS.md` is used only when the PR changes a file under its directory, and then all its rules
  are kept; a `.claude/rules` file with `paths:` / `globs:` front matter likewise only when a changed path matches. The
  rest are ranked by how many words of the changed paths and touched capability names appear in the rule file's path,
  its headings and the rule itself. What is left out is listed in the `semantic` stage message.
- A Claude finding that cites such a rule is shown 「依據已記錄的規則：file:line」 and raised one severity level.
- Symlinked docs are never followed; at most 25 other-doc rules and 16,000 characters of excerpts are used.
