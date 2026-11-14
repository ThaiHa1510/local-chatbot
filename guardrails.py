"""NeMo Guardrails adapter backed by the local Ollama model."""
import os
from pathlib import Path
import threading

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


def inspect_input(text):
    """Run NeMo's input rail and return the existing application contract."""
    if not isinstance(text, str) or not text.strip():
        return {"allowed": False, "reason": "invalid_input", "signals": 1,
                "provider": "nemo_guardrails"}
    result = _instance().generate(
        messages=[{"role": "user", "content": text}],
        options={"rails": ["input"], "log": {"activated_rails": True}},
    )
    activated = getattr(getattr(result, "log", None), "activated_rails", None) or []
    decisions = [str(decision).lower() for rail in activated
                 for decision in getattr(rail, "decisions", [])]
    blocked = any("refuse to respond" in decision or decision == "stop" for decision in decisions)
    return {"allowed": not blocked, "reason": "prompt_injection" if blocked else "ok",
            "signals": len(activated), "provider": "nemo_guardrails"}


def guard_tool_arguments(arguments):
    for key, value in arguments.items():
        if isinstance(value, str) and key in {"subject", "description", "message", "note"}:
            result = inspect_input(value)
            if not result["allowed"]:
                raise ValueError("Nội dung không vượt qua NeMo Guardrails.")
    return True
