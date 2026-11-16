#!/usr/bin/env bash
set -euo pipefail
ruff check src/ tests/
ruff format --check src/ tests/
mypy --strict src/
pytest tests/ --cov=src --cov-fail-under=100
deptry src/
bandit -r src/
pip-audit
