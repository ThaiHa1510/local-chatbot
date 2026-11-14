"""Truy vấn danh mục sản phẩm có cấu trúc: giá, thông số, so sánh, lọc, "rẻ nhất".

Nguyên tắc an toàn:
- Câu hỏi được dịch thành một bộ lọc (spec) bằng luật; chỉ khi luật không hiểu mới
  nhờ model, và model chỉ được trả JSON theo schema với giá trị trong enum.
- Python tự lọc, sắp xếp và ghép câu trả lời. Mọi con số trong câu trả lời đều lấy từ
  bảng giá đã duyệt và còn hiệu lực; model không bao giờ viết giá.
- Khuyến mãi và tồn kho không có trong bảng giá nên luôn cần nhân viên xác nhận.
"""
from datetime import date
import json
from pathlib import Path
import re

from prompts import fold

ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "data" / "catalog.json"

CATEGORIES = {"laptop": "laptop", "desktop": "máy tính để bàn", "phone": "điện thoại",
              "monitor": "màn hình", "audio": "tai nghe", "speaker": "loa",
              "microphone": "micro / máy ghi âm"}
STATUSES = {"selling", "out_of_stock", "discontinued"}
STATUS_NOTES = {"out_of_stock": "tạm hết hàng", "discontinued": "đã ngừng bán"}
DETAIL_FIELDS = {"chipset": "chip", "cpu": "CPU", "ram_type": "loại RAM", "panel": "màn",
                 "battery": "pin", "battery_life": "pin", "charging_time": "sạc",
                 "water_resistance": "kháng nước", "connection": "kết nối", "driver": "driver",
                 "power": "công suất", "microphone": "micro", "weight": "khối lượng",
                 "gpu": "card đồ họa", "resolution": "độ phân giải", "os": "hệ điều hành",
                 "use_case": "nhu cầu", "features": "tính năng", "material": "chất liệu",
                 "npu": "NPU", "webcam": "webcam", "wifi": "Wi-Fi", "bluetooth": "Bluetooth",
                 "ports": "cổng kết nối", "dimensions": "kích thước", "warranty": "bảo hành",
                 "design": "kiểu"}
# Chi tiết dài/ít khi hỏi: không đưa vào tóm tắt sản phẩm (vẫn có trong SQL và tra cứu).
SUMMARY_SKIP = {"npu", "webcam", "wifi", "bluetooth", "ports", "dimensions", "warranty", "features", "use_case"}
# Không đưa vào search_text để lọc từ khóa không bắt nhầm ("lỗi", "tháng"…).
SEARCH_SKIP = {"warranty", "dimensions"}
SPEC_FIELDS = {  # khóa → (nhãn, đơn vị)
    "ram_gb": ("RAM", "GB"),
    "storage_gb": ("bộ nhớ", "GB"),
    "screen_inch": ("màn hình", "inch"),
    "weight_kg": ("nặng", "kg"),
    "refresh_hz": ("tần số quét", "Hz"),
    "battery_wh": ("pin", "Wh"),
    "vram_gb": ("VRAM", "GB"),
    "battery_hours": ("dùng pin", "giờ"),
}
SORT_FIELDS = ("price_vnd",) + tuple(SPEC_FIELDS)
FILTER_FIELDS = ("price_min", "price_max", "ram_min", "storage_min", "brand", "keywords")
KINDS = ("rank", "list", "lookup", "compare")
LIST_LIMIT = 5
PRICE_NOTE = ("Giá niêm yết theo bảng giá đang áp dụng; khuyến mãi và tồn kho "
              "cần nhân viên xác nhận trước khi đặt hàng.")


# --------------------------------------------------------------------- dữ liệu
def catalog_files():
    """Bảng giá chính + các file nhập thêm data/catalog_*.json (ví dụ catalog_phones.json)."""
    return [CATALOG_PATH] + sorted(CATALOG_PATH.parent.glob("catalog_*.json"))


def load_catalog(path=None):
    """Đọc và kiểm tra bảng giá; sai một dòng thì từ chối cả bảng giá.

    Không truyền path: gộp mọi file trong catalog_files(), ID không được trùng giữa các file.
    """
    paths = [Path(path)] if path else catalog_files()
    items = []
    for p in paths:
        if p.exists():
            rows = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError(f"Bảng giá phải là danh sách: {p.name}")
            items.extend(rows)
    ids = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Mỗi dòng bảng giá phải là object.")
        for key in ("id", "sku", "category", "name", "source", "valid_from", "valid_to", "status"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"Bảng giá thiếu trường {key}.")
        if item["id"] in ids:
            raise ValueError("ID bảng giá bị trùng: " + item["id"])
        ids.add(item["id"])
        if item["category"] not in CATEGORIES or item["status"] not in STATUSES:
            raise ValueError("Loại sản phẩm hoặc trạng thái không hợp lệ: " + item["id"])
        price = item.get("price_vnd")
        if type(price) is not int or not 0 < price < 10**10:
            raise ValueError("Giá phải là số nguyên VND dương: " + item["id"])
        if date.fromisoformat(item["valid_from"]) > date.fromisoformat(item["valid_to"]):
            raise ValueError("Thời hạn giá không hợp lệ: " + item["id"])
        if type(item.get("approved")) is not bool or type(item.get("mock")) is not bool:
            raise ValueError("approved và mock phải là boolean: " + item["id"])
        specs = item.setdefault("specs", {})
        if not isinstance(specs, dict) or any(
                k not in SPEC_FIELDS or type(v) not in (int, float) or v <= 0 for k, v in specs.items()):
            raise ValueError("Thông số không hợp lệ: " + item["id"])
        details = item.setdefault("details", {})
        if not isinstance(details, dict) or any(
                k not in DETAIL_FIELDS or not isinstance(v, str) or len(v) > 200 for k, v in details.items()):
            raise ValueError("details không hợp lệ: " + item["id"])
        original = item.setdefault("original_price_vnd", None)
        if original is not None and (type(original) is not int or original <= 0):
            raise ValueError("original_price_vnd không hợp lệ: " + item["id"])
        brand = item.setdefault("manufacturer", None)
        if brand is not None and (not isinstance(brand, str) or not brand.strip()):
            raise ValueError("manufacturer không hợp lệ: " + item["id"])
        aliases = item.setdefault("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
            raise ValueError("aliases phải là danh sách chuỗi: " + item["id"])
        colors = item.setdefault("colors", [])
        if not isinstance(colors, list) or not all(isinstance(c, str) and c.strip() for c in colors):
            raise ValueError("colors phải là danh sách chuỗi: " + item["id"])
        variants = item.setdefault("variants", [])
        if not isinstance(variants, list) or any(
                not isinstance(v, dict) or type(v.get("price_vnd")) is not int or v["price_vnd"] <= 0
                or v.get("status") not in STATUSES or not isinstance(v.get("color", ""), str)
                for v in variants):
            raise ValueError("variants không hợp lệ: " + item["id"])
        image = item.setdefault("image_url", None)
        if image is not None and (not isinstance(image, str) or not image.startswith("https://")):
            raise ValueError("image_url không hợp lệ: " + item["id"])
    return items


def active_items(items, today=None):
    today = today or date.today().isoformat()
    return [i for i in items if i["approved"] and i["valid_from"] <= today <= i["valid_to"]]


# ------------------------------------------------------------------- đọc câu hỏi
CATEGORY_PATTERNS = (  # thứ tự quan trọng: "laptop nào màn hình to nhất" là laptop
    ("desktop", r"\b(may tinh de ban|may ban|desktop|pc|mac mini|mac studio|imac)\b"),
    ("laptop", r"\b(laptop|notebook|macbook|may tinh xach tay|may tinh)\b"),
    ("phone", r"\b(dien thoai|smartphone|phone)\b"),
    ("audio", r"\b(tai nghe|headphones?|earbuds?|airpods|earpods)\b"),
    ("speaker", r"\b(loa|speaker|soundbar)\b"),
    ("microphone", r"\b(microphone|micro thu am|mic thu am|may ghi am)\b"),
    ("monitor", r"\b(monitor|man hinh)\b"),
)
PRICE_WORD = re.compile(r"(?<!tham )\bgia\b(?! (han|dinh|tang|cong|dung))|\bbao nhieu tien\b|\bprice\b"
                        r"|\bre\b|\bdat (nhat|hon|tien)\b")
POLICY = re.compile(r"\b(bao hanh|doi tra|giao|ship|van chuyen|thanh toan|tra gop|cua hang|chi nhanh"
                    r"|dia chi|loi|sua|cai dat|huong dan|ket noi|phi)\b")
LIST_CUE = re.compile(r"\b(nhung|cac|danh sach|liet ke|goi y|tu van|loai nao|mau nao|nao)\b")
SORT_RULES = (
    (r"\b(re nhat|gia (thap|re|mem|tot) nhat|tiet kiem nhat)\b", "price_vnd", "asc"),
    (r"\b(dat nhat|mac nhat|gia cao nhat|cao cap nhat)\b", "price_vnd", "desc"),
    (r"\bram (?:nao )?(cao|lon|nhieu|khung|manh) nhat\b", "ram_gb", "desc"),
    (r"\b(bo nho|ssd|dung luong|o cung|luu tru) (?:nao )?(lon|nhieu|cao) nhat\b", "storage_gb", "desc"),
    (r"\bman hinh (?:nao )?(nho) nhat\b", "screen_inch", "asc"),
    (r"\b(to|lon|rong) nhat\b", "screen_inch", "desc"),
    (r"\bnhe nhat\b", "weight_kg", "asc"),
    (r"\bnang nhat\b", "weight_kg", "desc"),
)
MONEY_UNITS = {"trieu": 10**6, "tr": 10**6, "cu": 10**6, "m": 10**6, "k": 1000, "nghin": 1000, "ngan": 1000}
NUM = r"(\d+(?:[.,]\d+)*)\s*(trieu|tr|cu|m|k|nghin|ngan|d|vnd|dong)?(?![a-z0-9])"
NOT_MONEY = r"(?!\s*(?:gb|tb|inch|in|kg|hz|mah|wh|gio|thang|ngay|nam|w)\b)"
RANGE = re.compile(r"\b(?:tu|trong khoang)\s*" + NUM + r"\s*(?:den|toi|-)\s*" + NUM + NOT_MONEY)
# "10-15tr": số thứ hai bắt buộc có đơn vị tiền để không nhầm với ngày tháng.
DASH_RANGE = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(trieu|tr|cu|m|k)?\s*-\s*"
                        r"(\d+(?:[.,]\d+)?)\s*(trieu|tr|cu|m|k)(?![a-z0-9])")
MAX_PRICE = (re.compile(r"\b(?:duoi|nho hon|it hon|khong qua|toi da|re hon|thap hon|<=?)\s*" + NUM + NOT_MONEY),
             re.compile(NUM + NOT_MONEY + r"\s*(?:tro xuong|do lai|tro lai)\b"))
MIN_PRICE = (re.compile(r"\b(?<!re )(?<!thap )(?<!nho )(?<!it )(?:tren|hon|lon hon|cao hon|it nhat|toi thieu|>=?)\s*" + NUM + NOT_MONEY),
             re.compile(NUM + NOT_MONEY + r"\s*(?:tro len|do len)\b"))
ABOUT_PRICE = re.compile(r"\b(?:khoang|tam|tam khoang|quanh)\s*" + NUM + NOT_MONEY)
RAM = (re.compile(r"\bram\s*(?:tu |tren |it nhat |toi thieu |>=?\s*)?(\d+)\s*(?:gb?|g)?\b"),
       re.compile(r"\b(\d+)\s*(?:gb?|g)\s*ram\b"))
STORAGE = re.compile(r"\b(?:ssd|bo nho|o cung|dung luong|rom)\s*(?:tu |tren |it nhat |toi thieu )?(\d+)\s*(gb|tb)\b")


def parse_money(number, unit):
    """'15tr' → 15.000.000; '15,5 triệu' → 15.500.000; '15.990.000đ' → 15.990.000; '15' → 15 triệu."""
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", number) and unit not in MONEY_UNITS:
        value = float(re.sub(r"[.,]", "", number))
    elif re.fullmatch(r"\d+(?:[.,]\d+)?", number):
        value = float(number.replace(",", "."))
    else:
        value = float(re.sub(r"[.,]", "", number))
    if unit in MONEY_UNITS:
        value *= MONEY_UNITS[unit]
    elif value < 1000:
        value *= 10**6  # "laptop dưới 15" trong ngữ cảnh mua bán là 15 triệu
    return int(round(value))


def _clean(question):
    return " ".join(fold(question).replace("₫", " d").split())


def detect_category(text):
    for category, pattern in CATEGORY_PATTERNS:
        if re.search(pattern, text):
            return category
    return None


def detect_sort(text):
    for pattern, field, order in SORT_RULES:
        if re.search(pattern, text):
            return field, order
    return None


GAMING_NAMES = (r"gaming|hyperx|inzone|\brog\b|\btuf\b|legion|\bloq\b|victus|katana|cyborg|nitro|predator|helios"
                r"|\bvector\b|strix|\bomen\b")


def detect_keywords(text):
    """Từ khóa nhu cầu/CPU → [nhãn, regex tìm trong tên + CPU/chip/màn của sản phẩm].

    Chỉ nhận từ khóa trong danh sách dưới đây; regex do mã tạo, không lấy nguyên văn câu hỏi.
    """
    found = []

    def add(label, pattern):
        if [label, pattern] not in found:
            found.append([label, pattern])

    if re.search(r"\b(gaming|choi game|game)\b", text):
        add("gaming", GAMING_NAMES)
    # Tai nghe / loa: nhu cầu thường gặp.
    if re.search(r"\b(chong on|khu tieng on|anc|noise cancel\w*)\b", text):
        add("chống ồn", r"\b(anc|nc|chong on|khu tieng on|noise cancel\w*)\b")
    if re.search(r"\b(chup tai|over ear|headphone)\b", text):
        add("chụp tai", r"\bchup tai\b")
    if re.search(r"\b(true wireless|tws|khong day|bluetooth|wireless)\b", text):
        add("không dây", r"\b(true wireless|tws|ows|khong day|bluetooth|wireless)\b")
    if re.search(r"\b(co day|wired)\b", text):
        add("có dây", r"\b(co day|wired)\b")
    if re.search(r"\b(the thao|chay bo|tap gym|open ear)\b", text):
        add("thể thao", r"\b(the thao|open ear|open|fit|sport)\b")
    if re.search(r"\bkaraoke\b", text):
        add("karaoke", r"\b(karaoke|partybox|micro)\b")
    if re.search(r"\b(chong nuoc|khang nuoc|di mua|ip6\d|ip67|ip68)\b", text):
        add("chống nước", r"\bipx?[4-8]\d?\b")
    if re.search(r"\boled\b", text):
        add("OLED", r"\boled\b")
    if (m := re.search(r"\b(?:core\s*)?ultra\s*([579])\b", text)):
        add(f"Core Ultra {m.group(1)}", rf"core ultra {m.group(1)}\b")
    if (m := re.search(r"\b(?:core\s*)?i([3579])\b", text)):
        add(f"Core i{m.group(1)}", rf"\bi{m.group(1)}\b")
    if (m := re.search(r"(?<!ultra )\bcore\s*([357])\b", text)):
        add(f"Core {m.group(1)}", rf"(?<!ultra )\bcore {m.group(1)}\b")
    if (m := re.search(r"\bryzen\s*(ai\s*)?([3579])?\b", text)):
        ai = "ai " if m.group(1) else ""
        tier = m.group(2) or ""
        add(f"Ryzen {ai.upper()}{tier}".strip(), rf"ryzen {ai}{tier}\b" if tier else rf"ryzen {ai}".strip())
    for word, label in (("snapdragon", "Snapdragon"), ("dimensity", "Dimensity"), ("exynos", "Exynos"),
                        ("helio", "Helio")):
        if re.search(rf"\b{word}\b", text):
            add(label, rf"\b{word}\b")
    if (m := re.search(r"\b(?:chip\s*)?(?:apple\s*)?m([1-6])(\s*(?:pro|max))?\b", text)):
        tier = (m.group(2) or "").strip()
        add(f"Apple M{m.group(1)} {tier.capitalize()}".strip(), rf"\bm{m.group(1)}\b" + (f" {tier}" if tier else ""))
    if re.search(r"\bintel\b", text):
        add("Intel", r"\bintel\b|\bcore\b")
    if re.search(r"\bamd\b", text):
        add("AMD", r"\bamd\b|\bryzen\b")
    return found


def search_text(item):
    details = [v for k, v in item["details"].items() if k not in SEARCH_SKIP]
    text = " ".join([item["name"], item["sku"]] + details + item.get("colors", []))
    return " ".join(fold(text).replace("-", " ").split())


def detect_filters(text):
    filters = {}
    match = RANGE.search(text) or DASH_RANGE.search(text)
    if match:
        low_num, low_unit, high_num, high_unit = match.groups()
        low = parse_money(low_num, low_unit or high_unit)
        high = parse_money(high_num, high_unit or low_unit)
        filters["price_min"], filters["price_max"] = min(low, high), max(low, high)
    else:
        for pattern in MAX_PRICE:
            if (m := pattern.search(text)):
                filters["price_max"] = parse_money(*m.groups())
                break
        for pattern in MIN_PRICE:
            if (m := pattern.search(text)):
                filters["price_min"] = parse_money(*m.groups())
                break
        if not filters and (m := ABOUT_PRICE.search(text)):
            center = parse_money(*m.groups())
            filters["price_min"], filters["price_max"] = int(center * 0.85), int(center * 1.15)
    for pattern in RAM:
        if (m := pattern.search(text)):
            filters["ram_min"] = int(m.group(1))
            break
    if (m := STORAGE.search(text)):
        filters["storage_min"] = int(m.group(1)) * (1024 if m.group(2) == "tb" else 1)
    return filters


def _name_text(text):
    """So tên máy: '-' và khoảng trắng coi như nhau ("14-em0023au" = "14 em0023au")."""
    return " ".join(fold(text).replace("-", " ").split())


def _alias_pattern(alias):
    return r"(?<![a-z0-9])" + re.escape(_name_text(alias)) + r"(?![a-z0-9])"


# Tiêu chí luật không tính được: cần model viết SQL (điểm ước tính) hoặc dữ liệu chưa có.
BEYOND_RULES = re.compile(r"\b(hieu nang|dang tien|gia tri nhat|p ?/ ?p|pin (trau|lau|khoe|nhieu|tot)|camera|"
                          r"chup anh|card (do hoa|roi)|gpu|nen mua|(?<!gia )tot nhat|manh nhat|ben nhat|"
                          r"muot nhat|dep nhat)\b")


def beyond_rules(question):
    return bool(BEYOND_RULES.search(_clean(question)))


def mentioned_skus(text, items):
    """SKU được nhắc tới bằng mã hoặc tên, theo thứ tự xuất hiện trong câu.

    Ưu tiên tên dài nhất: "iphone 17 pro" không bị tính là "iphone 17". Nhiều phiên bản
    cùng tên (256GB, 512GB…) đều được trả về để so sánh.
    """
    found = []
    text = _name_text(text)
    for item in items:
        best = None
        for alias in [item["sku"]] + item["aliases"]:
            m = re.search(_alias_pattern(alias), text)
            if m and (best is None or m.end() - m.start() > best[1] - best[0]):
                best = (m.start(), m.end())
        if best:
            found.append((best[0], best[1], item["sku"]))
    kept = [f for f in found if not any(o[0] <= f[0] and f[1] <= o[1] and (o[1] - o[0]) > (f[1] - f[0])
                                        for o in found)]
    return list(dict.fromkeys(sku for _, _, sku in sorted(kept)))


BRAND_ALIASES = {"airpods": "apple", "earpods": "apple", "powerbeats": "beats", "iphone": "apple", "ip": "apple", "macbook": "apple", "imac": "apple", "mac": "apple",
                 "ss": "samsung", "redmi": "xiaomi", "poco": "xiaomi", "thinkpad": "lenovo",
                 "legion": "lenovo", "zenbook": "asus", "vivobook": "asus", "rog": "asus",
                 "surface": "microsoft"}


def detect_brand(text, items):
    """Hãng được nhắc tới (giữ cách viết trong dữ liệu, ví dụ 'Samsung'); None nếu không có."""
    brands = {fold(i["manufacturer"]): i["manufacturer"] for i in items if i.get("manufacturer")}
    for word, brand in list(BRAND_ALIASES.items()) + [(b, b) for b in brands]:
        if brand in brands and re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text):
            return brands[brand]
    return None


def _spec(kind, category=None, sort=None, filters=None, skus=()):
    sort_by, order = sort or ("price_vnd", "asc")
    return {"kind": kind, "category": category, "sort_by": sort_by, "order": order,
            "filters": dict(filters or {}), "skus": list(skus)}


def parse(question, selected_product, items, today=None):
    """Luật nhận diện câu hỏi danh mục. Trả spec, hoặc None nếu không phải câu hỏi danh mục."""
    text = _clean(question)
    current = active_items(items, today)
    category = detect_category(text)
    mentions = mentioned_skus(text, current)
    sort = detect_sort(text)
    filters = detect_filters(text)
    brand = detect_brand(text, current)
    if brand:
        filters["brand"] = brand
    keywords = detect_keywords(text)
    if keywords:
        filters["keywords"] = keywords
    price = bool(PRICE_WORD.search(text))
    policy = bool(POLICY.search(text))
    if len(mentions) >= 2 and not policy:
        return _spec("compare", skus=mentions)
    # Hãng đi kèm tên máy ("giá samsung galaxy s26") không phải điều kiện lọc riêng.
    if len(mentions) == 1 and price and not policy and not (filters.keys() - {"brand", "keywords"}):
        return _spec("lookup", skus=mentions)
    if not category and (sort or filters or (price and LIST_CUE.search(text))) and not policy:
        # "mẫu nào rẻ nhất?" → so trong cùng loại với sản phẩm đang chọn;
        # "samsung nào rẻ nhất?" → mọi loại của hãng đó.
        selected = next((i for i in current if i["sku"] == selected_product), None)
        category = selected["category"] if selected and not brand else None
        if not selected and not filters and not sort:
            return None
        return _spec("rank" if sort else "list", category, sort, filters)
    if category and (sort or filters):
        return _spec("rank" if sort else "list", category, sort, filters)
    if category and not policy and (price or LIST_CUE.search(text)):
        return _spec("list", category)
    if price and not policy and not category:
        sku = mentions[0] if mentions else selected_product
        if any(i["sku"] == sku for i in current):
            return _spec("lookup", skus=[sku])
    return None


# ------------------------------------------------------------------ trả lời
def vnd(value):
    return f"{value:,}".replace(",", ".") + "đ"


def _num(value):
    return str(int(value)) if float(value).is_integer() else str(value).replace(".", ",")


def spec_summary(item):
    parts = [f"{label} {_num(item['specs'][k])} {unit}" for k, (label, unit) in SPEC_FIELDS.items()
             if k in item["specs"]]
    parts += [f"{label} {item['details'][k]}" for k, label in DETAIL_FIELDS.items()
              if item["details"].get(k) and k not in SUMMARY_SKIP]
    colors = color_text(item)
    if colors:
        parts.append(colors)
    head = "Loại: " + CATEGORIES[item["category"]]
    if item.get("manufacturer"):
        head += f"; hãng {item['manufacturer']}"
    parts = [head] + parts
    if item["status"] in STATUS_NOTES:
        parts.append(STATUS_NOTES[item["status"]])
    return "; ".join(parts) + "."


def color_text(item):
    """"màu Bạc, Vàng (hết hàng: Hồng)" từ các phiên bản theo màu; rỗng nếu không có dữ liệu."""
    variants = [v for v in item.get("variants", []) if v.get("color")]
    if not variants:
        return "màu " + ", ".join(item["colors"]) if item.get("colors") else ""
    selling = list(dict.fromkeys(v["color"] for v in variants if v["status"] == "selling"))
    other = [c for c in dict.fromkeys(v["color"] for v in variants) if c not in selling]
    text = "màu " + (", ".join(selling) if selling else "chưa có màu còn hàng")
    return text + (f" (hết hàng: {', '.join(other)})" if other else "")


def price_text(item):
    text = vnd(item["price_vnd"])
    prices = sorted({v["price_vnd"] for v in item.get("variants", []) if v["status"] == "selling"})
    if len(prices) > 1:
        text += f" (tùy màu {vnd(prices[0])} – {vnd(prices[-1])})"
    original = item.get("original_price_vnd")
    if original and original > item["price_vnd"]:
        text += f" (giá gốc {vnd(original)})"
    return text


def citation(item):
    return {"id": item["id"], "product": item["sku"], "topic": f"{item['name'].strip()} — {price_text(item)}",
            "text": spec_summary(item), "source": item["source"], "valid_from": item["valid_from"],
            "valid_to": item["valid_to"], "mock": item["mock"], "price_vnd": item["price_vnd"],
            # Trường cho thẻ sản phẩm ở giao diện chat.
            "kind": "product", "name": item["name"].strip(), "price_text": price_text(item),
            "original_price_vnd": item.get("original_price_vnd"), "image_url": item.get("image_url"),
            "colors": item.get("colors", []), "status_note": STATUS_NOTES.get(item["status"])}


def describe_filters(filters):
    parts = []
    low, high = filters.get("price_min"), filters.get("price_max")
    if low and high:
        parts.append(f"giá từ {vnd(low)} đến {vnd(high)}")
    elif high:
        parts.append(f"giá không quá {vnd(high)}")
    elif low:
        parts.append(f"giá từ {vnd(low)} trở lên")
    if filters.get("ram_min"):
        parts.append(f"RAM từ {filters['ram_min']} GB")
    if filters.get("storage_min"):
        parts.append(f"bộ nhớ từ {filters['storage_min']} GB")
    if filters.get("brand"):
        parts.insert(0, f"hãng {filters['brand']}")
    if filters.get("keywords"):
        parts.insert(0, " ".join(label for label, _ in filters["keywords"]))
    return ", ".join(parts)


def matches(item, filters):
    price = item["price_vnd"]
    if filters.get("price_min") and price < filters["price_min"]:
        return False
    if filters.get("price_max") and price > filters["price_max"]:
        return False
    for key, field in (("ram_min", "ram_gb"), ("storage_min", "storage_gb")):
        if filters.get(key) and item["specs"].get(field, 0) < filters[key]:
            return False
    if filters.get("brand") and fold(item.get("manufacturer") or "") != fold(filters["brand"]):
        return False
    if filters.get("keywords"):
        text = search_text(item)
        if not all(re.search(pattern, text) for _, pattern in filters["keywords"]):
            return False
    return True


def _value(item, field):
    return item["price_vnd"] if field == "price_vnd" else item["specs"].get(field)


CRITERIA = {
    ("price_vnd", "asc"): "giá thấp nhất", ("price_vnd", "desc"): "giá cao nhất",
    ("ram_gb", "desc"): "RAM lớn nhất", ("ram_gb", "asc"): "RAM nhỏ nhất",
    ("storage_gb", "desc"): "bộ nhớ lớn nhất", ("storage_gb", "asc"): "bộ nhớ nhỏ nhất",
    ("screen_inch", "desc"): "màn hình lớn nhất", ("screen_inch", "asc"): "màn hình nhỏ nhất",
    ("weight_kg", "asc"): "trọng lượng nhẹ nhất", ("weight_kg", "desc"): "trọng lượng nặng nhất",
}


def _result(status, reason, answer, rows=()):
    return {"status": status, "reason": reason, "answer": answer,
            "citations": [citation(r) for r in rows], "mock": any(r["mock"] for r in rows),
            "disclaimer": PRICE_NOTE if rows else None}


def answer(spec, items, today=None):
    """Thực thi spec trên bảng giá đã duyệt, còn hiệu lực. Không gọi model."""
    current = active_items(items, today)
    by_sku = {}
    for item in current:
        by_sku.setdefault(item["sku"], []).append(item)
    if spec["kind"] in ("lookup", "compare"):
        involved = [s for s in spec["skus"] if s in by_sku]
    else:
        involved = [s for s, rows in by_sku.items()
                    if (not spec["category"] or rows[0]["category"] == spec["category"])]
    if any(len({r["price_vnd"] for r in by_sku[s]}) > 1 for s in involved):
        return _result("handoff", "conflicting_sources",
                       "Bảng giá đang có hai mức giá còn hiệu lực cho cùng sản phẩm. Cần nhân viên xác minh.")
    label = CATEGORIES.get(spec["category"], "sản phẩm")

    if spec["kind"] in ("lookup", "compare"):
        rows = [by_sku[s][0] for s in involved][:6]
        if not rows:
            return _result("handoff", "catalog_missing",
                           "Mình chưa có giá đang áp dụng cho sản phẩm này. Cần nhân viên kiểm tra.")
        if spec["kind"] == "lookup" or len(rows) == 1:
            item = rows[0]
            note = f" Sản phẩm này {STATUS_NOTES[item['status']]}." if item["status"] in STATUS_NOTES else ""
            return _result("catalog", "catalog_lookup",
                           f"Giá niêm yết của {item['name'].strip()} ({item['sku']}) là {price_text(item)}.{note}",
                           rows)
        lines = [f"{r['name'].strip()} – {vnd(r['price_vnd'])}"
                 + (f" ({STATUS_NOTES[r['status']]})" if r["status"] in STATUS_NOTES else "") for r in rows]
        cheapest = min(rows, key=lambda r: r["price_vnd"])
        priciest = max(rows, key=lambda r: r["price_vnd"])
        text = "So sánh theo bảng giá:\n" + "\n".join(f"• {line}" for line in lines)
        if cheapest["price_vnd"] != priciest["price_vnd"]:
            text += (f"\n{cheapest['name'].strip()} rẻ hơn {priciest['name'].strip()} "
                     f"{vnd(priciest['price_vnd'] - cheapest['price_vnd'])}.")
        return _result("catalog", "catalog_compare", text, rows)

    pool = [by_sku[s][0] for s in involved if by_sku[s][0]["status"] == "selling"]
    if not pool:
        return _result("handoff", "catalog_missing",
                       f"Mình chưa có bảng giá đang áp dụng cho {label}. Cần nhân viên kiểm tra.")
    filters = spec["filters"]
    rows = [r for r in pool if matches(r, filters)]
    condition = describe_filters(filters)
    if not rows:
        cheapest = min(pool, key=lambda r: r["price_vnd"])
        text = f"Hiện không có {label} nào {condition} trong bảng giá đang áp dụng."
        if filters.get("price_max"):
            text += f" Mẫu có giá thấp nhất là {cheapest['name'].strip()} – {vnd(cheapest['price_vnd'])}."
            return _result("catalog", "catalog_no_match", text, [cheapest])
        return _result("catalog", "catalog_no_match", text)

    field, order = spec["sort_by"], spec["order"]
    rows = [r for r in rows if _value(r, field) is not None]
    if not rows:
        return _result("handoff", "catalog_missing",
                       f"Bảng giá chưa có thông số này cho {label}. Cần nhân viên kiểm tra.")
    rows.sort(key=lambda r: (_value(r, field) * (-1 if order == "desc" else 1), r["price_vnd"]))
    suffix = f" ({condition})" if condition else ""
    if spec["kind"] == "rank":
        best = _value(rows[0], field)
        top = [r for r in rows if _value(r, field) == best][:3]
        criterion = CRITERIA.get((field, order), "phù hợp nhất")
        names = " và ".join(f"{r['name'].strip()} ({r['sku']})" for r in top)
        text = f"{label.capitalize()} có {criterion}{suffix} đang bán là {names}"
        if field == "price_vnd":
            text += f", giá niêm yết {vnd(top[0]['price_vnd'])}."
        else:
            unit_label, unit = SPEC_FIELDS[field]
            text += (f": {unit_label} {_num(best)} {unit}, giá niêm yết "
                     + ", ".join(vnd(r["price_vnd"]) for r in top) + ".")
        return _result("catalog", "catalog_rank", text, top)

    shown = rows[:LIST_LIMIT]
    order_text = "giá tăng dần" if (field, order) == ("price_vnd", "asc") else CRITERIA.get((field, order), "")
    text = (f"Có {len(rows)} {label}{suffix} đang bán, xếp theo {order_text}:\n"
            + "\n".join(f"{n}) {r['name'].strip()} – {vnd(r['price_vnd'])}" for n, r in enumerate(shown, 1)))
    if len(rows) > len(shown):
        text += f"\n(Hiển thị {len(shown)} mẫu đầu.)"
    return _result("catalog", "catalog_list", text, shown)
