#!/usr/bin/env bash
set -euo pipefail

uv sync
codegraph init -i
uv run ruff check .
uv run pyright
uv run pytest