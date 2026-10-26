# Argus — every target runs through uv so the pinned toolchain is used.
SHELL := /bin/bash
.DEFAULT_GOAL := help
UV ?= uv
PY := $(UV) run
PYTEST := $(PY) pytest
PI_HOST ?= argus-pi
CSS_IN := argus/web/static/src/app.css
CSS_OUT := argus/web/static/dist/app.css

.PHONY: help setup dev css css-watch lint fmt types test-unit test-int cov test-e2e test-a11y \
        test-visual visual-update visual-linux secrets evals check check-ci deploy logs mac mac-test clean

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

setup: ## Install deps, browsers, Tailwind CLI, git hooks
	@command -v gitleaks >/dev/null || { echo 'gitleaks not found: brew install gitleaks (or see docs/runbook.md)'; exit 1; }
	$(UV) sync --frozen
	$(PY) playwright install chromium
	./scripts/fetch_tailwind.sh
	$(PY) pre-commit install
	$(MAKE) css

dev: css ## Run the dashboard with reload on :8000
	$(PY) argus serve --reload

css: ## Build the minified stylesheet
	@./scripts/fetch_tailwind.sh
	bin/tailwindcss -i $(CSS_IN) -o $(CSS_OUT) --minify

css-watch: ## Rebuild CSS on change
	bin/tailwindcss -i $(CSS_IN) -o $(CSS_OUT) --watch

lint: ## Ruff lint + format check
	$(PY) ruff check .
	$(PY) ruff format --check .

fmt: ## Auto-format and fix
	$(PY) ruff format .
	$(PY) ruff check --fix .

types: ## mypy --strict on argus/
	$(PY) mypy

test-unit: ## Unit + property + time tests
	$(PYTEST) tests/unit -q

test-int: css ## Integration tests (store, collectors, vault git, pipeline, webhook)
	$(PYTEST) tests/integration -q

cov: css ## Unit + integration with coverage floors (the web tests read the built CSS)
	$(PYTEST) tests/unit tests/integration -q \
	  --cov --cov-report=term-missing:skip-covered --cov-report=json
	$(PY) python scripts/coverage_floors.py

test-e2e: css ## Browser end-to-end flows
	$(PYTEST) tests/e2e -q -m "e2e and not a11y"

test-a11y: css ## axe-core on every page, both themes
	$(PYTEST) tests/e2e -q -m a11y

test-visual: css ## Screenshot regression against committed baselines
	$(PYTEST) tests/visual -q

visual-update: css ## Re-record visual baselines for this platform
	ARGUS_UPDATE_SNAPSHOTS=1 $(PYTEST) tests/visual -q

visual-linux: ## Record/verify Linux baselines in the pinned Playwright container
	./scripts/visual_linux.sh

evals: ## AI evals against the real API (needs ARGUS_ANTHROPIC_API_KEY; not in CI)
	$(PYTEST) tests/evals -q -m evals --run-evals

secrets: ## gitleaks over the full git history
	gitleaks git --redact --no-banner .

check-ci: lint types secrets cov test-e2e test-a11y ## What CI runs: check minus visual until the Linux baselines are re-recorded
	@echo "make check-ci: all green"

check: check-ci test-visual ## Everything, including visual against this platform's baselines
	@echo "make check: all green"

deploy: ## Rsync to the Pi and run the installer (PI_HOST=argus-pi)
	./deploy/deploy.sh $(PI_HOST)

logs: ## Tail Argus logs on the Pi
	ssh $(PI_HOST) 'journalctl -f -u "argus-*"'

mac: ## Generate the Mac widget's Xcode project (needs xcodegen; D071)
	cd mac && xcodegen generate --quiet

mac-test: mac ## Swift tests for the Mac widget (countdown rule, feed decoding)
	xcodebuild -project mac/Argus.xcodeproj -scheme ArgusBar -destination 'platform=macOS' \
		-derivedDataPath mac/build -quiet test

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov coverage.json .coverage $(CSS_OUT)
