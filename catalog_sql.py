"""Model local viết SQL để truy vấn bảng giá — trong một hộp cát chỉ đọc.

Các lớp an toàn (không lớp nào dựa vào việc model "ngoan"):
1. Câu hỏi nhạy cảm (giá nhập, lợi nhuận, dữ liệu khách hàng, mật khẩu…) bị chặn trước khi tới model.
2. Model chỉ thấy một bảng `products` tạm trong bộ nhớ, gồm dữ liệu công khai của sản phẩm
   đã duyệt và còn hiệu lực. Không có bảng khách hàng, đơn hàng hay giá vốn để mà lộ.
3. SQLite authorizer chỉ cho SELECT, đọc bảng `products` và một danh sách hàm cho phép;
   ghi, PRAGMA, ATTACH, bảng hệ thống, CTE đệ quy đều bị từ chối. Truy vấn chạy quá lâu bị ngắt.
4. Câu trả lời do Python ghép từ các dòng kết quả; model không tự viết giá.
"""
import re
import sqlite3

import catalog as catalog_store
from prompts import contains_money, fold

MAX_SQL = 1500
MAX_ROWS = 10
MAX_STEPS = 200_000  # số bước VM SQLite trước khi ngắt (bảng vài trăm dòng chạy rất nhanh)
ALLOWED_FUNCTIONS = {
    "lower", "upper", "like", "instr", "length", "substr", "replace", "trim", "abs", "round",
    "coalesce", "ifnull", "nullif", "iif", "min", "max", "avg", "sum", "count", "total", "cast",
}
PERF_NOTE = ("Điểm hiệu năng là ước tính từ cấu hình (dòng CPU, số lõi, RAM, SSD), không phải "
             "điểm benchmark và chưa tính card đồ họa.")
ANC = re.compile(r"chống ồn chủ động|\banc\b|noise cancel", re.I)
AUDIO = {"audio", "speaker", "microphone"}
DGPU = re.compile(r"\b(rtx|gtx|geforce mx|mx\s?\d{3}|radeon rx\s?\d{3,4}|arc a\d{3})", re.I)
SENSITIVE = re.compile(
    r"\b(gia (nhap|von|goc nhap|dai ly|si)|loi nhuan|doanh thu|chiet khau dai ly|ton kho thuc|"
    r"mat khau|password|otp|so the|cvv|cccd|cmnd|thong tin (khach|nguoi mua)|du lieu khach|"
    r"don hang cua (nguoi|khach)|so dien thoai (khach|cua)|dia chi (khach|nha)|luong nhan vien|"
    r"nha cung cap|bang (user|nguoi dung)|sqlite_master|drop table|delete from|update products)\b")
CATALOG_HINT = re.compile(r"\b(nhat|hieu nang|dang tien|cau hinh|so sanh|nen mua|tu van|bao nhieu (mau|loai))\b")

COLUMNS = (
    ("id", "TEXT", "mã dòng (luôn SELECT cột này khi trả về sản phẩm)"),
    ("sku", "TEXT", "mã sản phẩm"),
    ("category", "TEXT", "laptop | desktop | phone | audio (tai nghe) | speaker (loa) | microphone | monitor"),
    ("name", "TEXT", "tên đầy đủ"),
    ("brand", "TEXT", "hãng, ví dụ Apple, Samsung, ASUS, Sony"),
    ("price_vnd", "INTEGER", "giá bán hiện tại (VND)"),
    ("original_price_vnd", "INTEGER", "giá gốc trước giảm, có thể NULL"),
    ("discount_pct", "REAL", "phần trăm giảm so với giá gốc, có thể NULL"),
    ("status", "TEXT", "selling | out_of_stock | discontinued — mặc định chỉ lấy 'selling'"),
    ("ram_gb", "REAL", "RAM (GB)"),
    ("storage_gb", "REAL", "bộ nhớ trong / SSD (GB)"),
    ("screen_inch", "REAL", "kích thước màn hình (inch)"),
    ("weight_kg", "REAL", "khối lượng (kg)"),
    ("cpu", "TEXT", "mô tả CPU/chip (chữ)"),
    ("cpu_cores", "INTEGER", "số lõi CPU đọc được từ mô tả, có thể NULL"),
    ("gpu", "TEXT", "mô tả card đồ họa (chữ), có thể NULL"),
    ("has_dgpu", "INTEGER", "1 nếu có card đồ họa rời (RTX/GTX/MX/Radeon RX), 0 nếu không, NULL nếu chưa rõ"),
    ("vram_gb", "REAL", "bộ nhớ card rời (GB), có thể NULL"),
    ("refresh_hz", "REAL", "tần số quét màn hình (Hz), có thể NULL"),
    ("battery_wh", "REAL", "dung lượng pin laptop (Wh) — càng lớn pin càng lâu, có thể NULL"),
    ("battery_hours", "REAL", "thời lượng pin tai nghe/loa/micro (giờ, 1 lần sạc) — càng lớn càng lâu, có thể NULL"),
    ("has_anc", "INTEGER", "tai nghe/loa: 1 nếu có chống ồn chủ động (ANC), 0 nếu không; NULL với loại khác"),
    ("design", "TEXT", "kiểu tai nghe: In Ear, Earbuds, Nhét tai, Chụp tai, Kẹp tai, Vòng qua mang tai; có thể NULL"),
    ("resolution", "TEXT", "độ phân giải màn hình (chữ), có thể NULL"),
    ("os", "TEXT", "hệ điều hành (chữ), có thể NULL"),
    ("use_case", "TEXT", "nhu cầu phù hợp, ví dụ 'Gaming', 'Học tập - Văn phòng', 'Mỏng nhẹ'"),
    ("colors", "TEXT", "các màu còn hàng, ngăn cách bằng dấu phẩy, có thể NULL"),
    ("perf_score", "REAL", "điểm hiệu năng ƯỚC TÍNH (laptop/desktop), có thể NULL"),
    ("value_score", "REAL", "perf_score trên mỗi triệu đồng — càng cao càng đáng tiền"),
    ("search_text", "TEXT", "tên + mô tả, chữ thường KHÔNG DẤU, dùng LIKE '%oled%', '%gaming%', '%chong on%'"),
)
LABELS = {"ram_gb": ("RAM", " GB"), "storage_gb": ("bộ nhớ", " GB"), "screen_inch": ("màn", " inch"),
          "weight_kg": ("nặng", " kg"), "perf_score": ("điểm hiệu năng ước tính", ""),
          "value_score": ("điểm/triệu", ""), "discount_pct": ("giảm", "%"), "cpu_cores": ("số lõi", ""),
          "cpu": ("CPU", ""), "brand": ("hãng", ""), "gpu": ("card", ""), "vram_gb": ("VRAM", " GB"),
          "refresh_hz": ("tần số quét", " Hz"), "battery_wh": ("pin", " Wh"),
          "resolution": ("độ phân giải", ""), "os": ("HĐH", ""), "use_case": ("nhu cầu", ""),
          "colors": ("màu", ""), "has_dgpu": ("card rời", ""),
          "battery_hours": ("pin", " giờ"), "has_anc": ("chống ồn chủ động", ""), "design": ("kiểu", "")}


# ------------------------------------------------------------- điểm hiệu năng
# Điểm nền theo dòng CPU (ước lượng tương đối, thang ~0–100); hậu tố H/HX cộng thêm.
CPU_BASE = (
    (r"m5 max", 100), (r"m5 pro", 90), (r"m4 pro", 85), (r"m6", 82), (r"m5", 78), (r"m4", 72),
    (r"m3", 65), (r"m2", 55), (r"a18 pro", 50),
    (r"ryzen ai max\+?", 95), (r"ryzen ai 9", 85), (r"ryzen ai 7", 72), (r"ryzen ai 5", 60),
    (r"ryzen 9", 82), (r"ryzen 7", 65), (r"ryzen 5", 50), (r"ryzen 3", 30),
    (r"core ultra 9", 85), (r"core ultra 7", 72), (r"core ultra 5", 60),
    (r"\bi9\b", 80), (r"\bi7\b", 65), (r"\bi5\b", 50), (r"\bi3\b", 30),
    (r"core 7", 68), (r"core 5", 55), (r"core 3", 35),
    (r"snapdragon x ?elite", 70), (r"snapdragon x ?plus|snapdragon xplus", 60), (r"snapdragon x", 55),
)


def cpu_profile(cpu):
    """Trả (điểm nền, số lõi) từ chuỗi CPU; (None, None) nếu không nhận ra dòng CPU."""
    text = fold(cpu or "")
    base = next((score for pattern, score in CPU_BASE if re.search(pattern, text)), None)
    if base is None:
        return None, None
    if re.search(r"\b(i3 ?n|core 3 ?n|n\d{3})", text):  # dòng N tiết kiệm điện
        base -= 8
    if re.search(r"\d{3,5}hx\b", text):
        base += 10
    elif re.search(r"\d{3,5}h[s]?\b", text):
        base += 5
    m = re.search(r"(\d+)\s*(?:loi|nhan|c\b|cores?)", text)
    cores = int(m.group(1)) if m and int(m.group(1)) <= 64 else None
    return base, cores


def perf_score(item):
    if item["category"] not in ("laptop", "desktop"):
        return None, None
    base, cores = cpu_profile(item["details"].get("cpu") or item["details"].get("chipset"))
    if base is None:
        return None, cores
    specs = item["specs"]
    score = base + min(cores or 0, 24) * 0.5 + min(specs.get("ram_gb", 0), 32) / 2
    if specs.get("storage_gb", 0) >= 1024:
        score += 5
    return round(score, 1), cores


# ------------------------------------------------------------------ bảng tạm
class _Connection(sqlite3.Connection):
    steps = None


def build_db(items, today=None):
    """Bảng `products` trong bộ nhớ, chỉ gồm sản phẩm đã duyệt và còn hiệu lực."""
    conn = sqlite3.connect(":memory:", factory=_Connection)
    conn.execute("CREATE TABLE products (" + ", ".join(f"{n} {t}" for n, t, _ in COLUMNS) + ")")
    rows = []
    for item in catalog_store.active_items(items, today):
        score, cores = perf_score(item)
        price, original = item["price_vnd"], item.get("original_price_vnd")
        specs, details = item["specs"], item["details"]
        gpu = details.get("gpu")
        selling = [v["color"] for v in item.get("variants", []) if v["status"] == "selling" and v.get("color")]
        colors = list(dict.fromkeys(selling)) or item.get("colors", [])
        values = {
            "id": item["id"], "sku": item["sku"], "category": item["category"], "name": item["name"].strip(),
            "brand": item.get("manufacturer"), "price_vnd": price, "original_price_vnd": original,
            "discount_pct": round((original - price) * 100 / original, 1) if original and original > price else None,
            "status": item["status"], "cpu": details.get("cpu") or details.get("chipset"), "cpu_cores": cores,
            "perf_score": score, "value_score": round(score / (price / 1e6), 2) if score else None,
            "gpu": gpu, "has_dgpu": (1 if DGPU.search(gpu) else 0) if gpu else None,
            "resolution": details.get("resolution"), "os": details.get("os"),
            "use_case": details.get("use_case"), "colors": ", ".join(colors) or None,
            "design": details.get("design"),
            "has_anc": (1 if ANC.search(" ".join([item["name"], details.get("features", ""),
                                                  details.get("battery_life", "")])) else 0)
            if item["category"] in AUDIO else None,
            "search_text": catalog_store.search_text(item),
        }
        values.update({k: specs.get(k) for k in catalog_store.SPEC_FIELDS})
        rows.append(tuple(values.get(name) for name, _, _ in COLUMNS))
    conn.executemany(f"INSERT INTO products VALUES ({', '.join('?' * len(COLUMNS))})", rows)
    conn.commit()
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)
    steps = {"n": 0}

    def progress():
        steps["n"] += 1000
        return 1 if steps["n"] > MAX_STEPS else 0  # khác 0 → SQLite ngắt truy vấn
    conn.set_progress_handler(progress, 1000)
    conn.steps = steps
    return conn


def _authorizer(action, arg1, arg2, db_name, trigger):
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and arg1 == "products":
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in ALLOWED_FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def is_sensitive(question):
    return bool(SENSITIVE.search(fold(question)))


def looks_catalog(question, items):
    """Câu hỏi có vẻ cần tra bảng giá → đáng gọi model viết SQL."""
    text = " ".join(fold(question).split())
    if catalog_store.POLICY.search(text):
        return False
    return bool(catalog_store.detect_category(text) or catalog_store.PRICE_WORD.search(text)
                or catalog_store.detect_brand(text, items) or CATALOG_HINT.search(text))


def schema_doc(items, today=None):
    brands = sorted({i["manufacturer"] for i in catalog_store.active_items(items, today) if i.get("manufacturer")})
    lines = [f"- {name} {kind}: {desc}" for name, kind, desc in COLUMNS]
    return "Bảng products:\n" + "\n".join(lines) + "\nCác hãng hiện có: " + ", ".join(brands)


# ------------------------------------------------------------- chạy truy vấn
def run_query(conn, sql):
    """Chạy đúng một câu SELECT; trả (tên cột, các dòng). Lỗi/từ chối → ValueError."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql or len(sql) > MAX_SQL or not re.match(r"(?is)^(select|with)\b", sql):
        raise ValueError("Chỉ nhận một câu SELECT.")
    conn.steps["n"] = 0
    try:
        cursor = conn.execute(sql)  # sqlite3 tự từ chối nhiều câu lệnh trong một lần gọi
        rows = cursor.fetchmany(MAX_ROWS)
    except sqlite3.Error as exc:
        raise ValueError(f"Truy vấn bị từ chối hoặc lỗi: {exc}") from exc
    return [d[0] for d in cursor.description or []], rows


def _fmt(column, value):
    if value is None:
        return None
    if column in ("price_vnd", "original_price_vnd") or column.endswith("_vnd"):
        return catalog_store.vnd(int(value))
    if column == "has_anc":  # cột có/không: không in 1/0
        return "chống ồn chủ động" if value else "không chống ồn chủ động"
    if isinstance(value, float):
        value = int(value) if value.is_integer() else round(value, 2)
    label, unit = LABELS.get(column, ("số lượng" if column.lower().startswith("count") else column, ""))
    return f"{label} {value}{unit}"


def answer_from_rows(intent, sql, columns, rows, items):
    """Ghép câu trả lời từ dòng kết quả; mọi con số lấy từ bảng giá."""
    by_id = {i["id"]: i for i in items}
    perf_used = bool(re.search(r"perf_score|value_score", sql, re.I))
    note = catalog_store.PRICE_NOTE + (" " + PERF_NOTE if perf_used else "")
    base = {"mode": "catalog", "catalog_parser": "sql", "catalog_sql": sql, "disclaimer": note,
            "context_used": False, "normalized_question": None, "spelling_corrected": False}
    if not rows:
        return {**base, "status": "catalog", "reason": "catalog_no_match", "citations": [], "mock": False,
                "answer": f"Dạ, hiện bảng giá chưa có sản phẩm nào phù hợp với yêu cầu “{intent}”. "
                          "Bạn thử nới điều kiện (giá, hãng, thông số) giúp mình nhé."}
    if "id" in columns:
        idx = columns.index("id")
        hidden = {"id", "sku", "name", "category", "status", "search_text"}
        lines, cited, highlights = [], [], []
        for n, row in enumerate(rows, 1):
            item = by_id.get(row[idx])
            if not item:
                continue
            cited.append(item)
            extras = [_fmt(c, v) for c, v in zip(columns, row) if c not in hidden and c != "price_vnd"]
            extras = [e for e in extras if e]
            if item["status"] in catalog_store.STATUS_NOTES:
                extras.append(catalog_store.STATUS_NOTES[item["status"]])
            highlights.append("; ".join(extras))
            lines.append(f"{n}) {item['name'].strip()} – {catalog_store.vnd(item['price_vnd'])}"
                         + (f" ({'; '.join(extras)})" if extras else ""))
        if not cited:
            raise ValueError("Kết quả không trỏ tới sản phẩm hợp lệ.")
        intro = f"Dạ, mình tìm được {len(cited)} sản phẩm phù hợp với yêu cầu “{intent}”:"
        citations = [{**catalog_store.citation(i), "highlight": h or None} for i, h in zip(cited, highlights)]
        return {**base, "status": "catalog", "reason": "catalog_sql", "intro": intro,
                "answer": intro + "\n" + "\n".join(lines),
                "citations": citations, "mock": any(i["mock"] for i in cited)}
    # Truy vấn tổng hợp (đếm, trung bình…): hiển thị nguyên giá trị đã tính từ bảng.
    cells = [", ".join(filter(None, (_fmt(c, v) for c, v in zip(columns, row)))) for row in rows]
    return {**base, "status": "catalog", "reason": "catalog_sql_aggregate", "citations": [], "mock": False,
            "answer": f"Dạ, với yêu cầu “{intent}”, kết quả từ bảng giá là:\n" + "\n".join(f"• {c}" for c in cells)}


def validate_intent(intent, question):
    """Mô tả ý định hiển thị cho khách: ngắn, không link, mọi con số phải có trong câu hỏi gốc."""
    intent = " ".join((intent or "").split())
    if not intent or len(intent) > 160 or re.search(r"https?://|www\.", intent):
        raise ValueError("Mô tả ý định không hợp lệ.")
    asked = set(re.findall(r"\d+", question))
    if contains_money(intent) and not set(re.findall(r"\d+", intent)) <= asked:
        raise ValueError("Mô tả ý định chứa số tiền không có trong câu hỏi.")
    return intent
