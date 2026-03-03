import os
import json
import time
from typing import Optional, Dict, List

from openai import OpenAI

# ---- Config ----
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4.1-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "12"))
DEBUG_LLM = os.getenv("DEBUG_LLM", "0") == "1"

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# circuit breaker memory (per phone)
_BREAKER: Dict[str, float] = {}
_BREAKER_SECONDS = 600  # 10 minutes


def _breaker_ok(phone: str) -> bool:
    return time.time() >= _BREAKER.get(phone, 0)


def _breaker_trip(phone: str) -> None:
    _BREAKER[phone] = time.time() + _BREAKER_SECONDS


def llm_extract(user_text: str, service_names: List[str], phone: str = "") -> Optional[dict]:
    """
    Returns dict like:
      {"intent":"book|menu|view|cancel|reschedule|unknown",
       "service":"Haircut|Skin Fade|...",
       "when_text":"tomorrow 2pm"}
    or None if not confident / error.
    """
    if not client:
        if DEBUG_LLM:
            print("LLM DEBUG: OPENAI_API_KEY missing -> llm_extract disabled", flush=True)
        return None

    if phone and not _breaker_ok(phone):
        if DEBUG_LLM:
            print("LLM DEBUG: breaker active for", phone, flush=True)
        return None

    text = (user_text or "").strip()
    if not text:
        return None

    # Keep prompt tight/stable
    services_line = ", ".join(service_names)

    system = (
        "You are an assistant that extracts booking intent from a message for a barbershop WhatsApp bot.\n"
        "Return ONLY valid JSON with keys: intent, service, when_text.\n"
        "intent must be one of: book, menu, view, cancel, reschedule, unknown.\n"
        "service must be exactly one of the provided services or empty string.\n"
        "when_text should be the raw time phrase from the user (e.g. 'tomorrow 2pm') or empty string.\n"
        "If user is not clearly booking or asking menu/bookings, return intent='unknown'."
    )

    user = (
        f"Services: {services_line}\n"
        f"Message: {text}\n\n"
        "Respond with JSON only."
    )

    try:
        if DEBUG_LLM:
            print("LLM DEBUG: calling OpenAI model:", OPENAI_MODEL, flush=True)

        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # small + deterministic-ish
            temperature=0.0,
            max_output_tokens=120,
            timeout=OPENAI_TIMEOUT,
        )

        out_text = (resp.output_text or "").strip()
        if DEBUG_LLM:
            print("LLM DEBUG: raw output:", out_text, flush=True)

        data = json.loads(out_text)

        intent = (data.get("intent") or "").strip().lower()
        service = (data.get("service") or "").strip()
        when_text = (data.get("when_text") or "").strip()

        if intent not in {"book", "menu", "view", "cancel", "reschedule", "unknown"}:
            return None

        if service and service not in service_names:
            # sometimes model returns close text; reject to avoid bad bookings
            service = ""

        cleaned = {"intent": intent, "service": service, "when_text": when_text}
        return cleaned

    except Exception as e:
        if DEBUG_LLM:
            print("LLM error:", repr(e), flush=True)
        if phone:
            _breaker_trip(phone)
        return None