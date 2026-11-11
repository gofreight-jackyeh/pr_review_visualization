# HTTP API (v2)

Backend listens on `0.0.0.0:8766` by default (all interfaces; `pr-explorer serve --host 127.0.0.1` for this machine only). There is no authentication. All bodies are JSON.

Document shapes: `schemas/artifact/v2/artifact.schema.json` — a `system_map` (whole repo at one commit, shared by every PR on that commit) and a `pr_overlay` (one PR over a system map).

There is no source-code endpoint: evidence is shown as `path:line` (spec v2 §4).

## POST /api/analyses

Start (or reuse) an analysis.

```json
{
  "pr_url": "https://github.com/owner/repo/pull/123",
  "local": { "repo_path": "/abs/path", "base": "main", "head": "feature-x", "title": "optional", "body": "optional" },
  "context_docs": [{ "name": "jira-ABC-1.md", "content": "..." }],
  "semantic_mode": "auto"
}
```

- Exactly one of `pr_url` / `local` is required (400 otherwise).
- `semantic_mode`: `auto | off | light | standard | deep` (default `auto`). It controls the PR explanation tier; the system map explanation runs whenever semantic_mode is not `off`.
- Overlay cache key: `(repo, merge_base_sha, head_sha, analyzer_version, schema_version, semantic_mode, context_docs hash)`. A hit responds immediately with `"cached": true`, `status: "done"`.
- System map cache key: `(repo, merge_base_sha, analyzer_version, schema_version, semantic_on)`. Reused across PRs.
  On a miss the static map is rebuilt for the merge base, and Claude texts are carried over from the nearest stored
  2.x map of the same repo (any analyzer version; preferably one whose commit is an ancestor of the merge base):
  a capability keeps its texts when its fingerprint (git blob SHAs of its files + its element/step IDs) is unchanged,
  and every carried-over text must still cite existing evidence. Only changed or new capabilities go to Claude
  (bounded parallel calls); the system summary is re-asked when more than 25% of capabilities changed.
  Concurrent analyses needing the same map share one build (per server process).
  Schema 2.1.0 adds two Claude steps after the capability texts, run in parallel: the domain assignment (reused when
  the donor's set of business capabilities is identical; otherwise only this step is re-asked) and flow phases
  (reused per flow when its step IDs are unchanged). Stored 2.0.0 maps are valid donors: they simply have no domains
  or phases. Engineering capabilities are not sent to Claude. Any Claude reply that is unparseable or wrongly shaped
  is retried once before the step is marked failed; a rejected domain assignment is retried once with the
  validator's problems, then the static domains stay (one per top-level directory; in a system of ≥ 12 business
  capabilities where one directory holds more than half, its sub-directories instead; at most 10, the smallest merged).
- Architecture rules (map) are import-linter contracts from `.importlinter`, `.importlinter.ini`, `setup.cfg` or
  `pyproject.toml` plus inferred statistics; overlay `architecture_checks` apply them to the PR's added edges.
- Schema 2.2.0 (spec §8 interface rings): the map lists `interfaces` (outer = public HTTP API with serializer fields,
  web app routes, published package exports, outbound formats; middle = module APIs used by other capabilities or
  across an import-linter layer boundary, task signatures). The overlay adds one `interface_change` check per changed
  interface (`origin: observed`; added → compatible, statically certain breaks → breaking/verified, the rest →
  unknown/needs confirmation), checks ordered outer → middle → inner; an API handler that returns a file (its
  module or a module it imports writes CSV / spreadsheets, or names a file content type) is diffed by the column keys
  of the column / header / field definitions in those modules that the PR changed (added → compatible, removed or
  renamed → breaking, changed without key changes → unknown naming the file); head-only interfaces go to
  `head_additions.interfaces`. A repository can declare or exclude outer interfaces in `.pr-explorer.yml`
  (format: [repo-config.md](repo-config.md)). Large capabilities split one level (≤ 12 children, small
  sub-directories merged into `<parent>/*`).
- Worktrees are removed when an analysis ends (the bare repo cache stays); `serve` prunes leftovers at start.
- To decide cache hits, POST resolves SHAs synchronously (local: `git rev-parse`; GitHub: `gh` + `git fetch`). Resolution failure → 400 `acquire_failed`.
- CORS allows only `http://localhost:*` / `http://127.0.0.1:*` origins (Vite dev server).

Response `202` (or `200` when cached):

```json
{ "analysis_id": "a_…", "status": "queued", "cached": false }
```

## GET /api/analyses

Recent analyses, newest first: `[{ analysis_id, status, title, repo, head_sha, created_at, overlay_id, system_map_id }]`.

## GET /api/analyses/{analysis_id}

```json
{
  "analysis_id": "a_…",
  "status": "queued | running | done | failed",
  "stages": [{ "name": "acquire", "status": "pending | running | done | failed | skipped", "message": "" }],
  "error": null,
  "overlay_id": "ov_…",
  "system_map_id": "sm_…"
}
```

Stage names, in order: `acquire`, `system_map`, `pr_static`, `flows`, `impact`, `semantic`, `assemble`.
`system_map` reports `skipped` with message `reused <system_map_id>` on a cache hit (or when it joined another
analysis' in-flight build). It stores the static map; the map's Claude texts are written in the background and the
`semantic` stage waits for them before the PR explanation.

**Progressive results.** `overlay_id` / `system_map_id` are exposed as soon as the static documents are stored, while
`status` stays `running` until Claude is done. Until then those documents carry `semantic.status: "pending"`
(structure, flows, impacts and static findings are complete; business texts may be missing); when the analysis is
`done` the final documents have replaced them under the same IDs. Without Claude (`semantic_mode: off`, tier none)
the documents are stored once, at the end.

## GET /api/system-maps/{system_map_id}

Returns the system map JSON.

## GET /api/overlays/{overlay_id}

Returns the PR overlay JSON. Its `system_map_id` names the map to load with it.

## GET /api/overlays/{overlay_id}/freshness

Re-resolves the PR head (GitHub: `gh`; local: `git rev-parse <head>`).

```json
{ "stale": true, "analyzed_head_sha": "…", "current_head_sha": "…" }
```

## Errors

`{ "error": { "code": "invalid_input | not_found | acquire_failed | internal", "message": "…" } }`
