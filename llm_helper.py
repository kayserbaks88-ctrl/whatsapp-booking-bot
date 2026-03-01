import os
import json

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4.1-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20"))
DEBUG_LLM = os.getenv("DEBUG_LLM", "0").strip() == "1"


def llm_extract(user_text: str, service_names: list[str]):
    """
    Returns dict like:
      {"intent":"book","service":"Haircut","when_text":"tomorrow 2pm"}
      {"intent":"menu"} / {"intent":"view"} / {"intent":"cancel","booking_index":1} / {"intent":"reschedule","booking_index":1}
    Or None if disabled / failed.
    """
    if not OPENAI_API_KEY:
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT)
    except Exception:
        return None

    # Keep it strict + tiny so it can’t take over
    system = (
        "You are a strict JSON extractor for a barbershop WhatsApp booking bot. "
        "Return ONLY valid JSON. No commentary.\n"
        "Intents allowed: menu, view, book, cancel, reschedule, unknown.\n"
        "If booking: choose service from the provided service list ONLY.\n"
        "If cancel/reschedule and user mentions a number, set booking_index.\n"
        "If book, put the remaining time phrase into when_text.\n"
    )

    user = {
        "text": user_text,
        "services": service_names,
    }

    try:
        r = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user)},
            ],
        )
        content = (r.choices[0].message.content or "").strip()
        data = json.loads(content)
        if DEBUG_LLM:
            print("LLM_RAW:", content)
        if not isinstance(data, dict):
            return None
        if data.get("intent") == "unknown":
            return None
        return data
    except Exception:
        return None