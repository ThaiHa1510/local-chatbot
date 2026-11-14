"""Manual live check: python3 tests/live_chat_check.py (requires local Ollama)."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import all_records, ask
records=all_records()
cases=[
 ('AUDIO-B2','chính sách bảo hành và đổi trả',None,{'warranty','returns'}),
 ('LAP-A14','chính sách bảo hành và đổi trả',None,{'warranty','returns'}),
 ('PHONE-P6','chính sách bảo hành và đổi trả',None,{'warranty','returns'}),
 ('MON-M27','chính sách bảo hành và đổi trả',None,{'warranty','returns'}),
 ('AUDIO-B2','Chính sách đó có điều kiện gì?',[{'product':'AUDIO-B2','question':'đổi trả'}],{'returns'}),
]
for sku,q,h,expected in cases:
 r=ask(q,sku,records,model='Qwen:latest',history=h)
 keys={c['fact_key'] for c in r['citations']}
 print(json.dumps({'product':sku,'question':q,'status':r['status'],'reason':r['reason'],'keys':sorted(keys),'context_used':r['context_used']},ensure_ascii=False),flush=True)
 assert expected <= keys,(sku,r)
