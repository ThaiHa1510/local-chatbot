#!/usr/bin/env bash
# Chạy app. Cấu hình đọc từ local.env (do install.sh tạo); chưa cài thì gọi install.sh.
set -euo pipefail
cd -- "$(dirname -- "$0")"
[[ -x .venv/bin/python && -f local.env ]] || ./install.sh
source local.env
.venv/bin/python import_catalog.py --all --missing-only | grep -v '^  ' || true
if [[ ${WITH_MODELS:-1} == 1 ]] && ! curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  if command -v ollama >/dev/null; then
    echo "Khởi động Ollama nền…"; nohup ollama serve >data/ollama.log 2>&1 &
    for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
  else
    echo "[!] Ollama chưa chạy — app vẫn mở được ở chế độ tra cứu."
  fi
fi
exec .venv/bin/python app.py --port "$PORT" --model "$LIGHT_MODEL" --heavy-model "$HEAVY_MODEL"
