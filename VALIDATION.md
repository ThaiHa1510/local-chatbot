# Kiểm chứng bản mẫu — 05/11/2026

## Phạm vi

Python 3.9.6, Ollama local, model `Qwen:latest`.
Ứng dụng thực chạy ở `127.0.0.1:8876`, không phải server giả.

## Kết quả

* Unit tests: 13/13 qua. Lọc SKU, thời hạn, duyệt tài liệu, từ chối giá chưa có,
  nguồn mâu thuẫn, mô hình lỗi, không chèn văn bản sinh tự do, không chấp nhận
  ID nguồn bịa hoặc boolean sai kiểu, bảo toàn kho khi nhập dữ liệu lỗi.
* Postman CLI: 10 HTTP requests, 29 assertions, 0 failures, exit code 0.
  Đã gọi thật Ollama ở các câu bảo hành LAP-A14, eSIM PHONE-P6 và
  tần số HDMI MON-M27; nội dung nguồn chứa lần lượt 24 tháng, không hỗ trợ
  eSIM, 75 Hz qua HDMI. Các trường hợp giá chưa có, nguồn mâu thuẫn,
  mã lạ đều không trả bằng chứng. Câu rỗng trả 400, thiếu token trả 403.
* Bộ chạy Postman dùng `--no-report-events`; không đăng nhập hoặc xuất bản
  collection/kết quả lên workspace. CLI báo thiếu credentials nhưng chạy local
  thành công. `postman status` không có trong CLI đang cài; không dùng workspace.

Lệnh tái lập:

```sh
python3 -m unittest discover -s tests -v
postman collection run postman/local-rag.postman_collection.json --no-report-events
```

Lệnh Postman cần ứng dụng đang chạy ở cổng 8876 và Ollama có `Qwen:latest`.

## Giới hạn bằng chứng

Kết quả trên là kiểm tra nhỏ với dữ liệu mock, không phải chứng nhận chống
hallucination. Chưa đo chất lượng trên dữ liệu thật, các cách hỏi tiếng Việt
đa dạng, nhiều câu hỏi trong một lượt hoặc prompt injection có chủ đích.
Kiểm tra timeout là unit test có thay thế hàm; chưa giả lập lỗi mạng end-to-end.
Chưa có embedding, OCR, tài khoản, lịch sử hội thoại, kết nối CRM/đơn hàng
hoặc ticket thật. Không có bảo đảm phát hiện mọi mâu thuẫn trong văn xuôi.

## Bổ sung chính sách cửa hàng — 05/11/2026

Kho mẫu tăng từ 30 lên 41 đoạn. Có 11 đoạn phạm vi `STORE-POLICY` về giao
hàng, phí giao, kiểm hàng, địa chỉ và giờ mở cửa giả lập, liên hệ, thanh toán,
đặt hàng, VAT, bảo mật và thay đổi/hủy đơn. Truy xuất tìm tài liệu SKU và phạm
vi cửa hàng nhưng không đưa `STORE-POLICY` vào danh sách sản phẩm.

Unit tests: 27/27 đạt. Các kiểm tra mới xác nhận chính sách giao hàng dùng được
với bốn SKU, địa chỉ và giờ mở cửa cùng được phủ, giá chưa có vẫn bị từ chối,
không lẫn thông số SKU khác và hướng dẫn OTP có thể truy xuất. Địa chỉ, hotline,
email, phí và thời gian giao đều là dữ liệu giả lập; không dùng cho khách thật.

Nhận diện vị trí cửa hàng được mở rộng cho “danh sách cửa hàng”, “chi nhánh”,
“cửa hàng gần đây/gần nhất” và “hệ thống cửa hàng”. Unit tests sau cập nhật:
28/28 đạt. Dữ liệu mẫu chỉ có một địa chỉ và không dùng GPS, nên “gần đây” trả
nguồn địa chỉ hiện có chứ chưa tính khoảng cách thực tế.

## MCP vận hành CSKH — 06/11/2026

MCP server stdio cung cấp 7 tool cho cấu hình, ticket, trạng thái và thông báo
nhân viên. Tổng unit tests: 35/35 đạt. Đã kiểm tra JSON-RPC `initialize`,
`tools/list`, tạo ticket và quy trình chuẩn bị/thực thi. SMTP được kiểm tra bằng
mock client, không gửi ra Internet. Gửi email thật chưa được kiểm tra vì chưa có
SMTP và địa chỉ nhân viên thực tế; mặc định `SUPPORT_EMAIL_MODE=console`.

Trong lần thử đầu, mô hình sinh ID không có trong nguồn; ứng dụng từ chối.
Sau đó schema được giới hạn ID theo chính danh sách truy xuất và kiểm tra lại
thành công. Vẫn giữ kiểm tra ID ở tầng ứng dụng.
