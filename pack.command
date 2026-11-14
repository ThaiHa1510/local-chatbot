#!/bin/zsh
# Đóng gói mã nguồn + dữ liệu nguồn để chạy ở máy khác.
# KHÔNG kèm: .venv, bảng giá đã sinh, tài khoản, lịch sử chat, phiếu hỗ trợ, cache, trace, backup.
cd -- "$(dirname -- "$0")" || exit 1
name="LocalSupportAgent-$(date +%Y%m%d)"
mkdir -p dist && rm -f "dist/$name.zip"
staging=$(mktemp -d) && trap 'rm -rf "$staging"' EXIT
rsync -a --exclude-from=.gitignore --exclude=.gitignore ./ "$staging/$name/" || exit 1
cp .gitignore "$staging/$name/"
(cd "$staging" && zip -qr -X - "$name") > "dist/$name.zip" || exit 1
echo "Đã tạo dist/$name.zip ($(du -h "dist/$name.zip" | cut -f1))"
