"""Manual live check; requires local Ollama."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import all_records, ask

records = all_records()
cases = [
    ("LAP-A14", "Có giao hàng tận nơi không?", {"shipping_scope"}),
    ("PHONE-P6", "Địa chỉ và giờ mở cửa của cửa hàng?", {"store_address", "opening_hours"}),
    ("AUDIO-B2", "Cửa hàng hỗ trợ phương thức thanh toán nào?", {"payment_methods"}),
]
for sku, question, expected in cases:
    result = ask(question, sku, records, model="Qwen:latest")
    keys = {citation["fact_key"] for citation in result["citations"]}
    print(sku, result["status"], sorted(keys), flush=True)
    assert result["status"] == "evidence", result
    assert expected <= keys, result
    assert all(citation["product"] == "STORE-POLICY" for citation in result["citations"]), result
