#!/bin/zsh
# Khởi động app. Lần đầu ở máy mới: cài môi trường (cần uv) và tạo bảng giá từ data/sources/.
cd -- "$(dirname -- "$0")" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  uv sync --python 3.12 || exit 1
fi
.venv/bin/python import_catalog.py --all --missing-only || exit 1
exec .venv/bin/python app.py --port 8876 --heavy-model qwen3.8:27b
