#!/bin/bash
set -e

echo "Phase 0 Workspace Setup (uv standard)..."

if [ -f "uv.lock" ]; then
    uv sync --locked
elif [ -f "pyproject.toml" ]; then
    uv sync || uv pip install -e .
elif [ -f "requirements.txt" ]; then
    uv venv .venv && uv pip install -r requirements.txt
fi

echo "Phase 0 Setup Complete."
