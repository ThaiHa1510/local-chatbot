# MCP server cho vận hành CSKH

## Phân quyền và trace

MCP lấy danh tính từ `SUPPORT_MCP_ACTOR` và `SUPPORT_MCP_ROLE` (`viewer`, `agent`, `admin`). Mặc định là `agent`; chỉ `admin` được gọi `execute_staff_notification`. Nội dung tool được kiểm tra bằng NVIDIA NeMo Guardrails qua Ollama; mọi tool call ghi trace và eval `rbac`, `argument_guard`, `tool_completed`.

```json
{"env":{"SUPPORT_MCP_ACTOR":"support-desktop","SUPPORT_MCP_ROLE":"agent"}}
```

`mcp_server.py` là MCP server chạy qua stdio, chỉ dùng thư viện chuẩn Python.
Nó tách hành động nghiệp vụ khỏi RAG: RAG tìm và trả lời bằng tài liệu; MCP tạo
ticket, cập nhật trạng thái và liên hệ nhân viên.

## Các tool

| Tool | Tác dụng | Side effect |
| --- | --- | --- |
| `support_config_status` | Xem chế độ email và phần cấu hình còn thiếu | Không |
| `create_support_ticket` | Tạo ticket trong `data/support.sqlite3` | Ghi local |
| `get_support_ticket` | Đọc một ticket | Không |
| `list_support_tickets` | Liệt kê tối đa 100 ticket | Không |
| `update_support_ticket_status` | Đổi trạng thái và ghi audit event | Ghi local |
| `prepare_staff_notification` | Tạo bản xem trước, token xác nhận hiệu lực 15 phút | Ghi local, chưa gửi |
| `execute_staff_notification` | Thực thi bản đã chuẩn bị | Ghi local hoặc gửi SMTP thật |

Luồng liên hệ nhân viên:

```text
create_support_ticket
        ↓
prepare_staff_notification
        ↓
Hiển thị người nhận + tiêu đề + nội dung cho người dùng duyệt
        ↓
execute_staff_notification(action_id, confirmation_token)
        ↓
console: ghi local   |   smtp: gửi email thật
```

## Chạy thử MCP server

```sh
cd /Users/hbliss/Documents/LocalSupportAgent
python3 mcp_server.py
```

Server đọc JSON-RPC từ stdin và chỉ ghi protocol message ra stdout. Có thể dùng
`start_mcp.command` hoặc cấu hình client theo `mcp-config.example.json`.

## Chế độ an toàn mặc định

```text
SUPPORT_EMAIL_MODE=console
```

`execute_staff_notification` chỉ ghi sự kiện `simulated` vào SQLite, không gửi
ra mạng. Đây là chế độ dùng cho demo và kiểm thử.

## Cấu hình gửi email thật

Đặt biến môi trường trong cấu hình MCP client, trình quản lý secret hoặc shell.
Không ghi mật khẩu vào source code hoặc commit vào Git.

```text
SUPPORT_EMAIL_MODE=smtp
SUPPORT_SMTP_HOST=smtp.example.com
SUPPORT_SMTP_PORT=587
SUPPORT_SMTP_SECURITY=starttls
SUPPORT_SMTP_USERNAME=cs@example.com
SUPPORT_SMTP_PASSWORD=<app-password-or-secret>
SUPPORT_EMAIL_FROM=cs@example.com
SUPPORT_STAFF_EMAIL=support-team@example.com
```

`SUPPORT_STAFF_EMAIL` là đích cố định; tool không nhận địa chỉ người nhận tùy ý.
Với cổng SSL trực tiếp thường dùng `SUPPORT_SMTP_SECURITY=ssl` và cổng phù hợp.
SMTP thật chưa được kiểm tra end-to-end trong bản demo vì chưa có tài khoản.

## Quy tắc cho MCP client/agent

1. Không tự tạo ticket chỉ vì RAG thiếu dữ liệu; hỏi khách có muốn chuyển nhân viên.
2. Trước khi tạo ticket, cho khách xem chủ đề, mô tả và thông tin liên hệ sẽ lưu.
3. Sau khi tạo ticket, trả lại mã ticket.
4. `prepare_staff_notification` không gửi email. Hiển thị đích nhận, tiêu đề và
   nội dung; chỉ gọi `execute_staff_notification` sau khi khách xác nhận.
5. Không đưa mật khẩu, OTP, số thẻ đầy đủ hoặc giấy tờ tùy thân vào ticket/email.
6. Không tuyên bố nhân viên đã nhận nếu kết quả chỉ là `delivery=simulated`.

Ticket và audit log nằm trong SQLite local dưới dạng văn bản thuần. Nếu dùng dữ
liệu khách thật, cần phân quyền file, chính sách lưu/xóa dữ liệu và sao lưu phù hợp.

## Ví dụ cấu hình client

Sao chép cấu trúc trong `mcp-config.example.json` vào phần MCP server của client.
Đường dẫn `command`/`args` phải trỏ đến bản `mcp_server.py` trong máy chạy client.
Khởi động lại client sau khi đổi cấu hình hoặc biến môi trường.

## Kiểm thử

```sh
python3 -m unittest discover -s tests -v
```

Kiểm thử SMTP dùng mock: xác nhận STARTTLS, đăng nhập, đích nhận cố định và gửi
message mà không truyền dữ liệu ra Internet. Kiểm thử stdio gửi `initialize` và
`tools/list` qua subprocess thật.
