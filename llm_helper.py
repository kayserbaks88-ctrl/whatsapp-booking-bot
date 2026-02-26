import os
import re

# Optional OpenAI (won't crash if not installed)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None


def _clean(text: str) -> str:
    return (text or "").strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", _clean(text)).strip().lower()


def _guess_service(text: str, service_names: list[str]) -> str | None:
    t = _norm(text)
    if not t:
        return None

    # quick aliases you mentioned
    aliases = {
        "trim": "Haircut",
        "kids cut": "Children's Cut",
        "kid cut": "Children's Cut",
        "childrens cut": "Children's Cut",
        "children cut": "Children's Cut",
        "boys cut": "Boy's Cut",
        "boy cut": "Boy's Cut",
        "beard": "Beard Trim",
        "skin fade": "Skin Fade",
        "fade": "Skin Fade",
        "shape": "Shape Up",
        "hot towel": "Hot Towel Shave",
        "ear wax": "Ear Waxing",
        "nose wax": "Nose Waxing",
        "eyebrow": "Eyebrow Trim",
    }
    for k, v in aliases.items():
        if k in t:
            return v

    # exact/contains match to your menu names
    best = None
    for name in service_names:
        n = name.lower()
        if n in t:
            best = name
            break
    return best


def _extract_booking_index(text: str) -> int | None:
    m = re.search(r"\b(\d{1,2})\b", _norm(text))
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def llm_extract(user_text: str, service_names: list[str]) -> dict | None:
    """
    Returns dict like:
      {"intent": "book"|"menu"|"view"|"cancel"|"reschedule", "service": "...", "when_text": "...", "booking_index": 1}
    or None if no confident intent.
    """
    text = _clean(user_text)
    t = _norm(text)
    if not t:
        return None

    # RULES FIRST (fast + reliable)
    if t in {"menu", "help"}:
        return {"intent": "menu"}

    if t in {"my bookings", "mybookings", "bookings", "view"}:
        return {"intent": "view"}

    if t.startswith("cancel"):
        idx = _extract_booking_index(t) or 1
        return {"intent": "cancel", "booking_index": idx}

    if t.startswith("reschedule"):
        idx = _extract_booking_index(t) or 1
        return {"intent": "reschedule", "booking_index": idx}

    # booking style phrases
    book_words = ("book", "can i book", "can i get", "i want", "i need", "get me", "appointment")
    if any(w in t for w in book_words):
        svc = _guess_service(t, service_names)
        # Try to grab "when" part roughly: after service word or after "book"
        when_text = ""
        m = re.search(r"\b(book|appointment)\b(.+)$", t)
        if m:
            when_text = m.group(2).strip()
        return {"intent": "book", "service": svc or "", "when_text": when_text}

    # OPTIONAL: OpenAI fallback (only if installed + key set)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
    timeout = float(os.getenv("OPENAI_TIMEOUT", "20").strip() or "20")

    if not api_key or OpenAI is None:
        return None

    try:
        client = OpenAI(api_key=api_key, timeout=timeout)

        system = (
            "You extract intent from WhatsApp barber booking messages.\n"
            "Return ONLY strict JSON, no markdown.\n"
            "Schema:\n"
            "{"
            '"intent":"menu|view|cancel|reschedule|book|none",'
            '"service":"",'
            '"when_text":"",'
            '"booking_index":1'
            "}\n"
            "If unknown, intent=none."
        )

        user = (
            f"Services: {service_names}\n"
            f"Message: {text}"
        )

        r = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )

        raw = r.choices[0].message.content.strip()
        # super light json parse without importing json (keep simple)
        if raw.startswith("{") and raw.endswith("}"):
            # if it's valid JSON, parse properly
            import json
            obj = json.loads(raw)
            if obj.get("intent") == "none":
                return None
            return obj
        return None
    except Exception:
        return None