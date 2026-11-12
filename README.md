# PR Review Explorer

Local-first tool that helps engineers who don't know a repository — its business, its architecture — understand a pull request without reading code: first a map of the whole system (business domains, capabilities, flows), then the PR laid over it, with business-step flows and architecture-consistency checks centred on how close each touched interface is to clients. The documents under `docs/` are written in Traditional Chinese. Spec: [docs/spec.md](docs/spec.md) · Structure: [docs/architecture.md](docs/architecture.md) · API: [docs/api.md](docs/api.md) · Repo config: [docs/repo-config.md](docs/repo-config.md)

## Run

Requires `git`, Python ≥ 3.12 with `uv`, Node ≥ 22.12 with `pnpm` (or `corepack`, which the Makefile falls back to). Optional: `gh` (logged in) for GitHub PR URLs; `claude` CLI for business explanations.

```bash
make setup   # install dependencies, build the UI and the TS analyzer
make start   # http://127.0.0.1:8766 — listens on all interfaces; HOST=127.0.0.1 make start for this machine only
```

Paste a GitHub PR URL, or open "Local repo mode" and give a repo path plus base/head refs. Analyses are kept in `$PR_EXPLORER_HOME` (default `~/.pr-explorer/`) and reused: the same PR is never re-analyzed, and a new PR reuses the system map's explanations for unchanged parts.

Try it without GitHub: `make demo`, then use local repo mode with the printed path, base `main`, head `feature/discount`.

Generated explanations are in Traditional Chinese by default, matching the UI; `PR_EXPLORER_LANG=en` writes English and `PR_EXPLORER_LANG=auto` follows the repository's own language (Chinese when its docs / PR are Chinese). Follow-up questions: the 「問 AI」 button (every tab) asks Claude about the PR you are reading; answers cite the analysis' evidence and are labelled as AI interpretation. `PR_EXPLORER_ASK=local` (default) lets only browsers on this machine ask, `all` lets every client ask, `off` hides the feature. `make start` runs the server in the background and prints its URL; output goes to `~/.pr-explorer/logs/server-<port>.log`. `make stop` stops the server. Other targets: `make test`, `make clean`, `make help`. `PORT=… make start` changes the port (stop it with the same `PORT=… make stop`).

## What it will and won't claim

- Structure (capabilities, flows, interfaces, rules) comes only from static analysis. Claude writes explanations, groups capabilities into business domains and cuts flows into phases — every statement must cite evidence or it is dropped, and these are labelled as Claude's.
- Dynamic calls are **unknown**; steps inside unconfirmed branches are **possible**; flows are reconstructions, never runtime traces.
- Claude's findings and architecture hypotheses are always **needs confirmation**.
- Written rules (`CLAUDE.md`, `AGENTS.md`, `.claude/rules/`, docs) are read from the PR's base commit; a PR that edits an instruction file is flagged for review. A suspected departure from a written rule is a hypothesis, never a verdict ([details](docs/repo-config.md#documented-rules-no-configuration)).
- PR code is parsed, never executed.
- Claude's text is interpretation, and PR content (title, description, code, comments) can influence it. Treat it as unverified.

## Data & privacy

- Everything is stored locally under `$PR_EXPLORER_HOME` (default `~/.pr-explorer/`): a bare git clone of each analyzed repository under `repos/` (this holds the **raw, unredacted source**), an SQLite index, and the result documents. Per-commit worktrees under `worktrees/` are kept for up to 10 minutes after an analysis (at most two per repository) so the next analysis can reuse them, then removed; leftovers are removed at the next server start.
- Result documents are scrubbed for common secret formats (tokens, keys, passwords) before they are written. This is pattern-based and best-effort, not a guarantee.
- `logs/semantic-calls.jsonl` records one line per Claude call: time, kind, tier, attempt, prompt size, duration, exit code, reply size. It never contains prompt text.
- With Claude enabled, the PR title and description, diff and code excerpts, repo docs and any context text you paste are sent to Claude through the local `claude` CLI (and so to the model service your CLI is signed in to). The initial prompt is scrubbed for secrets. Claude may also read any file inside the analyzed worktree during the call; those reads are **not** scrubbed.
- A follow-up question (「問 AI」) sends the question, a digest of the stored analysis, the PR diff and up to 6 earlier questions and answers of that PR to Claude the same way; the PR's head is checked out again for that one call (then kept idle like an analysis' worktrees). Questions and answers are stored (scrubbed) in the SQLite index.
- To keep everything on this machine, turn Claude off: choose "關閉（只做靜態分析）" under "Claude 業務說明" in the UI, or pass `--semantic off` to `pr-explorer analyze`.

## Network exposure

By default the server listens on `0.0.0.0:8766` with **no authentication**. Anyone who can reach the port can read all analysis results and can make this machine analyze any local repository path, or any private GitHub repository the local `gh` login can read, using the operator's GitHub and Claude identities. On untrusted networks run `HOST=127.0.0.1 make start`. Follow-up questions to Claude are allowed only from this machine unless `PR_EXPLORER_ASK=all`.

Requests whose `Host` header is not `localhost`, an IP address or this machine's hostname are rejected with 400 (DNS-rebinding protection). Allow extra names with `PR_EXPLORER_ALLOWED_HOSTS=name1,name2`; `*` disables the check.

## Sharing results for reuse

Analyses are cached in `$PR_EXPLORER_HOME` (default `~/.pr-explorer/`): the SQLite index `pr_explorer_v2.db` plus the result documents in `system-maps/` and `overlays/`. Copying these three to a teammate lets their server answer the same PR from cache, with no new Claude calls, and reuse the project map's explanations for later PRs of that repository.

A shared result is reused only when all of these match:

- The same version of this tool on both sides. A version change re-analyzes on purpose.
- The same Claude setting (off / light / standard / deep) and the same attached context documents.
- The same repository identity. Results of GitHub PR URLs are keyed by `owner/repo`, so they work on any machine. Results of local repositories are keyed by the repository's absolute path, so they are reused only if the teammate's checkout sits at the same path.

The teammate still needs read access to the repository, because the PR is resolved before the cache is checked. Share only `pr_explorer_v2.db`, `system-maps/` and `overlays/`. Do not share `repos/` (raw source) or `logs/`.

Export (sender). `.backup` takes a consistent copy even while the server is running:

```bash
H="${PR_EXPLORER_HOME:-$HOME/.pr-explorer}"; OUT=pr-explorer-share
mkdir -p "$OUT" && sqlite3 "$H/pr_explorer_v2.db" ".backup '$OUT/pr_explorer_v2.db'"
cp -R "$H/system-maps" "$H/overlays" "$OUT/" && tar czf pr-explorer-share.tgz "$OUT"
```

Import (receiver). Stop the server first. If you have never run the tool, `$H/pr_explorer_v2.db` does not exist yet; in that case just copy the three items into `$H`. Otherwise merge, keeping your own entries when both sides have the same one:

```bash
H="${PR_EXPLORER_HOME:-$HOME/.pr-explorer}"; IN=pr-explorer-share
tar xzf pr-explorer-share.tgz && mkdir -p "$H"
cp -Rn "$IN/system-maps" "$IN/overlays" "$H/"
sqlite3 "$H/pr_explorer_v2.db" "ATTACH '$IN/pr_explorer_v2.db' AS s;
  INSERT OR IGNORE INTO system_maps SELECT * FROM s.system_maps;
  INSERT OR IGNORE INTO overlays SELECT * FROM s.overlays;
  INSERT OR IGNORE INTO analyses SELECT * FROM s.analyses;
  INSERT OR IGNORE INTO stages SELECT * FROM s.stages;
  INSERT OR IGNORE INTO questions SELECT * FROM s.questions;"
```

Requires the `sqlite3` command-line tool (preinstalled on macOS). The `questions` line needs both sides on a version with follow-up questions (drop it if the sender's database has no `questions` table). The shared files contain the analyzed repository's business explanations, file and symbol names, PR titles and descriptions, follow-up questions and Claude's answers, and the sender's local repository paths. Share them only with people who may read that repository. Secret scrubbing is best-effort.
