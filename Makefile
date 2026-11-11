# PR Review Explorer — one-command setup and start.
#   make setup   install dependencies and build the UI + TS analyzer
#   make start   run the app (http://127.0.0.1:8766, reachable on the LAN)
HOST ?= 0.0.0.0
PORT ?= 8766

.PHONY: help setup start test demo clean check-tools

help:
	@echo "make setup   install deps, build frontend and ts-analyzer"
	@echo "make start   start the server on $(HOST):$(PORT) (HOST=127.0.0.1 for this machine only)"
	@echo "make test    run backend, frontend and ts-analyzer tests"
	@echo "make demo    build the sample Django repo under .data/fixtures/shop to try local mode"
	@echo "make clean   remove build outputs (keeps ~/.pr-explorer analysis data)"

check-tools:
	@for t in git uv node pnpm; do command -v $$t >/dev/null || { echo "missing: $$t"; exit 1; }; done
	@node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a==22&&b>=12)?0:1)' || { echo "Node >= 22.12 required"; exit 1; }
	@command -v gh >/dev/null || echo "note: gh not found — GitHub PR URLs won't work (local repo mode still does)"
	@command -v claude >/dev/null || echo "note: claude not found — business explanations will be unavailable"

setup: check-tools
	cd backend && uv sync
	cd tools/ts-analyzer && pnpm install --frozen-lockfile && pnpm build
	cd frontend && pnpm install --frozen-lockfile && pnpm build

# Builds the UI first if it is missing, so `make start` also works right after a fresh clone + `make setup`.
start:
	@test -f frontend/dist/index.html || $(MAKE) setup
	cd backend && uv run pr-explorer serve --host $(HOST) --port $(PORT)

test:
	cd backend && uv run pytest -q
	cd frontend && pnpm typecheck && pnpm test
	cd tools/ts-analyzer && pnpm test

demo:
	cd backend && uv run python tests/fixtures/make_django_fixture.py
	@echo "In the UI open 'Local repo mode': repo $(CURDIR)/.data/fixtures/shop, base main, head feature/discount"

clean:
	rm -rf frontend/dist tools/ts-analyzer/dist
