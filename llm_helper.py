import os
import json
import time

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4.1-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "10"))
DEBUG_LLM = os.getenv("DEBUG_LLM", "0") == "1"

# circuit breaker memory
_BREAKER = {}
_BREAKER_SECONDS = 600


def _breaker_ok(phone: str):
    return time.time() >= _BREAKER.get(phone, 0)


def _breaker_trip(phone: str):
    _BREAKER[phone] = time.time() + _BREAKER_SECONDS


def llm_extract(phone: str, user_text: str, service_names: list[str]):
    """
    Safe extractor.

    Returns dict OR None
    Never crashes.
    """

    if not OPENAI_API_KEY:
        return None

    if not _breaker_ok(phone):
        return None

    try:

        from openai import OpenAI
        client = OpenAI(
            api_key=OPENAI_API_KEY,
            timeout=OPENAI_TIMEOUT
        )

        system_prompt = f"""
You are a JSON extractor.

Return ONLY valid JSON.

Allowed intents:

book
menu
view
cancel
reschedule

Services allowed:

{service_names}

Examples:

User: haircut tomorrow 2pm

Output:

{{"intent":"book","service":"Haircut","when_text":"tomorrow 2pm"}}

User: cancel my booking

Output:

{{"intent":"cancel"}}
"""

        resp = client.chat.completions.create(

            model=OPENAI_MODEL,

            messages=[

                {"role": "system", "content": system_prompt},

                {"role": "user", "content": user_text}

            ],

            temperature=0,

            max_tokens=120

        )

        text = resp.choices[0].message.content.strip()

        if DEBUG_LLM:
            print("LLM RAW:", text)

        data = json.loads(text)

        # validate

        intent = data.get("intent")

        if intent not in [
            "book",
            "menu",
            "view",
            "cancel",
            "reschedule"
        ]:
            return None

        service = data.get("service")

        if service and service not in service_names:
            return None

        return data

    except Exception as e:

        if DEBUG_LLM:
            print("LLM ERROR:", e)

        _breaker_trip(phone)

        return None