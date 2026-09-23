.DEFAULT_GOAL := help
.PHONY: help sync run run-quick test lint format check build install uninstall models summarize reindex clean

ARGS ?=

help: ## Show this help
	@echo "Usage: make <target> [ARGS='...']"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync: ## Create/update .venv with runtime + dev dependencies
	uv sync

run: ## Run from source (extra options via ARGS='--lang sv')
	uv run mic2md $(ARGS)

run-quick: ## Run with a small model and no Ollama pass
	uv run mic2md --no-llm -m base.en $(ARGS)

test: ## Run the unit tests
	uv run pytest

lint: ## Check code style with ruff
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-format and fix lint issues
	uv run ruff format .
	uv run ruff check --fix .

check: lint test ## Lint + tests (run before committing)

build: ## Build wheel and sdist into dist/
	uv build

install: ## Install/update the global `mic2md` command
	uv tool install --reinstall .

uninstall: ## Remove the global `mic2md` command
	uv tool uninstall mic2md

models: ## List Whisper models and which are downloaded
	uv run mic2md models

summarize: ## Add meeting notes to a session file: make summarize FILE=path/to/session.md
	@test -n "$(FILE)" || { echo "Usage: make summarize FILE=path/to/session.md"; exit 1; }
	uv run mic2md summarize "$(FILE)" $(ARGS)

reindex: ## Rebuild index.md in the output folder (moves old flat files into place)
	uv run mic2md reindex $(ARGS)


clean: ## Remove build artifacts and caches
	rm -rf dist build .pytest_cache .ruff_cache
	find . -path ./.venv -prune -o -name __pycache__ -type d -exec rm -rf {} +
