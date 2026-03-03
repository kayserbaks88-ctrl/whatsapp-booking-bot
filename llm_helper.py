import os
import json
import time
import requests
from typing import Optional, Dict, Any, List

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none").strip().lower()
OLLAMA_BASE_URL = (os.getenv("OLLAMA_BASE_URL") or "").strip().rstrip("/")
OLLAMA_MODEL = (os.getenv("OLLAMA_MODEL") or "llama3:8b").strip()
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "12"))
DEBUG_LLM = os.getenv("DEBUG_LLM", "0").strip() == "1"

# Simple circuit breaker per phone
_BREAKER: Dict[str, float] = {}
_BREAKER_SECONDS = int(os.getenv("LLM_BREAKER_SECONDS", "600"))

def _breaker_ok(phone: str) -> bool:
    return time.time() >= _BREAKER.get(phone, 0.0)

def _breaker_trip(phone: str) -> None:
    _BREAKER[phone] = time.time() + _BREAKER_SECONDS

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = text.strip()

    # direct JSON
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    # JSON inside text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start:end+1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None

def llm_extract(user_text: str, service_names: List[str], phone: str = "") -> Optional[Dict[str, Any]]:
    """
    Returns dict like:
      {"intent":"book","service":"Haircut","when_text":"tomorrow 2pm"}
      {"intent":"menu"} / {"intent":"view"} / {"intent":"cancel","booking_index":1}
      {"intent":"reschedule","booking_index":1,"when_text":"fri 3pm"}
    Or None if disabled/failed.
    """
    if LLM_PROVIDER != "ollama":
        return None
    if not OLLAMA_BASE_URL:
        return None
    if phone and not _breaker_ok(phone):
        return None

    services_line = ", ".join(service_names[:60])

    system = (
        "You are a strict JSON extractor for a barber booking WhatsApp bot.\n"
        "Return ONLY valid JSON. No markdown. No extra text.\n"
        "Allowed intents: book, menu, view, cancel, reschedule, help.\n"
        "If booking: include keys intent, service, when_text.\n"
        "If cancel/reschedule: include booking_index (1-based) if user mentions a number.\n"
        "If reschedule and user gives time: include when_text.\n"
        "If unclear: intent=help.\n"
    )

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "system", "content": f"Valid services: {services_line}"},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }

    try:
        r = requests.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=LLM_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        content = ((data.get("message") or {}).get("content") or "").strip()

        if DEBUG_LLM:
            print("LLM raw:", content)

        obj = _extract_json(content)
        if not obj:
            return None

        # normalize
        if isinstance(obj.get("intent"), str):
            obj["intent"] = obj["intent"].strip().lower()
        if isinstance(obj.get("service"), str):
            obj["service"] = obj["service"].strip()
        if isinstance(obj.get("when_text"), str):
            obj["when_text"] = obj["when_text"].strip()
        if isinstance(obj.get("booking_index"), str) and obj["booking_index"].isdigit():
            obj["booking_index"] = int(obj["booking_index"])

        return obj

    except Exception as e:
        if DEBUG_LLM:
            print("LLM error:", repr(e))
        if phone:
            _breaker_trip(phone)
        return None