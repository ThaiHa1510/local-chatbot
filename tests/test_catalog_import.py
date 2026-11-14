import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import ask
import catalog
import import_catalog
import catalog_sql

TODAY = "2026-11-12"


def row(id, sku, name, price, specs, status="selling", aliases=(), manufacturer="Apple", original=None):
    return {"id": id, "sku": sku, "category": "phone", "name": name, "aliases": list(aliases),
            "price_vnd": price, "original_price_vnd": original, "status": status, "specs": specs,
            "manufacturer": manufacturer, "source": f"https://cellphones.com.vn/{sku.lower()}.html",
            "valid_from": "2026-11-11", "valid_to": None, "approved": True, "mock": False,
            "image_url": "https://example.invalid/x.jpg"}


# Các dòng chép từ dữ liệu cellphones (rút gọn trường không dùng).
SOURCE = [
    row("CAT-PHONE-112580", "IPHONE-17-256GB", "iPhone 17 256GB | Chính hãng", 28490000,
        {"ram_gb": 8, "storage_gb": 256, "screen_inch": 6.3, "weight_kg": 0.177, "chipset": "Apple A19",
         "battery": "Xem video: 30 giờ Xem video trực tuyến: 27 giờ"},
        aliases=["iphone 17", "iphone-17-256gb", "iphone-17"], original=28990000),
    row("CAT-PHONE-112583", "IPHONE-17-512GB", "iPhone 17 512GB | Chính hãng", 33990000,
        {"ram_gb": 12, "storage_gb": 512, "screen_inch": 6.3, "weight_kg": 0.177, "chipset": "Apple A19"},
        aliases=["iphone 17", "iphone-17-512gb", "iphone-17"], original=35490000),
    row("CAT-PHONE-112586", "IPHONE-17-PRO", "iPhone 17 Pro 256GB | Chính hãng", 31990000,
        {"storage_gb": 256, "screen_inch": 6.3, "weight_kg": 0.204, "chipset": "Chip A19 Pro"},
        aliases=["iphone 17 pro", "iphone-17-pro"], original=34990000),
    row("CAT-PHONE-59258", "IPHONE-16-PRO-MAX", "Điện thoại iPhone 16 Pro Max 256GB", 30990000,
        {"ram_gb": 8, "storage_gb": 256, "screen_inch": 6.9, "weight_kg": 0.227, "chipset": "Apple A18 Pro"},
        status="out_of_stock", aliases=["điện thoại iphone 16 pro max", "iphone-16-pro-max"], original=34990000),
    row("CAT-PHONE-59254", "IPHONE-16", "iPhone 16 128GB | Chính hãng VN/A", 24490000,
        {"ram_gb": 8, "storage_gb": 128, "screen_inch": 6.1, "weight_kg": 0.17, "chipset": "Apple A18",
         "battery": "4.300 mAh"},
        aliases=["iphone 16 vn/a", "iphone-16", "iphone-16-vn-a"], original=24990000),
    row("CAT-PHONE-109161", "DIEN-THOAI-SAMSUNG-GALAXY-A07", "Samsung Galaxy A07 4GB 128GB", 3990000,
        {"ram_gb": 4, "storage_gb": 128, "screen_inch": 6.7, "weight_kg": 0.184, "chipset": "MediaTek Helio G99",
         "battery": "Li-Ion 5000 mAh"},
        aliases=["samsung galaxy a07", "dien-thoai-samsung-galaxy-a07", "samsung-galaxy-a07"],
        manufacturer="Samsung", original=4590000),
    row("CAT-PHONE-125121", "DIEN-THOAI-SAMSUNG-GALAXY-S26-ULTRA", "Samsung Galaxy S26 Ultra 5G 12GB 256GB",
        30490000, {"ram_gb": 12, "storage_gb": 256, "screen_inch": 6.9, "weight_kg": 0.214,
                   "chipset": "Snapdragon 8 Elite Gen 5", "battery": "5000 mAh"},
        aliases=["samsung galaxy s26 ultra 5g", "dien-thoai-samsung-galaxy-s26-ultra"],
        manufacturer="Samsung", original=40990000),
    row("CAT-PHONE-88715", "NOKIA-HMD-105-4G", "Nokia HMD 105 4G", 750000,
        {"storage_gb": 128, "screen_inch": 2.4, "chipset": "Unisoc 8910 & T127", "battery": "1450 mAh"},
        aliases=["nokia hmd 105 4g", "nokia-hmd-105-4g"], manufacturer="Nokia"),
    row("CAT-PHONE-143742", "DIEN-THOAI-XIAOMI-18-PRO-MAX-16GB-512GB", "Xiaomi 18 Pro Max 5G 16GB 512GB", None,
        {}, status="out_of_stock", manufacturer="Xiaomi"),
]


class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        results = [import_catalog.normalize(r, 14) for r in SOURCE]
        cls.skipped = [r for (item, _), r in zip(results, SOURCE) if item is None]
        cls.items_raw = [item for item, _ in results if item]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog_phones.json"
            path.write_text(json.dumps(cls.items_raw, ensure_ascii=False), encoding="utf-8")
            cls.items = catalog.load_catalog(path)

    def setUp(self):
        guard = patch("agent.inspect_input", return_value={"allowed": True, "reason": "ok"})
        guard.start()
        self.addCleanup(guard.stop)

    def ask(self, question, product="PHONE-P6"):
        return ask(question, product, [], catalog=self.items, today=TODAY)

    def skus(self, result):
        return [c["product"] for c in result["citations"]]

    # ------------------------------------------------------------ chuẩn hóa
    def test_rows_without_price_are_skipped(self):
        self.assertEqual([r["id"] for r in self.skipped], ["CAT-PHONE-143742"])

    def test_normalization(self):
        by_id = {i["id"]: i for i in self.items}
        iphone16 = by_id["CAT-PHONE-59254"]
        self.assertEqual(iphone16["valid_to"], "2026-11-25")       # valid_from + 14 ngày
        self.assertIn("iphone 16", iphone16["aliases"])             # rút từ "iphone 16 vn/a"
        self.assertEqual(iphone16["details"]["chipset"], "Apple A18")
        self.assertNotIn("chipset", iphone16["specs"])
        self.assertNotIn("storage_gb", by_id["CAT-PHONE-88715"]["specs"])  # máy phổ thông
        self.assertEqual(iphone16["image_url"], "https://example.invalid/x.jpg")

    def test_prices_expire(self):
        r = ask("điện thoại rẻ nhất", "PHONE-P6", [], catalog=self.items, today="2026-12-01")
        self.assertEqual(r["reason"], "catalog_missing")

    # ------------------------------------------------------------ hỏi đáp
    def test_cheapest_phone_skips_out_of_stock(self):
        r = self.ask("điện thoại nào rẻ nhất")
        self.assertEqual(self.skus(r), ["NOKIA-HMD-105-4G"])
        r = self.ask("điện thoại nào đắt nhất")
        self.assertEqual(self.skus(r), ["IPHONE-17-512GB"])  # 16 Pro Max tạm hết hàng không tính

    def test_brand_filter(self):
        r = self.ask("điện thoại samsung nào rẻ nhất")
        self.assertEqual(self.skus(r), ["DIEN-THOAI-SAMSUNG-GALAXY-A07"])
        self.assertIn("hãng Samsung", r["answer"])
        r = self.ask("iphone nào rẻ nhất", "LAP-A14")
        self.assertEqual(self.skus(r), ["IPHONE-16"])

    def test_longest_name_wins(self):
        self.assertEqual(self.skus(self.ask("giá iphone 17 pro")), ["IPHONE-17-PRO"])
        r = self.ask("giá iphone 17 bao nhiêu")
        self.assertEqual(r["reason"], "catalog_compare")
        self.assertEqual(self.skus(r), ["IPHONE-17-256GB", "IPHONE-17-512GB"])
        self.assertEqual(self.skus(self.ask("giá iphone 16")), ["IPHONE-16"])

    def test_lookup_shows_original_price_and_stock(self):
        r = self.ask("giá samsung galaxy s26 ultra")
        self.assertIn("30.490.000đ (giá gốc 40.990.000đ)", r["answer"])
        r = self.ask("giá iphone 16 pro max")
        self.assertIn("tạm hết hàng", r["answer"])

    def test_compare_two_brands(self):
        r = self.ask("so sánh iphone 16 và samsung galaxy s26 ultra")
        self.assertEqual(r["reason"], "catalog_compare")
        self.assertIn("6.000.000đ", r["answer"])


if __name__ == "__main__":
    unittest.main()


def lap(id, sku, name, price, specs, status="selling", aliases=(), manufacturer="Apple", original=None):
    item = row(id, sku, name, price, specs, status, aliases, manufacturer, original)
    item["category"] = "laptop"
    return item


# Các dòng chép từ dữ liệu laptop cellphones.
LAPTOPS = [
    lap("CAT-LAP-137232", "LAPTOP-ASUS-ZENBOOK-14-UX3405CA-ST1713W", "Laptop ASUS Zenbook 14 UX3405CA-ST1713W",
        32490000, {"ram_gb": 16, "storage_gb": 512, "screen_inch": 14, "weight_kg": 1.2,
                   "cpu": "Intel Core Ultra 5 225H 1.7 GHz (18MB Cache, up to 4.9 GHz, 14 lõi, 14 luồng)",
                   "ram_type": "LPDDR5X on board", "panel": "Tấm nền Oled"},
        aliases=["asus zenbook 14 ux3405ca-st1713w", "laptop-asus-zenbook-14-ux3405ca-st1713w"],
        manufacturer="ASUS", original=38990000),
    lap("CAT-LAP-125874", "MACBOOK-NEO-13-A18-PRO-6-CPU-5-GPU-8GB-256GB",
        "MacBook Neo 13 inch A18 Pro 2026 6CPU 5GPU 8GB 256GB | Chính hãng Apple Việt Nam", 18790000,
        {"ram_gb": 8, "storage_gb": 256, "screen_inch": 13, "weight_kg": 1.23}, original=19490000),
    lap("CAT-LAP-125698", "MACBOOK-AIR-13-M5-10-CPU-8-GPU-16GB-512GB",
        "MacBook Air M5 13 inch 2026 10CPU 8GPU 16GB 512GB | Chính hãng Apple Việt Nam", 33190000,
        {"ram_gb": 16, "storage_gb": 512, "screen_inch": 13.6, "weight_kg": 1.23}, original=35990000),
    lap("CAT-LAP-129218", "MACBOOK-AIR-13-INCH-M5-10-CPU-8-GPU-16GB-512GB-2026-SAC-70W",
        "MacBook Air M5 13 inch 2026 10CPU 8GPU 16GB 512GB Sạc 70W | Chính hãng Apple Việt Nam", 33490000,
        {"ram_gb": 16, "storage_gb": 512, "screen_inch": 13.6, "weight_kg": 1.23}, original=36590000),
    lap("CAT-LAP-115962", "MACBOOK-PRO-14-INCH-M5-16GB-1TB",
        "MacBook Pro 14 M5 10CPU 10GPU 16GB 1TB | Chính hãng Apple Việt Nam", 53190000,
        {"ram_gb": 16, "storage_gb": 1, "screen_inch": 14.2, "weight_kg": 1.55}, original=54990000),
    lap("CAT-LAP-93958", "MAC-MINI-M4-2024-16GB-256GB",
        "Mac mini M4 2024 10CPU 10GPU 16GB 256GB | Chính hãng Apple Việt Nam", 21990000,
        {"ram_gb": 16, "storage_gb": 256, "weight_kg": 0.67}),
    lap("CAT-LAP-136820", "LAPTOP-DELL-INSPIRON-14-5441-71092476", "Laptop Dell Inspiron 14 5441 71092476",
        24990000, {"ram_gb": 16, "storage_gb": 524288, "screen_inch": 14, "weight_kg": 1.66},
        manufacturer="Dell", original=28990000),
    lap("CAT-LAP-127303", "LAPTOP-HP-14-EM0023AU-D0BG7PA", "Laptop HP 14‑EM0023AU D0BG7PA", 21490000,
        {"ram_gb": 16, "storage_gb": 512, "screen_inch": 14, "weight_kg": 1.4},
        aliases=["hp 14‑em0023au d0bg7pa", "hp-14-em0023au-d0bg7pa"], manufacturer="HP", original=23990000),
    lap("CAT-LAP-144919", "LAPTOP-THUNDEROBOT-T-BOOK-15A", "Laptop Thunderobot T-Book 15A", 13590000,
        {"ram_gb": 8, "storage_gb": 512, "screen_inch": 15.6, "weight_kg": 1.8},
        aliases=["thunderobot t-book 15a"], manufacturer="Thunderobot", original=14190000),
    lap("CAT-LAP-106917", "LAPTOP-ASUS-VIVOBOOK-GO-15-E1504GA-BQ1141W", "Laptop ASUS Vivobook Go 15 E1504GA-BQ1141W",
        10990000, {"ram_gb": 8, "storage_gb": 256, "screen_inch": 15.6, "weight_kg": 1.63},
        status="out_of_stock", manufacturer="ASUS"),
]


LAPTOPS += [
    lap("CAT-LAP-116688", "LAPTOP-MSI-CYBORG-15-A13UC-2082VN", "Laptop MSI Cyborg 15 A13UC-2082VN", 28990000,
        {"ram_gb": 16, "storage_gb": 512, "screen_inch": 15.6, "weight_kg": 1.98,
         "cpu": "Intel Core i7-13620H Gen 13th (10 lõi / 16 luồng, 24MB Cache)", "ram_type": "DDR5 5600",
         "panel": "Tấm nền IPS"}, manufacturer="MSI", original=30690000),
    lap("CAT-LAP-142512", "LAPTOP-ASUS-TUF-GAMING-A15-FA506NCG-HN396W", "Laptop ASUS TUF Gaming A15 FA506NCG-HN396W",
        25990000, {"ram_gb": 8, "storage_gb": 512, "screen_inch": 15.6, "weight_kg": 2.3,
                   "cpu": "AMD Ryzen 7 155 (6 nhân 12 luồng, up to 4.70GHz, 22MB Cache)",
                   "panel": "Tấm nền IPS"}, manufacturer="ASUS", original=32990000),
    lap("CAT-LAP-139787", "LAPTOP-LENOVO-IDEAPAD-SLIM-5-14AKP10-83HX00F5VN-RYZEN-AI-5",
        "Laptop Lenovo IdeaPad Slim 5 14AKP10 83HX00F5VN", 31990000,
        {"ram_gb": 16, "storage_gb": 512, "screen_inch": 14, "weight_kg": 1.39,
         "cpu": "AMD Ryzen AI 5 330 (4 lõi / 8 luồng, 2.0 / 4.5GHz, 4MB L2 / 8MB L3)",
         "panel": "Tấm nền Oled"}, manufacturer="Lenovo", original=34990000),
]


class LaptopImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = [import_catalog.normalize(r, 14)[0] for r in LAPTOPS]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog_laptops.json"
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            cls.items = catalog.load_catalog(path)
        cls.by_id = {i["id"]: i for i in cls.items}

    def setUp(self):
        guard = patch("agent.inspect_input", return_value={"allowed": True, "reason": "ok"})
        guard.start()
        self.addCleanup(guard.stop)

    def ask(self, question):
        return ask(question, "LAP-A14", [], catalog=self.items, today=TODAY)

    def skus(self, result):
        return [c["product"] for c in result["citations"]]

    def test_storage_units_fixed(self):
        self.assertEqual(self.by_id["CAT-LAP-115962"]["specs"]["storage_gb"], 1024)
        self.assertEqual(self.by_id["CAT-LAP-136820"]["specs"]["storage_gb"], 512)
        r = self.ask("laptop nào bộ nhớ lớn nhất")
        self.assertEqual(self.skus(r), ["MACBOOK-PRO-14-INCH-M5-16GB-1TB"])

    def test_desktops_split_from_laptops(self):
        self.assertEqual(self.by_id["CAT-LAP-93958"]["category"], "desktop")
        self.assertNotIn("MAC-MINI-M4-2024-16GB-256GB", self.skus(self.ask("có những laptop nào")))
        self.assertEqual(self.skus(self.ask("máy tính để bàn nào rẻ nhất")), ["MAC-MINI-M4-2024-16GB-256GB"])

    def test_cheapest_laptop_skips_out_of_stock(self):
        self.assertEqual(self.skus(self.ask("laptop nào rẻ nhất")), ["LAPTOP-THUNDEROBOT-T-BOOK-15A"])
        self.assertEqual(self.skus(self.ask("macbook nào rẻ nhất")),
                         ["MACBOOK-NEO-13-A18-PRO-6-CPU-5-GPU-8GB-256GB"])

    def test_family_name_lists_variants(self):
        r = self.ask("giá macbook air m5")
        self.assertEqual(r["reason"], "catalog_compare")
        self.assertEqual(len(r["citations"]), 2)

    def test_special_hyphen_in_model_code(self):
        r = self.ask("giá hp 14-em0023au")
        self.assertEqual((r["reason"], self.skus(r)), ("catalog_lookup", ["LAPTOP-HP-14-EM0023AU-D0BG7PA"]))

    def test_cpu_shown_in_details(self):
        r = self.ask("giá asus zenbook 14 ux3405ca-st1713w")
        self.assertIn("CPU Intel Core Ultra 5 225H", r["citations"][0]["text"])
        self.assertIn("32.490.000đ (giá gốc 38.990.000đ)", r["answer"])

    # ------------------------------------------------------------ từ khóa
    def test_gaming_keyword(self):
        r = self.ask("laptop gaming nào rẻ nhất")
        self.assertEqual(self.skus(r), ["LAPTOP-ASUS-TUF-GAMING-A15-FA506NCG-HN396W"])
        r = self.ask("laptop chơi game dưới 30 triệu")
        self.assertEqual(self.skus(r), ["LAPTOP-ASUS-TUF-GAMING-A15-FA506NCG-HN396W",
                                        "LAPTOP-MSI-CYBORG-15-A13UC-2082VN"])
        self.assertIn("gaming", r["answer"])

    def test_oled_keyword(self):
        r = self.ask("laptop oled nào rẻ nhất")
        self.assertEqual(self.skus(r), ["LAPTOP-LENOVO-IDEAPAD-SLIM-5-14AKP10-83HX00F5VN-RYZEN-AI-5"])

    def test_cpu_keywords(self):
        self.assertEqual(self.skus(self.ask("laptop core i7 rẻ nhất")), ["LAPTOP-MSI-CYBORG-15-A13UC-2082VN"])
        self.assertEqual(self.skus(self.ask("laptop ryzen 7")), ["LAPTOP-ASUS-TUF-GAMING-A15-FA506NCG-HN396W"])
        self.assertEqual(self.skus(self.ask("laptop core ultra 5 nào rẻ nhất")),
                         ["LAPTOP-ASUS-ZENBOOK-14-UX3405CA-ST1713W"])
        r = self.ask("laptop amd nào rẻ nhất")
        self.assertEqual(self.skus(r), ["LAPTOP-ASUS-TUF-GAMING-A15-FA506NCG-HN396W"])

    def test_apple_chip_keyword(self):
        r = self.ask("macbook chip m5 nào rẻ nhất")
        self.assertEqual(self.skus(r), ["MACBOOK-AIR-13-M5-10-CPU-8-GPU-16GB-512GB"])

    def test_keyword_without_match(self):
        r = self.ask("laptop core i9 rẻ nhất")
        self.assertEqual(r["reason"], "catalog_no_match")
        self.assertIn("Core i9", r["answer"])

    def test_keyword_detection_is_whitelisted(self):
        self.assertEqual(catalog.detect_keywords("laptop bao hanh bao lau"), [])
        self.assertEqual(catalog.detect_keywords("man hinh m27"), [])


def audio(id, sku, name, price, specs, status="selling", aliases=(), original=None):
    item = row(id, sku, name, price, specs, status, aliases, None, original)
    item["category"] = "audio"
    return item


# Các dòng chép từ dữ liệu audio cellphones (manufacturer = null như nguồn).
AUDIO = [
    audio("CAT-AUDIO-42499", "APPLE-AIRPODS-4", "Tai nghe Bluetooth Apple AirPods 4 | Chính hãng Apple Việt Nam",
          3090000, {"battery_life": "Thời gian nghe lên đến 5 giờ với một lần sạc", "microphone": "Có"},
          aliases=["bluetooth apple airpods 4 | chính hãng apple việt nam", "apple-airpods-4"], original=3790000),
    audio("CAT-AUDIO-90111", "APPLE-AIRPODS-4-CHONG-ON-CHU-DONG",
          "Tai nghe Bluetooth Apple AirPods 4 chống ồn chủ động | Chính hãng Apple Việt Nam", 4090000,
          {"microphone": "Có"},
          aliases=["bluetooth apple airpods 4 chống ồn chủ động | chính hãng apple việt nam"], original=4990000),
    audio("CAT-AUDIO-58850", "TAI-NGHE-CHUP-TAI-SONY-WH-1000XM6", "Tai nghe Bluetooth chụp tai Sony WH-1000XM6",
          9490000, {"connection": "Không dây", "battery_life": "Tắt chống ồn: 40 giờ; Bật chống ồn: 30 giờ"},
          aliases=["bluetooth chụp tai sony wh-1000xm6", "tai-nghe-chup-tai-sony-wh-1000xm6"], original=11990000),
    audio("CAT-AUDIO-63021", "T-SN-13", "Tai nghe Bluetooth chụp tai Sony WH-CH520", 790000,
          {"connection": "Không dây", "battery_life": "50 giờ"},
          aliases=["bluetooth chụp tai sony wh-ch520", "t-sn-13"], original=1290000),
    audio("CAT-AUDIO-93635", "TAI-NGHE-KHONG-DAY-ANKER-SOUNDCORE-R50I-NC",
          "Tai nghe Bluetooth True Wireless Anker Soundcore R50I NC", 460000,
          {"connection": "Không dây", "battery_life": "Tai nghe: 10 giờ; Hộp sạc: 45 giờ"},
          aliases=["bluetooth true wireless anker soundcore r50i nc"], original=480000),
    audio("CAT-AUDIO-85814", "TAI-NGHE-CO-DAY-ROBOT-RE101S-3-5MM", "Tai nghe có dây cổng 3.5mm Robot RE101S",
          45000, {"driver": "10 mm", "microphone": "Có"}, aliases=["có dây cổng 3.5mm robot re101s"], original=50000),
    audio("CAT-AUDIO-90749", "TAI-NGHE-BLUETOOTH-ALPHA-WORKS-CURVE-100",
          "Tai nghe Bluetooth True Wireless Alpha Works Curve 100", 99000, {"microphone": "4 mic"},
          status="out_of_stock", original=190000),
    audio("CAT-AUDIO-49410", "TN-SN-04", "Tai nghe chụp tai Gaming Sony Inzone H3", 1050000,
          {"connection": "Có dây", "driver": "40 mm"}, aliases=["chụp tai gaming sony inzone h3"], original=1550000),
    audio("CAT-AUDIO-61535", "T-JBL-21", "Tai nghe Bluetooth True Wireless JBL Tune Beam", 1550000,
          {"connection": "Không dây", "weight": "Tai nghe: 5g ; Hộp sạc: \b 50.3 g"}, original=2490000),
    audio("CAT-AUDIO-126270", "LOA-JBL-GO-5", "Loa Bluetooth JBL Go 5", 1150000,
          {"battery_life": "8 giờ + 2 giờ cùng Playtime Boost", "power": "4.8 W RMS"},
          aliases=["bluetooth jbl go 5", "loa-jbl-go-5"], original=1290000),
    audio("CAT-AUDIO-79909", "LOA-BLUETOOTH-ROBOT-RB120", "Loa Robot RB120", 180000,
          {"connection": "Thẻ TF, cáp âm thanh Aux", "water_resistance": "IPX4 chống thấm nước, mồ hôi và chống mưa"},
          aliases=["robot rb120", "loa-bluetooth-robot-rb120"], original=210000),
    audio("CAT-AUDIO-73903", "LOA-BLUETOOTH-JBL-PARTYBOX-CLUB-120", "Loa JBL PartyBox Club 120", 7890000,
          {"power": "160W", "weight": "\\"}, original=10700000),
    audio("CAT-AUDIO-130416", "MICROPHONE-DIEN-THOAI-DJI-MIC-MINI-2-1-TX-1-RX",
          "Microphone không dây DJI Mic Mini 2 (1 TX + 1 MOBILE RX + CHARGING CASE)", 1745000,
          {"battery_life": "TX khoảng 11.5 giờ; RX khoảng 10.5 giờ"}),
]


class AudioImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = [import_catalog.normalize(r, 14)[0] for r in AUDIO]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog_audio.json"
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            cls.items = catalog.load_catalog(path)
        cls.by_id = {i["id"]: i for i in cls.items}

    def setUp(self):
        guard = patch("agent.inspect_input", return_value={"allowed": True, "reason": "ok"})
        guard.start()
        self.addCleanup(guard.stop)

    def ask(self, question):
        return ask(question, "AUDIO-B2", [], catalog=self.items, today=TODAY)

    def skus(self, result):
        return [c["product"] for c in result["citations"]]

    def test_subcategories_and_brands(self):
        self.assertEqual(self.by_id["CAT-AUDIO-42499"]["category"], "audio")
        self.assertEqual(self.by_id["CAT-AUDIO-126270"]["category"], "speaker")
        self.assertEqual(self.by_id["CAT-AUDIO-130416"]["category"], "microphone")
        self.assertEqual(self.by_id["CAT-AUDIO-42499"]["manufacturer"], "Apple")
        self.assertEqual(self.by_id["CAT-AUDIO-126270"]["manufacturer"], "JBL")
        self.assertEqual(self.by_id["CAT-AUDIO-90749"]["manufacturer"], "Alpha Works")

    def test_junk_detail_values_cleaned(self):
        self.assertNotIn("\b", self.by_id["CAT-AUDIO-61535"]["details"]["weight"])
        self.assertNotIn("weight", self.by_id["CAT-AUDIO-73903"]["details"])

    def test_headphones_vs_speakers(self):
        self.assertEqual(self.skus(self.ask("tai nghe nào rẻ nhất")), ["TAI-NGHE-CO-DAY-ROBOT-RE101S-3-5MM"])
        self.assertEqual(self.skus(self.ask("loa nào rẻ nhất")), ["LOA-BLUETOOTH-ROBOT-RB120"])
        self.assertEqual(self.skus(self.ask("loa bluetooth jbl rẻ nhất")), ["LOA-JBL-GO-5"])
        self.assertEqual(self.skus(self.ask("micro thu âm rẻ nhất")),
                         ["MICROPHONE-DIEN-THOAI-DJI-MIC-MINI-2-1-TX-1-RX"])

    def test_audio_keywords(self):
        self.assertEqual(self.skus(self.ask("tai nghe chống ồn rẻ nhất")),
                         ["TAI-NGHE-KHONG-DAY-ANKER-SOUNDCORE-R50I-NC"])
        self.assertEqual(self.skus(self.ask("tai nghe chụp tai chống ồn rẻ nhất")),
                         ["TAI-NGHE-CHUP-TAI-SONY-WH-1000XM6"])
        self.assertEqual(self.skus(self.ask("tai nghe gaming rẻ nhất")), ["TN-SN-04"])
        self.assertEqual(self.skus(self.ask("tai nghe bluetooth rẻ nhất")),
                         ["TAI-NGHE-KHONG-DAY-ANKER-SOUNDCORE-R50I-NC"])  # Alpha Works tạm hết hàng

    def test_names_without_brand_or_descriptor(self):
        r = self.ask("giá airpods 4")
        self.assertEqual(r["reason"], "catalog_compare")
        self.assertEqual(len(r["citations"]), 2)
        r = self.ask("giá sony wh-1000xm6")
        self.assertEqual((r["reason"], self.skus(r)), ("catalog_lookup", ["TAI-NGHE-CHUP-TAI-SONY-WH-1000XM6"]))
        self.assertEqual(self.skus(self.ask("giá wh-ch520")), ["T-SN-13"])


def _laptop(id, sku, name, price, specs, specifications, variants, colors):
    r = row(id, sku, name, price, specs)
    r.update(category="laptop", specifications=specifications, variants=variants, colors=colors,
             versions=variants)
    return r


NEW_FIELD_SOURCE = [
    _laptop("CAT-LAP-125874", "MACBOOK-NEO-13-A18-PRO-6-CPU-5-GPU-8GB-256GB",
            "MacBook Neo 13 inch A18 Pro 2026 6CPU 5GPU 8GB 256GB | Chính hãng Apple Việt Nam", 18790000,
            {"ram_gb": 8, "storage_gb": 256, "screen_inch": 13, "weight_kg": 1.23,
             "cpu": "Chip Apple A18 Pro CPU 6 lõi"},
            {"vga": "GPU 5 lõi Công nghệ dò tia", "battery": "Pin lithium-ion 36.5 watt‑giờ tích hợp",
             "display_resolution": "2408 x 1506 pixels", "os_version": "macOS",
             "macbook_anh_bao_mat": "/i/m/image_1800_8.png", "deposit_amount": "500000.0000",
             "included_accessories": "Bộ nguồn, máy, sách hdsd T&igrave;m hiểu"},
            [{"sku": "APP-NEO-BA", "color": "Bạc", "price_vnd": 19290000, "status": "selling"},
             {"sku": "APP-NEO-VA", "color": "Vàng Citrus", "price_vnd": 18790000, "status": "selling"},
             {"sku": "APP-NEO-HO", "color": "Hồng Phớt", "price_vnd": 19390000, "status": "out_of_stock"}],
            ["Bạc", "Vàng Citrus", "Hồng Phớt"]),
    _laptop("CAT-LAP-144019", "LAPTOP-LENOVO-LOQ-15IRH11E-83Y9003VVN", "Laptop Lenovo LOQ 15IRH11E 83Y9003VVN",
            26990000, {"ram_gb": 8, "storage_gb": 512, "screen_inch": 15.3, "cpu": "Intel Core 5 205H, 8 lõi"},
            {"vga": "NVIDIA GeForce RTX 3050 6GB GDDR6, Boost Clock 1732MHz", "battery": "60Wh, 135W Slim Tip",
             "laptop_tan_so_quet": "165Hz", "nhu_cau_su_dung": "Đồ họa - Kỹ thuật, Gaming",
             "warranty_information": "Bảo hành 24 tháng tại trung tâm bảo hành Chính hãng."},
            [{"sku": "LAP-LEN-LOQ-XA", "color": "Xám", "price_vnd": 26990000, "status": "selling"}], ["Xám"]),
]


class NewFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = [import_catalog.normalize(r, 14)[0] for r in NEW_FIELD_SOURCE]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog_laptops.json"
            path.write_text(json.dumps(cls.raw, ensure_ascii=False), encoding="utf-8")
            cls.items = catalog.load_catalog(path)
        cls.by_id = {i["id"]: i for i in cls.items}

    def test_specifications_mapped(self):
        loq = self.by_id["CAT-LAP-144019"]
        self.assertEqual(loq["specs"]["refresh_hz"], 165)
        self.assertEqual(loq["specs"]["battery_wh"], 60)
        self.assertEqual(loq["specs"]["vram_gb"], 6)
        self.assertIn("RTX 3050", loq["details"]["gpu"])
        self.assertEqual(loq["details"]["use_case"], "Đồ họa - Kỹ thuật, Gaming")
        neo = self.by_id["CAT-LAP-125874"]
        self.assertEqual(neo["specs"]["battery_wh"], 36.5)
        self.assertNotIn("vram_gb", neo["specs"])  # GPU tích hợp
        self.assertEqual(neo["details"]["os"], "macOS")
        self.assertFalse(any("image_1800" in v for v in neo["details"].values()))

    def test_variants_and_colors(self):
        neo = self.by_id["CAT-LAP-125874"]
        self.assertEqual(len(neo["variants"]), 3)
        self.assertEqual(catalog.color_text(neo), "màu Bạc, Vàng Citrus (hết hàng: Hồng Phớt)")
        self.assertIn("tùy màu 18.790.000đ – 19.290.000đ", catalog.price_text(neo))
        self.assertTrue(neo["image_url"].startswith("https://"))

    def test_sql_columns(self):
        import catalog_sql
        conn = catalog_sql.build_db(self.items, TODAY)
        cols, rows = catalog_sql.run_query(conn, "SELECT sku, has_dgpu, refresh_hz, battery_wh, colors "
                                                 "FROM products ORDER BY battery_wh DESC")
        self.assertEqual(rows[0], ("LAPTOP-LENOVO-LOQ-15IRH11E-83Y9003VVN", 1, 165, 60, "Xám"))
        self.assertEqual(rows[1][1], 0)
        self.assertEqual(rows[1][4], "Bạc, Vàng Citrus")

    def test_warranty_not_in_search_text(self):
        self.assertNotIn("bao hanh", catalog.search_text(self.by_id["CAT-LAP-144019"]))


class AudioSpecificationTests(unittest.TestCase):
    def _row(self, **spec):
        return {"id": "CAT-AUDIO-T1", "sku": "T1", "category": "audio", "name": "Tai nghe Bluetooth Sony WH-CH720N",
                "price_vnd": 2090000, "status": "selling", "source": "https://example.invalid/a",
                "specs": {}, "specifications": spec, "manufacturer": "Sony"}

    def test_audio_keys_and_battery_hours(self):
        item, _ = import_catalog.normalize(self._row(
            tai_nghe_thiet_ke="Chụp tai", tai_nghe_tinh_nang="Chống ồn, Chống ồn chủ động ANC",
            tai_nghe_thoi_luong_dung_pin="Bật ANC: 35 giờ; Tắt ANC: 50 giờ", tai_nghe_bluetooth="5.2"), 14)
        self.assertEqual(item["details"]["design"], "Chụp tai")
        self.assertEqual(item["details"]["bluetooth"], "5.2")
        self.assertEqual(item["specs"]["battery_hours"], 35)
        db = catalog_sql.build_db([dict(item, approved=True)])
        row = db.execute("SELECT has_anc, design, battery_hours FROM products").fetchone()
        self.assertEqual(tuple(row), (1, "Chụp tai", 35))

    def test_plugged_speaker_has_no_hours(self):
        item, _ = import_catalog.normalize(self._row(loa_thoi_luong_pin="Cắm nguồn trực tiếp"), 14)
        self.assertNotIn("battery_hours", item["specs"])
