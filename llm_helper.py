import os
import json
import re

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


def llm_extract(user_text: str, services):
    """
    Returns dict: { intent: "book|cancel|reschedule|menu|my_bookings|unknown",
                    service: "<service name or empty>",
                    when: "<time text or empty>" }

    SAFE: It must return JSON only; if anything fails -> return {}.
    """
    if not OpenAI:
        return {}

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-4.1").strip()
    timeout = float(os.getenv("OPENAI_TIMEOUT", "20").strip() or "20")

    if not api_key:
        return {}

    client = OpenAI(api_key=api_key, timeout=timeout)

    service_names = [s[0] for s in services]  # list of names

    system = (
        "You are a strict information extractor for a barbershop WhatsApp booking bot. "
        "Return ONLY valid JSON. No extra text.\n\n"
        "Schema:\n"
        "{"
        "\"intent\":\"book|cancel|reschedule|menu|my_bookings|unknown\","
        "\"service\":\"\","
        "\"when\":\"\""
        "}\n\n"
        "Rules:\n"
        "- If user wants to book, intent=book.\n"
        "- Extract service if mentioned (match closest from list).\n"
        "- Extract time phrase into 'when' (e.g. 'tomorrow 2pm', 'Friday at 1', '10/02 15:30').\n"
        "- If no service/time, leave empty strings.\n"
        "- Never invent information.\n"
    )

    user = (
        f"Services list:\n{service_names}\n\n"
        f"User message:\n{user_text}\n\n"
        "Return JSON only."
    )

    try:
        r = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = r.output_text.strip()

        # guard: pull json block
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        data = json.loads(m.group(0))
        # minimal validation
        if not isinstance(data, dict):
            return {}
        return {
            "intent": (data.get("intent") or "unknown"),
            "service": (data.get("service") or ""),
            "when": (data.get("when") or ""),
        }
    except Exception:
        return {}