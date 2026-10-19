.PHONY: help install test lint fmt source review upload clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

install:  ## install the package and dev tools into .venv
	python3 -m venv .venv
	./.venv/bin/pip install -e ".[dev]"
	./.venv/bin/playwright install chromium

test:  ## run the test suite
	./.venv/bin/python -m pytest

lint:  ## check style and common bugs
	./.venv/bin/ruff check src tests

fmt:  ## autofix what ruff can
	./.venv/bin/ruff check --fix src tests

source:  ## phase 1 — source and verify images (LIMIT=10 to cap)
	./.venv/bin/python -m lowprice.pipeline $(if $(LIMIT),--limit $(LIMIT),)

review:  ## regenerate review.html from the manifest
	./.venv/bin/python -m lowprice.review

upload:  ## phase 2 — dry run by default; make upload DRY= to go live
	./.venv/bin/python -m lowprice.phase2.oms_uploader $(if $(DRY),--dry-run,) $(if $(LIMIT),--limit $(LIMIT),)

clean:  ## remove caches and build artifacts
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info src/*.egg-info
