#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  uv sync --python 3.12 || exit 1
fi
exec .venv/bin/python mcp_server.py
