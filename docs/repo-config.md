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
