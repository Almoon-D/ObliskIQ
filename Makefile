# ObliskIQ developer commands. Everything runs inside the uv-managed environment, against
# the locked dependency set (uv.lock).

UV_RUN := uv run --locked

.PHONY: setup check test fmt lint types imports format-check coverage

setup:  ## Install the locked environment (uv-managed Python 3.14) and the pre-commit hooks
	uv sync --locked
	$(UV_RUN) pre-commit install

check: format-check lint types imports coverage  ## Everything CI runs

test:  ## pytest only
	$(UV_RUN) pytest

fmt:  ## Format the code
	$(UV_RUN) ruff format

lint:
	$(UV_RUN) ruff check

types:
	$(UV_RUN) mypy

imports:
	$(UV_RUN) lint-imports

format-check:
	$(UV_RUN) ruff format --check

coverage:  ## pytest with the coverage floor from pyproject.toml
	$(UV_RUN) pytest --cov --cov-report=term-missing
