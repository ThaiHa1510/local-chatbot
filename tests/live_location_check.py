"""Manual live API-equivalent check for store-location phrasings."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import all_records, ask

records = all_records()
for question in ("Danh sách cửa hàng", "Các chi nhánh", "Cửa hàng gần đây",
                 "Hệ thống cửa hàng"):
    result = ask(question, "LAP-A14", records, model="Qwen:latest")
    keys = {citation["fact_key"] for citation in result["citations"]}
    print(question, result["status"], sorted(keys), flush=True)
    assert result["status"] == "evidence", result
    assert "store_address" in keys, result
    assert all(citation["product"] == "STORE-POLICY"
               for citation in result["citations"]), result
