UV ?= uv
export UV_CACHE_DIR ?= $(CURDIR)/.local/uv-cache
export UV_PYTHON_DOWNLOADS = never
COMPOSE = docker compose --project-name mgar-v3 --env-file .env -f deploy/compose.yaml -f deploy/compose.local.yaml

.PHONY: setup deps-up deps-stop migrate doctor verify lint typecheck test dev live-preflight install-github-mcp
setup:
	$(UV) sync --frozen
	$(UV) run --frozen python scripts/bootstrap.py
deps-up:
	$(COMPOSE) up -d --wait
deps-stop:
	$(COMPOSE) stop
migrate:
	$(UV) run --frozen python -m mgar_v3.store.migrate
doctor:
	$(UV) run --frozen mgar doctor
install-github-mcp:
	$(UV) run --frozen python scripts/install_github_mcp.py
live-preflight:
	$(UV) run --frozen mgar live-preflight --profile "$(PROFILE)"
verify:
	$(UV) run --frozen python scripts/verify.py --task "$(TASK)"
lint:
	$(UV) run --frozen ruff check src tests scripts
typecheck:
	$(UV) run --frozen mypy
test:
	$(UV) run --frozen pytest
dev:
	$(UV) run --frozen uvicorn mgar_v3.api:create_app --factory --host 127.0.0.1 --port 18080

.PHONY: rag-models rag-train rag-demo rag-eval data-demo
rag-models:
	$(UV) sync --frozen --extra rag-models
	$(UV) run --frozen --extra rag-models python scripts/install_rag_models.py
rag-train:
	$(UV) run --frozen --extra rag-models python scripts/r4.py train
rag-demo:
	$(UV) run --frozen --extra rag-models python scripts/r4.py seed
rag-eval:
	$(UV) run --frozen --extra rag-models python scripts/r4.py eval
data-demo:
	$(UV) run --frozen python scripts/r4.py data

.PHONY: r4-local
r4-local:
	$(UV) run --frozen --extra rag-models python scripts/r4_local.py

.PHONY: sandbox-build sandbox-build-r5 sandbox-build-r03 r5-demo
sandbox-build: sandbox-build-r03
sandbox-build-r5:
	$(UV) run --frozen python scripts/build_r5_sandbox.py
sandbox-build-r03:
	$(UV) run --frozen python scripts/build_r03_sandbox.py
r5-demo:
	$(UV) run --frozen python scripts/r5_demo.py

.PHONY: watchdog-setup watchdog-once
watchdog-setup:
	$(UV) run --frozen python scripts/setup_watchdog.py
watchdog-once:
	env -i PATH="$(PATH)" LANG=C.UTF-8 "$(CURDIR)/.venv/bin/python" -m mgar_v3.recovery.watchdog --config "$(CURDIR)/.local/watchdog/client.json" --once
