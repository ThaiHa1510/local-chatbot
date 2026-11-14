#!/bin/zsh
# Mac: bấm đúp để cài đặt trọn gói.
"$(dirname -- "$0")/install.sh" "$@"
echo; read -k1 "?Nhấn phím bất kỳ để đóng…"
