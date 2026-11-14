#!/usr/bin/env bash
# Cài đặt trọn gói Local Support Agent trên máy mới (macOS / Linux).
#
#   ./install.sh                 # cài mọi thứ, hỏi trước khi cài uv/Ollama
#   ./install.sh --yes --start   # không hỏi, cài xong chạy app luôn
#
# Tuỳ chọn:
#   --light MODEL   model Ollama nhẹ   (mặc định: qwen:latest)
#   --heavy MODEL   model Ollama nặng  (mặc định: qwen3.8:27b — guardrails cũng dùng model này)
#   --port N        cổng web           (mặc định: 8876)
#   --no-models     bỏ qua Ollama/model (app vẫn chạy chế độ tra cứu, bỏ chọn "Kiểm tra bằng mô hình local")
#   --demo-prices   tính hạn bảng giá từ hôm nay (chỉ để demo dữ liệu crawl cũ)
#   --yes           tự đồng ý mọi câu hỏi
#   --start         chạy app ngay sau khi cài
set -euo pipefail
cd -- "$(dirname -- "$0")"

LIGHT_MODEL="qwen:latest"; HEAVY_MODEL="qwen3.8:27b"; PORT=8876
WITH_MODELS=1; DEMO_PRICES=0; ASSUME_YES=0; START=0
# Giữ lựa chọn của lần cài trước (nếu có).
if [[ -f local.env ]]; then source local.env; WITH_MODELS=1; fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --light) LIGHT_MODEL="$2"; shift ;;
    --heavy) HEAVY_MODEL="$2"; shift ;;
    --port) PORT="$2"; shift ;;
    --no-models) WITH_MODELS=0 ;;
    --demo-prices) DEMO_PRICES=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --start) START=1 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Tuỳ chọn không hợp lệ: $1 (xem --help)"; exit 2 ;;
  esac
  shift
done

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }
ask() {  # ask "câu hỏi" → 0 nếu đồng ý
  [[ $ASSUME_YES == 1 ]] && return 0
  read -r -p "$1 [Y/n] " reply </dev/tty || return 1
  [[ -z $reply || $reply =~ ^[YyCc] ]]
}
OS=$(uname -s)
[[ $OS == Darwin || $OS == Linux ]] || fail "Chỉ hỗ trợ macOS và Linux (hệ điều hành hiện tại: $OS)."
command -v curl >/dev/null || fail "Cần cài curl trước."

# 1. uv: quản lý Python 3.12 + thư viện theo uv.lock
step "1/6 Kiểm tra uv"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null; then
  ask "Chưa có uv. Cài uv từ https://astral.sh/uv ?" || fail "Cần uv để cài môi trường Python."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  command -v uv >/dev/null || fail "Cài uv xong nhưng không tìm thấy lệnh uv — mở Terminal mới rồi chạy lại."
fi
echo "uv $(uv --version | awk '{print $2}')"

# 2. Môi trường Python
step "2/6 Cài Python 3.12 và thư viện (.venv)"
uv sync --python 3.12 --frozen
PY=.venv/bin/python

# 3. Ollama + model
step "3/6 Ollama và model"
ollama_up() { curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1; }
has_model() {  # so khớp không phân biệt hoa thường, thiếu tag thì coi là :latest
  local want; want=$(tr '[:upper:]' '[:lower:]' <<<"$1"); [[ $want == *:* ]] || want="$want:latest"
  ollama list 2>/dev/null | awk 'NR>1{print tolower($1)}' | grep -qx -- "$want"
}
if [[ $WITH_MODELS == 1 ]]; then
  if ! command -v ollama >/dev/null; then
    if ask "Chưa có Ollama. Cài Ollama?"; then
      if [[ $OS == Darwin ]] && command -v brew >/dev/null; then brew install ollama
      elif [[ $OS == Linux ]]; then curl -fsSL https://ollama.com/install.sh | sh
      else fail "Trên macOS chưa có Homebrew: tải Ollama tại https://ollama.com/download, mở app rồi chạy lại script."
      fi
    else
      warn "Bỏ qua Ollama — app chỉ chạy chế độ tra cứu."; WITH_MODELS=0
    fi
  fi
fi
if [[ $WITH_MODELS == 1 ]]; then
  if ! ollama_up; then
    echo "Khởi động Ollama nền (log: data/ollama.log)…"
    mkdir -p data; nohup ollama serve >data/ollama.log 2>&1 &
    for _ in $(seq 1 30); do ollama_up && break; sleep 1; done
    ollama_up || fail "Ollama không phản hồi ở 127.0.0.1:11434 — mở app Ollama rồi chạy lại."
  fi
  for model in "$LIGHT_MODEL" "$HEAVY_MODEL"; do
    if has_model "$model"; then echo "Đã có model $model"
    else
      echo "Tải model $model (có thể mất nhiều phút / nhiều GB)…"
      ollama pull "$model" || fail "Không tải được $model. Thử tên khác bằng --light/--heavy."
    fi
  done
  grep -q "model: $HEAVY_MODEL\$" guardrails_config/config.yml \
    || warn "guardrails_config/config.yml đang dùng model khác $HEAVY_MODEL — sửa dòng 'model:' nếu muốn guardrails dùng model này."
fi

# 4. Lưu cấu hình cho start.sh
step "4/6 Lưu cấu hình vào local.env"
cat > local.env <<EOF
# Sinh bởi install.sh — start.sh đọc file này. Sửa tay được.
LIGHT_MODEL="$LIGHT_MODEL"
HEAVY_MODEL="$HEAVY_MODEL"
PORT="$PORT"
WITH_MODELS="$WITH_MODELS"
EOF
cat local.env | grep -v '^#'

# 5. Dữ liệu: bảng giá từ data/sources/, tài liệu chính sách nạp khi app khởi động lần đầu
step "5/6 Nhập bảng giá từ data/sources/"
if [[ $DEMO_PRICES == 1 ]]; then "$PY" import_catalog.py --all --from-today | grep -v '^  '
else "$PY" import_catalog.py --all --missing-only | grep -v '^  '
fi

# 6. Kiểm tra nhanh
step "6/6 Chạy test"
"$PY" -m unittest discover -s tests 2>&1 | tail -n 3

printf '\n\033[1;32mCài đặt xong.\033[0m Chạy app: ./start.sh  (macOS: mở start.command)\n'
echo "Lần chạy đầu, tài khoản admin + mật khẩu dùng một lần được in ra Terminal."
[[ $START == 1 ]] && exec ./start.sh
exit 0
