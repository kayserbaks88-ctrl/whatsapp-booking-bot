import os
import json
import time
import requests

# Provider switch (so you can turn LLM off without code changes)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()

# Ollama settings
OLLAMA_BASE_URL = (os.getenv("OLLAMA_BASE_URL") or "").strip().rstrip("/")
OLLAMA_MODEL = (os.getenv("OLLAMA_MODEL") or "llama3:8b").strip()
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "10"))

# Optional debug
DEBUG_LLM = os.getenv("DEBUG_LLM", "0").strip() == "1"

# Circuit breaker (per phone) to avoid repeated failures
_BREAKER = {}
_BREAKER_SECONDS = int(os.getenv("LLM_BREAKER_SECONDS", "600"))

def _breaker_ok(phone: str) -> bool:
    return time.time() >= _BREAKER.get(phone, 0)

def _breaker_trip(phone: str):
    _BREAKER[phone] = time.time() + _BREAKER_SECONDS

def _extract_json(text: str):
    """
    Ollama sometimes returns JSON with extra text.
    This tries to extract the first valid JSON object.
    """
    if not text:
        return None

    text = text.strip()

    # Fast path: already JSON
    try:
        return json.loads(text)
    except Exception:
        pass

    # Try to find a JSON object inside the text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end+1]
        try:
            return json.loads(candidate)
        except Exception:
            return None

    return None

def llm_extract(user_text: str, service_names: list[str], phone: str = ""):
    """
    Returns dict like:
      {"intent":"book","service":"Haircut","when_text":"tomorrow 2pm"}
      {"intent":"menu"}
      {"intent":"view"}
      {"intent":"cancel","booking_index":1}
      {"intent":"reschedule","booking_index":1}

    Or None if disabled / failed.
    """
    if LLM_PROVIDER != "ollama":
        return None

    if not OLLAMA_BASE_URL:
        if DEBUG_LLM:
            print("LLM: missing OLLAMA_BASE_URL")
        return None

    if phone and not _breaker_ok(phone):
        return None

    # Keep the model tightly controlled: intent extraction only
    system = (
        "You are a strict JSON extractor for a barber booking assistant.\n"
        "Return ONLY valid JSON. No markdown. No extra text.\n"
        "Allowed intents: book, menu, view, cancel, reschedule, help.\n"
        "If intent is book, include keys: intent, service, when_text.\n"
        "If intent is cancel/reschedule and user refers to a number, include booking_index (1-based).\n"
        "If unsure, use intent=help.\n"
    )

    services_line = "Services: " + ", ".join(service_names[:50])

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "system", "content": services_line},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "options": {
            "temperature": 0.1,
        },
    }

    try:
        r = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=LLM_TIMEOUT,
        )
        r.raise_for_status()

        data = r.json()
        content = (data.get("message", {}) or {}).get("content", "")

        if DEBUG_LLM:
            print("LLM raw:", content)

        obj = _extract_json(content)
        if not isinstance(obj, dict):
            return None

        # Normalize a bit
        if "intent" in obj and isinstance(obj["intent"], str):
            obj["intent"] = obj["intent"].strip().lower()

        if "service" in obj and isinstance(obj["service"], str):
            obj["service"] = obj["service"].strip()

        if "when_text" in obj and isinstance(obj["when_text"], str):
            obj["when_text"] = obj["when_text"].strip()

        return obj

    except Exception as e:
        if DEBUG_LLM:
            print("LLM error:", repr(e))
        if phone:
            _breaker_trip(phone)
        return None