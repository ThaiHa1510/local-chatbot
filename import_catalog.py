"""Nhập bảng giá từ file JSON bên ngoài vào data/catalog_<tên>.json.

Ví dụ:
    .venv/bin/python import_catalog.py data/sources/phones.json --name phones --days 14
    .venv/bin/python import_catalog.py --all          # mọi data/sources/<tên>.json → catalog_<tên>.json
    .venv/bin/python import_catalog.py --all --missing-only   # chỉ tạo bảng giá còn thiếu (start.command dùng)
    .venv/bin/python import_catalog.py --all --from-today     # DEMO: tính hạn giá từ hôm nay

- Bỏ dòng không có giá (price_vnd null/0) và báo lại.
- valid_to null → valid_from + --days ngày, để giá cũ tự hết hiệu lực thay vì trả lời sai.
- specs dạng chữ (chipset, battery) chuyển sang details; chỉ giữ số cho ram/bộ nhớ/màn hình/cân nặng.
- Máy phổ thông (màn hình < 4 inch) bỏ ram_gb/storage_gb vì nguồn thường ghi MB thành GB.
- Thêm alias rút gọn từ tên ("iPhone 16 128GB | Chính hãng VN/A" → "iphone 16").
- Bảng "specifications" (nếu có) bổ sung details (GPU, độ phân giải, hệ điều hành, cổng…) và
  số đo (tần số quét Hz, pin Wh, VRAM GB, giờ pin tai nghe/loa) — chỉ điền trường mà "specs" còn thiếu.
- Giữ colors, phiên bản theo màu (variants: sku, màu, giá, trạng thái) và image_url.
- Kiểm tra lại bằng catalog.load_catalog trước khi ghi; lỗi thì không ghi gì.
"""
import argparse
import html
from datetime import date, timedelta
import json
from pathlib import Path
import re
import tempfile

import catalog

ROOT = Path(__file__).resolve().parent
NOISE = re.compile(r"\b(\d+\s*(gb|tb)|5g|4g|nfc|vn/a|chinh hang|chính hãng)\b", re.I)
HYPHENS = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-"})
DESKTOP = re.compile(r"\b(mac mini|mac studio|imac|pc|desktop)\b", re.I)
# Phần đuôi tên chỉ mô tả cấu hình: "MacBook Air M5 13 inch 2026 10CPU…" → họ máy "macbook air m5".
FAMILY_CUT = re.compile(r"\s(\d+(\.\d+)?\s*inch|20\d\d|\d+\s*cpu|\d+\s*gpu|\d+\s*(gb|tb))\b.*$", re.I)


# Từ mô tả đứng trước hãng: "Tai nghe Bluetooth True Wireless JBL…" → "jbl…".
DESCRIPTOR = re.compile(
    r"^(điện thoại|dien thoai|laptop|trẻ em|tai nghe|loa|microphone|máy ghi âm|máy trợ giảng|bluetooth|"
    r"true wireless|tws|ows|không dây|khong day|có dây|co day|chụp tai|chup tai|nhét tai|nhet tai|"
    r"thể thao|the thao|kiểm âm|kiem am|thanh|soundbar|vi tính|vi tinh|chống ồn|chong on|karaoke|"
    r"phiên dịch cảm ứng|thu âm|tích hợp ai|cổng 3\.5mm|gaming)\s+", re.I)
# Hãng thường gặp khi nguồn để manufacturer = null; cụm dài đứng trước.
KNOWN_BRANDS = ("Harman Kardon", "Alpha Works", "Apple", "AirPods:Apple", "EarPods:Apple", "Beats",
                "Powerbeats:Beats", "JBL", "Sony", "Bose", "Anker", "Samsung", "Xiaomi", "Redmi:Xiaomi",
                "Huawei", "Marshall", "Sennheiser", "Baseus", "Edifier", "Havit", "HyperX", "SoundPeats",
                "QCY", "GOOJODOQ", "Nakamichi", "Tribit", "DJI", "Gochek", "Plaud", "StarGO", "Logitech",
                "Robot", "KZ", "OPPO", "HONOR", "Nothing", "Masstel", "Tecno", "Itel", "Nokia")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Khóa trong "specifications" của nguồn → khóa details của bảng giá.
SPECIFICATION_DETAILS = {
    "vga": "gpu", "display_resolution": "resolution", "os_version": "os", "nhu_cau_su_dung": "use_case",
    "laptop_special_feature": "features", "laptop_chat_lieu": "material", "npu": "npu",
    "laptop_camera_webcam": "webcam", "wlan": "wifi", "bluetooth": "bluetooth", "ports_slots": "ports",
    "dimensions": "dimensions", "warranty_information": "warranty", "battery": "battery",
    "cpu": "cpu", "laptop_tam_nen_man_hinh": "panel", "laptop_loai_ram": "ram_type",
    # Tai nghe / loa / micro: khóa đứng trước được ưu tiên khi cùng đích.
    "tai_nghe_thiet_ke": "design", "tai_nghe_tinh_nang": "features", "loa_tinh_nang": "features",
    "tai_nghe_nhu_cau_su_dung": "use_case", "loa_nhu_cau_su_dung": "use_case",
    "tai_nghe_bluetooth": "bluetooth", "loa_bluetooth": "bluetooth", "micro_bluetooth": "bluetooth",
    "tai_nghe_thoi_luong_dung_pin": "battery_life", "loa_thoi_luong_pin": "battery_life",
    "micro_thoi_luong_pin": "battery_life", "tai_nghe_pin": "battery_life",
    "tai_nghe_chong_nuoc": "water_resistance", "loa_chong_nuoc": "water_resistance",
    "tai_nghe_ket_noi": "connection", "tai_nghe_kich_thuoc_driver": "driver", "loa_cong_suat": "power",
    "tai_nghe_micro": "microphone",
}
LONG_DETAILS = {"ports", "warranty", "features", "use_case", "os", "battery_life"}
DGPU = re.compile(r"\b(rtx|gtx|geforce mx|mx\s?\d{3}|radeon rx\s?\d{3,4}|arc a\d{3})", re.I)


def clean_text(value, limit=120):
    value = " ".join(CONTROL.sub(" ", html.unescape(value)).split())
    if len(value) > limit:
        value = value[:limit].rsplit(" ", 1)[0]
    return value if re.search(r"[0-9A-Za-zÀ-ỹ]", value) else ""  # bỏ giá trị rác như "\\", "-"


def _number(pattern, text, low, high):
    m = re.search(pattern, text or "", re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    value = int(value) if value.is_integer() else value
    return value if low <= value <= high else None


def from_specifications(raw, specs, details):
    """Bổ sung details/specs từ bảng "specifications" nguồn; không ghi đè dữ liệu đã có."""
    raw = {k: v for k, v in (raw or {}).items() if isinstance(v, str)}
    for key, target in SPECIFICATION_DETAILS.items():
        if target not in details and raw.get(key):
            value = clean_text(raw[key], 200 if target in LONG_DETAILS else 120)
            if value:
                details[target] = value
    refresh = _number(r"(\d+(?:[.,]\d+)?)\s*hz", raw.get("laptop_tan_so_quet"), 30, 600)
    if refresh and "refresh_hz" not in specs:
        specs["refresh_hz"] = refresh
    battery = _number(r"(\d+(?:[.,]\d+)?)\s*(?:wh|whrs?|watt\W?gi[ờo])", raw.get("battery"), 10, 120)
    if battery and "battery_wh" not in specs:
        specs["battery_wh"] = battery
    # Thời lượng pin tai nghe/loa: số giờ đầu tiên ("Tai nghe: 10 giờ; Hộp sạc: 50 giờ" → 10).
    hours = _number(r"(\d+(?:[.,]\d+)?)\s*(?:giờ|giò|tiếng|h)\b", details.get("battery_life"), 1, 200)
    if hours and "battery_hours" not in specs:
        specs["battery_hours"] = hours
    gpu = raw.get("vga", "")
    if DGPU.search(gpu) and "vram_gb" not in specs:
        vram = _number(r"(\d+)\s*gb", gpu, 1, 32)
        if vram:
            specs["vram_gb"] = vram


def variants_of(row):
    """Phiên bản theo màu: giữ sku, màu, giá, trạng thái hợp lệ; bỏ trùng sku."""
    out = {}
    for v in row.get("variants") or row.get("versions") or []:
        if not isinstance(v, dict) or type(v.get("price_vnd")) is not int or v["price_vnd"] <= 0:
            continue
        status = v.get("status") or "selling"
        if status not in catalog.STATUSES:
            continue
        sku = str(v.get("sku") or v.get("id") or "").strip()
        original = v.get("original_price_vnd")
        out[sku or len(out)] = {
            "sku": sku, "color": clean_text(v.get("color") or "", 60), "price_vnd": v["price_vnd"],
            "original_price_vnd": original if type(original) is int and original > 0 else None,
            "status": status}
    return list(out.values())


def short_alias(text):
    text = text.split("|")[0].lower()
    previous = None
    while previous != text:  # bỏ lần lượt mọi từ mô tả ở đầu tên
        previous, text = text, DESCRIPTOR.sub("", text.strip())
    text = NOISE.sub(" ", text.replace("-", " "))
    return " ".join(text.replace("(", " ").replace(")", " ").split())


def infer_brand(name):
    folded = catalog.fold(name)
    for entry in KNOWN_BRANDS:
        word, _, brand = entry.partition(":")
        if re.search(r"(?<![a-z0-9])" + re.escape(catalog.fold(word)) + r"(?![a-z0-9])", folded):
            return brand or word
    return None


def brandless_alias(alias, brand):
    """"apple airpods 4" → "airpods 4"; bỏ nếu phần còn lại quá ngắn ("jbl go 5" → không tạo "go 5")."""
    if not brand:
        return None
    prefix = brand.lower() + " "
    if not alias.startswith(prefix):
        return None
    rest = alias[len(prefix):]
    return rest if len(rest) >= 6 and len(rest.split()) >= 2 else None


def model_alias(alias):
    """Cắt sau số model đầu tiên: "airpods 4 chống ồn chủ động" → "airpods 4"."""
    words = alias.split()
    for i, word in enumerate(words):
        if re.search(r"\d", word):
            cut = " ".join(words[:i + 1])
            return cut if cut != alias and len(words[:i + 1]) >= 2 and len(cut) >= 6 else None
    return None


AUDIO_KIND = (("speaker", re.compile(r"^\s*(loa|máy trợ giảng)\b", re.I)),
              ("microphone", re.compile(r"^\s*(microphone|máy ghi âm)\b", re.I)))


def family_alias(name):
    """Họ máy cho tên dài kiểu Apple; None nếu không rút gọn được an toàn (cần ≥ 3 từ)."""
    base = short_alias(name)
    family = FAMILY_CUT.sub("", base).strip()
    return family if family != base and len(family.split()) >= 3 else None


def prefix_alias(name):
    """Bỏ hậu tố mã cuối ("hp 14 em0023au d0bg7pa" → "hp 14 em0023au") để khách gõ thiếu vẫn khớp."""
    words = short_alias(name).replace("-", " ").split()
    last = words[-1] if words else ""
    if len(words) >= 4 and len(last) >= 5 and re.search(r"\d", last) and re.search(r"[a-z]", last):
        return " ".join(words[:-1])
    return None


def fix_storage(specs, notes):
    """Nguồn ghi 1/2 cho 1TB/2TB và 524288 cho 512GB; đưa về GB."""
    value = specs.get("storage_gb")
    if value is None:
        return
    if value <= 8:
        specs["storage_gb"] = int(value * 1024)
        notes.append(f"bộ nhớ {value} → {specs['storage_gb']} GB (TB)")
    elif value >= 100000:
        specs["storage_gb"] = int(value // 1024)
        notes.append(f"bộ nhớ {value} → {specs['storage_gb']} GB")


def normalize(row, days, from_today=False):
    """Trả (item, ghi chú) hoặc (None, lý do bỏ)."""
    price = row.get("price_vnd")
    if type(price) is not int or price <= 0:
        return None, "không có giá"
    status = row.get("status") or "selling"
    if status not in catalog.STATUSES:
        return None, f"trạng thái lạ: {status}"
    specs, details, notes = {}, {}, []
    for key, value in (row.get("specs") or {}).items():
        if key in catalog.SPEC_FIELDS and type(value) in (int, float) and value > 0:
            specs[key] = value
        elif key in catalog.DETAIL_FIELDS and isinstance(value, str):
            value = clean_text(value)
            if value:
                details[key] = value
    from_specifications(row.get("specifications"), specs, details)
    fix_storage(specs, notes)
    if specs.get("screen_inch", 99) < 4 and {"ram_gb", "storage_gb"} & specs.keys():
        specs.pop("ram_gb", None)
        specs.pop("storage_gb", None)
        notes.append("máy phổ thông: bỏ RAM/bộ nhớ (nguồn có thể ghi MB thành GB)")
    if from_today:  # chỉ cho demo: coi giá trong nguồn là giá của hôm nay
        valid_from, valid_to = date.today().isoformat(), None
    else:
        valid_from, valid_to = row.get("valid_from") or date.today().isoformat(), row.get("valid_to")
    valid_to = valid_to or (date.fromisoformat(valid_from) + timedelta(days=days)).isoformat()
    name = " ".join(row["name"].translate(HYPHENS).split())
    category = row["category"]
    if category == "laptop" and DESKTOP.search(name):
        category = "desktop"
        notes.append("máy để bàn: chuyển sang desktop")
    if category == "audio":
        for kind, pattern in AUDIO_KIND:
            if pattern.search(name):
                category = kind
                break
    brand = (row.get("manufacturer") or "").strip() or infer_brand(name)
    aliases = [a.translate(HYPHENS).strip().lower() for a in row.get("aliases") or []
               if isinstance(a, str) and a.strip()]
    aliases += [short_alias(name)] + [short_alias(a) for a in aliases] + [family_alias(name) or "", prefix_alias(name) or ""]
    aliases += [brandless_alias(a, brand) or "" for a in list(aliases)]
    if category in ("audio", "speaker", "microphone"):
        aliases += [model_alias(a) or "" for a in list(aliases)]
    aliases = [a for a in dict.fromkeys(aliases) if len(a) >= 3]
    original = row.get("original_price_vnd")
    item = {
        "id": row["id"], "sku": row["sku"], "category": category, "name": name,
        "aliases": aliases, "price_vnd": price,
        "original_price_vnd": original if type(original) is int and original > 0 else None,
        "status": status, "specs": specs, "details": details,
        "manufacturer": brand,
        "source": row["source"], "valid_from": valid_from, "valid_to": valid_to,
        "approved": bool(row.get("approved", False)), "mock": bool(row.get("mock", False)),
        "colors": list(dict.fromkeys(c for c in (clean_text(c, 60) for c in row.get("colors") or []
                                                 if isinstance(c, str)) if c)),
        "variants": variants_of(row),
        "image_url": image if isinstance(image := row.get("image_url"), str) and image.startswith("https://") else None,
    }
    return item, "; ".join(notes)


SOURCES = ROOT / "data" / "sources"


def import_file(source, name, days=14, from_today=False):
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise SystemExit("--name chỉ gồm chữ thường, số, gạch dưới.")
    rows = json.loads(Path(source).read_text(encoding="utf-8"))
    items, skipped, noted = [], [], []
    for row in rows:
        item, note = normalize(row, days, from_today)
        if item is None:
            skipped.append(f"{row.get('id')} {row.get('name', '').strip()}: {note}")
            continue
        items.append(item)
        if note:
            noted.append(f"{item['id']} {item['name']}: {note}")
    out = catalog.CATALOG_PATH.parent / f"catalog_{name}.json"
    # Kiểm tra trên bản tạm, gộp cùng các bảng giá khác để bắt ID trùng.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_file = Path(tmp) / "check.json"
        others = [i for p in catalog.catalog_files() if p != out and p.exists()
                  for i in json.loads(p.read_text(encoding="utf-8"))]
        tmp_file.write_text(json.dumps(others + items, ensure_ascii=False), encoding="utf-8")
        catalog.load_catalog(tmp_file)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    until = min((i["valid_to"] for i in items), default="-")
    print(f"Đã ghi {len(items)} sản phẩm vào {out.relative_to(ROOT)} (giá hiệu lực đến {until}); "
          f"bỏ {len(skipped)} dòng.")
    for line in skipped:
        print("  BỎ:", line)
    for line in noted:
        print("  SỬA:", line)
    return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?")
    parser.add_argument("--name", help="ghi ra data/catalog_<name>.json")
    parser.add_argument("--all", action="store_true", help="nhập mọi data/sources/<tên>.json")
    parser.add_argument("--missing-only", action="store_true", help="với --all: bỏ qua bảng giá đã có")
    parser.add_argument("--from-today", action="store_true",
                        help="DEMO: hạn giá tính từ hôm nay thay vì ngày lấy dữ liệu trong nguồn")
    parser.add_argument("--days", type=int, default=14, help="hạn giá khi nguồn không có valid_to")
    args = parser.parse_args()
    if args.all:
        sources = sorted(SOURCES.glob("*.json"))
        if not sources:
            raise SystemExit(f"Không có file nguồn trong {SOURCES.relative_to(ROOT)}/")
        if not catalog.CATALOG_PATH.exists():
            catalog.CATALOG_PATH.write_text("[]\n", encoding="utf-8")
        today = date.today().isoformat()
        for src in sources:
            out = catalog.CATALOG_PATH.parent / f"catalog_{src.stem}.json"
            if args.missing_only and out.exists():
                rows = json.loads(out.read_text(encoding="utf-8"))
                if rows and max(r["valid_to"] for r in rows) < today:
                    print(f"CẢNH BÁO: giá trong {out.name} đã hết hạn — chạy "
                          "`import_catalog.py --all --from-today` nếu chỉ để demo.")
                continue
            import_file(src, src.stem, args.days, args.from_today)
        return
    if not args.source or not args.name:
        parser.error("cần SOURCE và --name, hoặc dùng --all")
    import_file(args.source, args.name, args.days, args.from_today)


if __name__ == "__main__":
    main()
