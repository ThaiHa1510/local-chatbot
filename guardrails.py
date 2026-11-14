"""Kiểm tra prompt injection 2 lớp: luật (injection.py) rồi NeMo Guardrails qua Ollama."""
import logging
import os
from pathlib import Path
import threading

import injection

ROOT = Path(__file__).resolve().parent
_rails = None
_lock = threading.Lock()


def _instance():
    global _rails
    if _rails is None:
        with _lock:
            if _rails is None:
                from nemoguardrails import LLMRails, RailsConfig
                config = RailsConfig.from_path(str(ROOT / "guardrails_config"))
                _rails = LLMRails(config)
    return _rails


def _nemo(text):
    result = _instance().generate(
        messages=[{"role": "user", "content": text}],
        options={"rails": ["input"], "log": {"activated_rails": True}},
    )
    activated = getattr(getattr(result, "log", None), "activated_rails", None) or []
    decisions = [str(decision).lower() for rail in activated
                 for decision in getattr(rail, "decisions", [])]
    blocked = any("refuse to respond" in decision or decision == "stop" for decision in decisions)
    return blocked, len(activated)


def inspect_input(text):
    """Trả {allowed, reason, provider, score, hits, degraded?} — giữ hợp đồng cũ (allowed/reason).

    1. Luật heuristic: chặn ngay các mẫu rõ ràng, không tốn model.
    2. NeMo self-check: với mọi câu còn lại (khi NEMO_GUARDRAILS != "0").
    3. NeMo lỗi (Ollama tắt…): câu đã bị luật đánh dấu "suspicious" thì chặn (fail-closed),
       câu sạch thì cho qua kèm degraded=True để trace thấy guardrail đang thiếu một lớp.
    """
    if not isinstance(text, str) or not text.strip():
        return {"allowed": False, "reason": "invalid_input", "signals": 1, "provider": "heuristic",
                "score": 0, "hits": []}
    heuristic = injection.scan(text)
    base = {"score": heuristic["score"], "hits": heuristic["hits"]}
    if heuristic["verdict"] == "block":
        return {"allowed": False, "reason": "prompt_injection", "signals": len(heuristic["hits"]),
                "provider": "heuristic", **base}
    if os.environ.get("NEMO_GUARDRAILS", "1") == "0":
        return {"allowed": heuristic["verdict"] == "allow",
                "reason": "ok" if heuristic["verdict"] == "allow" else "prompt_injection_suspected",
                "signals": len(heuristic["hits"]), "provider": "heuristic", **base}
    try:
        blocked, signals = _nemo(text)
    except Exception as exc:  # model/guardrail không sẵn sàng
        logging.getLogger(__name__).warning("NeMo Guardrails lỗi: %s", exc)
        suspicious = heuristic["verdict"] == "suspicious"
        return {"allowed": not suspicious,
                "reason": "prompt_injection_suspected" if suspicious else "ok",
                "signals": len(heuristic["hits"]), "provider": "heuristic", "degraded": True, **base}
    return {"allowed": not blocked, "reason": "prompt_injection" if blocked else "ok",
            "signals": signals, "provider": "nemo_guardrails", **base}


def guard_tool_arguments(arguments):
    for key, value in arguments.items():
        if isinstance(value, str) and key in {"subject", "description", "message", "note", "question", "text"}:
            result = inspect_input(value)
            if not result["allowed"]:
                raise ValueError(f"Nội dung trường {key} bị chặn vì nghi prompt injection.")
    return True
