#!/bin/zsh
# Chạy MCP server (stdio). Model lấy từ local.env do install.sh tạo.
cd -- "$(dirname -- "$0")" || exit 1
[[ -x .venv/bin/python ]] || ./install.sh --yes >&2 || exit 1
if [[ -f local.env ]]; then
  source local.env
  export LOCAL_LIGHT_MODEL="${LOCAL_LIGHT_MODEL:-$LIGHT_MODEL}" LOCAL_HEAVY_MODEL="${LOCAL_HEAVY_MODEL:-$HEAVY_MODEL}"
fi
exec .venv/bin/python mcp_server.py
