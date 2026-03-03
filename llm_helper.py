import os
import json
from typing import Optional, Dict, Any, List

from openai import OpenAI

# Model choice:
# - Set OPENAI_MODEL=gpt-4.1   (best quality)
# - or OPENAI_MODEL=gpt-4.1-mini (cheaper/faster)
#
# OpenAI docs show GPT-4.1 series supported in Responses API. :contentReference[oaicite:0]{index=0}
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM = """You are an intent extractor for a WhatsApp barbershop booking bot.
Return ONLY valid JSON (no markdown).
Keys:
- intent: one of ["menu","view","book","cancel","reschedule","unknown"]
- service: lowercase service name if present else ""
- when_text: the time text if present else ""
Rules:
- If user asks to see menu -> intent "menu"
- If user asks to view bookings -> intent "view"
- If user is booking -> intent "book" and include service + when_text if present
- If user wants cancel/reschedule -> those intents
"""

def llm_extract(text: str, service_names: List[str], phone: str = "") -> Optional[Dict[str, Any]]:
    """
    Returns a dict like:
      {"intent":"book","service":"haircut","when_text":"tomorrow 2pm"}
    or None on failure.
    """

    if not os.getenv("OPENAI_API_KEY"):
        # Very common cause of your "Missing bearer authentication" / connection issues
        print("LLM ERROR: OPENAI_API_KEY missing in environment", flush=True)
        return None

    try:
        allowed_services = ", ".join([s.lower() for s in service_names])

        prompt = f"""
User message: {text}

Allowed services (lowercase): {allowed_services}

Return JSON only.
"""

        resp = client.responses.create(
            model=DEFAULT_MODEL,
            input=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )

        # Responses API returns a structured object; easiest is to pull the output text.
        out_text = ""
        for item in resp.output:
            if item.type == "message":
                for c in item.content:
                    if c.type == "output_text":
                        out_text += c.text

        out_text = (out_text or "").strip()
        if not out_text:
            print("LLM ERROR: empty response text", flush=True)
            return None

        data = json.loads(out_text)

        # normalize
        intent = (data.get("intent") or "unknown").lower().strip()
        service = (data.get("service") or "").lower().strip()
        when_text = (data.get("when_text") or "").strip()

        return {"intent": intent, "service": service, "when_text": when_text}

    except Exception as e:
        print("LLM error:", repr(e), flush=True)
        return None