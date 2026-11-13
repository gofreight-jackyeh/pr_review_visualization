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
  "semantic_mode": "auto",
  "force": false,
  "ai": { "engine": "codex", "model": "gpt-6.1-sol", "effort": "high" }
}
```

- Exactly one of `pr_url` / `local` is required (400 otherwise).
- `semantic_mode`: `auto | off | light | standard | deep` (default `auto`). It controls the PR explanation tier; the system map explanation runs whenever semantic_mode is not `off`.
- `ai` (optional): which AI writes this PR's texts and answers its 「問 AI」 questions. `engine` is `claude` or `codex` and must be one the server enabled (`PR_EXPLORER_ENGINES`, else 400); `model` is a model name of that engine (letters, digits, `. _ : / - [ ]`, ≤100); `effort` is `low | medium | high | xhigh | max`. Missing parts fall back to the server default for the same engine, then to that CLI's own settings. The system map always uses the server default. Choosing `codex` sends the PR's code and analysis data to OpenAI. The overlay records the choice in `semantic.ai` (with the model that answered, when the CLI reports it).
- `force` (optional, default `false`): retry. Skips the overlay cache hit and runs the pipeline again for the same request (always `202`, `cached: false`); the new overlay replaces the cached one under the same `overlay_id`. The system map is reused when its Claude step completed (`done` / `skipped`); a map whose Claude step failed, was unavailable or only partly explained is rebuilt. A second forced request for a key that is already being re-run returns the running analysis. The client must resend the request body (attached `context_docs` are not stored); a different `semantic_mode` or docs set is a different key.

`POST /api/overlays/{overlay_id}/regenerate` (no body): the UI's 「重新產生 AI 說明」. The server repeats the stored request that produced that overlay (same PR, same `semantic_mode`, same `ai`) with `force` and answers like `POST /api/analyses` (`202`, `cached: false`). `404` for an unknown overlay; `400` when that analysis had attached `context_docs` (they are not stored — start it again from the home page).
- Overlay cache key: `(repo, merge_base_sha, head_sha, analyzer_version, schema_version, semantic_mode, context_docs hash, AI profile)`; the AI profile (engine / model / effort) is empty for Claude with default settings. A hit responds immediately with `"cached": true`, `status: "done"`.
- System map cache key: `(repo, merge_base_sha, analyzer_version, schema_version, semantic_on)`. Reused across PRs.
  On a miss the static map is rebuilt for the merge base, and Claude texts are carried over from the nearest stored
  2.x map of the same repo (any analyzer version; preferably one whose commit is an ancestor of the merge base):
  a capability keeps its texts unless its business-facing surface changed (analyzer 2.6.0 / schema 2.5.0: name/path,
  entry points, entities and their fields, interfaces, built flows and their step structure, relations — stored per
  capability as `text_basis.surface`; a change to implementation only keeps the texts; donors stored before 2.5.0 use
  the earlier rule, git blob SHAs of its files + its element/step IDs). Every carried-over text must still cite
  existing evidence: line-based evidence IDs whose cited lines only moved are remapped with the zero-context diff
  between the donor's commit and the merge base; when the cited lines themselves changed, or the file moved or was
  deleted, only that element (entity, entry point, or flow with its steps) is re-asked, in the same batched calls.
  Staleness guard (`PipelineConfig`): a capability is re-explained as a whole once more than
  `reuse_max_changed_share` (0.4) of its code lines changed since its texts were written (summed across base
  changes, `text_basis.changed_lines`), once they were carried across more than `reuse_max_base_changes` (40) base
  commits, or once they are older than `reuse_max_age_days` (14). Only those capabilities go to Claude (bounded
  parallel calls; with several batches, the capabilities the PR's files belong to go first, in a batch of their own);
  the system summary is re-asked when more than 25% of capabilities are re-explained as a whole.
  Concurrent analyses needing the same map share one build (per server process). The PR call (b) does not wait for
  the whole map: it starts once the calls writing what it shows (names of the elements it lists, step labels of its
  flows, the system summary) returned (`pr_early_start`); each capability call only writes what it was asked about,
  so those texts are final then. Its prompt is the same as when waiting; if the stored map ends up different (e.g. a
  validation failure), or a call it needs failed, the PR call waits for / is repeated with the final map as before.
  Schema 2.1.0 adds two Claude steps after the capability texts, run in parallel: the domain assignment (reused when
  the donor's set of business capabilities is identical; otherwise only this step is re-asked) and flow phases
  (reused per flow when its step IDs are unchanged). Stored 2.0.0 maps are valid donors: they simply have no domains
  or phases. Engineering capabilities are not sent to Claude. Every Claude call passes `--json-schema` with its reply
  shape (structured output; a CLI that rejects the flag is called without it). Any Claude reply that is unparseable or
  wrongly shaped is retried once before the step is marked failed; a rejected domain assignment is retried once with the
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
- Worktrees stay idle for up to 10 minutes after an analysis ends (at most two per repository; the next analysis
  resets one to its commit instead of a full checkout), then are removed; the bare repo cache stays; `serve` prunes
  leftovers at start.
- To decide cache hits, POST resolves SHAs synchronously (local: `git rev-parse`; GitHub: `gh` + `git fetch`). Resolution failure → 400 `acquire_failed`.
- CORS allows only `http://localhost:*` / `http://127.0.0.1:*` origins (Vite dev server).

Response `202` (or `200` when cached):

```json
{ "analysis_id": "a_…", "status": "queued", "cached": false }
```

## GET /api/analyses

Recent analyses, newest first: `[{ analysis_id, status, title, repo, head_sha, created_at, overlay_id, system_map_id, queue_position }]`; `queue_position` is 1, 2, … for `queued` analyses (1 = next to start), otherwise `null`. Analyses run `PR_EXPLORER_WORKERS` at a time (default 2, 1–32). JSON responses are gzip-compressed when the client sends `Accept-Encoding: gzip`.

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

Returns the PR overlay JSON. Its `system_map_id` names the map to load with it. A Claude finding's `rule` is
`semantic_review` or `semantic_review:<focus>` (`human_decision | release_dependency | public_contract |
responsibility_boundary | convention | claim_needs_proof`); its `refs` are the changed lines it cites itself when it
cites any (`file:<path>:<start>-<end>`, inside a changed hunk ± 3 lines, at most 30 lines).

## GET /api/overlays/{overlay_id}/freshness

Re-resolves the PR head (GitHub: `gh`; local: `git rev-parse <head>`).

```json
{ "stale": true, "analyzed_head_sha": "…", "current_head_sha": "…" }
```

## GET /api/config

What this server allows the calling client to do.

```json
{ "ask": { "mode": "local | all | off", "allowed": true, "reason": null },
  "ai": { "default": { "engine": "claude", "effort": "high" },
          "engines": [{ "engine": "claude", "efforts": ["low", "medium", "high", "xhigh", "max"], "models": ["opus", "sonnet", "haiku"] },
                      { "engine": "codex", "efforts": ["low", "medium", "high", "xhigh", "max"], "models": ["gpt-6.1-sol"] }] } }
```

`ai` lists the engines a request may choose (`POST /api/analyses` `ai`) with suggested model names (Claude's aliases;
Codex's own cached model list), and the server default (`PR_EXPLORER_ENGINE`, `PR_EXPLORER_<ENGINE>_MODEL` /
`_EFFORT`; absent parts = the CLI's own settings).

`mode` is `PR_EXPLORER_ASK` (default `local`). `allowed` is computed for this request: `local` allows only clients whose
TCP peer address is loopback (127.0.0.0/8, ::1), `all` allows every client, `off` allows nobody. When not allowed,
`reason` names the setting. The UI hides 「問 AI」 for `off` and shows earlier answers read-only when `allowed` is false.

## Questions (follow-up questions to Claude about one overlay)

### POST /api/overlays/{overlay_id}/questions

```json
{ "question": "1–2000 characters", "selected_id": "optional id of the item the reviewer has selected" }
```

Response `202`: `{ "question_id": "q_…", "status": "queued" }`. The answer is written in the background (same executor
as analyses); poll the list below.

- 403 `forbidden` when the access mode does not allow this client (the message names `PR_EXPLORER_ASK`).
- 409 `conflict` while another question about the same overlay is queued or running (one Claude call at a time per PR).
- 404 `not_found` for an unknown overlay; 400 `invalid_input` for an empty / too long question or `selected_id` > 300 chars.

One question = one Claude call (call kind `ask`, system prompt `prompts/ask.md`, tier `standard`, same read-only
isolation flags as the analysis calls, same output language setting `PR_EXPLORER_LANG`). Inside the untrusted-data fence
it gets: the question (marked as the one to answer), a compact digest of the stored overlay (PR title/description,
purpose, behavior changes, findings, architecture checks, flows touched with their changed steps, changed files with
their evidence IDs, capability names; at most 40 000 characters, long lists halved to fit), the selected item, the last
6 answered questions of this overlay, and the PR diff (code before tests, at most 30 000 characters). For the duration of
that call the PR's worktrees are materialized again from the git cache (`repos/`) and Claude may Read the head
checkout; they are released (left idle, see above) before the answer is published. A result shared from another machine without the git cache
fails with a message to analyze the PR locally first.

The reply must be `{"answer": "<markdown>", "cites": ["file:…", "ev:…", "flow:…", …]}` (retried once when it is not).
Cites are kept only when they are IDs of that overlay or its system map (elements, steps, findings, architecture
checks, behavior changes, evidence, changed files, `pr`) or `file:<changed path>:<line>[-<end>]`; the rest are listed in
`dropped`. An answer with no valid cite is kept with `unsupported: true` (the UI shows 「未附來源，請自行確認」). The
question and the answer are scrubbed for secrets before they are stored.

### GET /api/overlays/{overlay_id}/questions

Oldest first. 403 when the mode is `off`; readable by every client otherwise.

```json
[{
  "question_id": "q_…", "overlay_id": "ov_…", "created_at": "…", "question": "…", "selected_id": "cap:orders",
  "status": "queued | running | done | failed", "error": null,
  "answer": {
    "text": "markdown", "unsupported": false, "dropped": ["ev:invented"],
    "cites": [{ "id": "file:orders/services.py:20", "kind": "file", "label": "orders/services.py",
                "refs": [{ "side": "head", "path": "orders/services.py", "start_line": 20 }] }]
  }
}]
```

`kind`: `file | evidence | flow | step | capability | entity | entry_point | component | interface | finding | arch |
behavior | pr`. `refs` are code locations (a changed file without a line has no `start_line`). Questions still queued or
running when the server stops are marked failed at the next start.

## Errors

`{ "error": { "code": "invalid_input | not_found | acquire_failed | forbidden | conflict | internal", "message": "…" } }`
