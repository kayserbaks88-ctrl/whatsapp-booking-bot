import os
import json
import re
import urllib.request

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "30"))
DEBUG_LLM = os.getenv("DEBUG_LLM", "0").strip() == "1"


def _post_json(url: str, payload: dict, timeout: int):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _safe_json_extract(text: str):
    # try to find a JSON object in any text
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def llm_extract(user_text: str, service_names: list[str]):
    """
    Returns dict like:
    {
      "intent": "book|menu|view|cancel|reschedule|unknown",
      "service": "Haircut" (optional),
      "when_text": "Friday 2pm" (optional),
      "booking_index": 1 (optional int)
    }
    """
    if not OPENAI_API_KEY:
        return None

    prompt = f"""
You are an assistant for a barbershop WhatsApp booking bot.

Extract intent + details from the user's message.

Allowed intents:
- "menu" (show services)
- "view" (my bookings)
- "cancel"
- "reschedule"
- "book"
- "unknown"

Services list (match EXACTLY if possible):
{service_names}

Rules:
- If user says "my bookings", "view", "see bookings" => intent=view
- If user says cancel booking # => intent=cancel and booking_index
- If user says reschedule booking # => intent=reschedule and booking_index
- If user is trying to book and mentions a service + time/day => intent=book, include "service" and "when_text"
- If user says "trim" assume Haircut.
- If unsure, intent=unknown.

Return ONLY valid JSON, no extra text.
JSON schema:
{{
  "intent": "menu|view|cancel|reschedule|book|unknown",
  "service": "string or empty",
  "when_text": "string or empty",
  "booking_index": 0
}}

User message:
{user_text}
""".strip()

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "max_output_tokens": 200,
    }

    try:
        # OpenAI Responses API
        data = _post_json("https://api.openai.com/v1/responses", payload, timeout=OPENAI_TIMEOUT)

        # responses output text can appear in different shapes; grab any text fields
        text_chunks = []

        if isinstance(data, dict):
            if "output" in data and isinstance(data["output"], list):
                for item in data["output"]:
                    content = item.get("content") if isinstance(item, dict) else None
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                                text_chunks.append(c.get("text", ""))
            if "output_text" in data and isinstance(data["output_text"], str):
                text_chunks.append(data["output_text"])

        combined = "\n".join([t for t in text_chunks if t]).strip()

        parsed = _safe_json_extract(combined) if combined else None
        if not parsed or "intent" not in parsed:
            return None

        # normalize
        intent = (parsed.get("intent") or "unknown").lower()
        service = (parsed.get("service") or "").strip()
        when_text = (parsed.get("when_text") or "").strip()
        booking_index = parsed.get("booking_index") or 0

        out = {"intent": intent, "service": service, "when_text": when_text, "booking_index": booking_index}

        if DEBUG_LLM:
            print("LLM OUT:", out)

        return out

    except Exception as e:
        if DEBUG_LLM:
            print("LLM ERROR:", repr(e))
        return None