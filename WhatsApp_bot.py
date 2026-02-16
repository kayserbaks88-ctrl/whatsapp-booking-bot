import os
import re
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import dateparser
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

from openai import OpenAI

from calendar_helper import (
    is_time_available,
    next_available_slots,
    create_booking_event,
)

load_dotenv()

# ---------------- CONFIG ----------------
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20"))
DEBUG_LLM = os.getenv("DEBUG_LLM", "0").strip() == "1"

HOLD_EXPIRE_MINUTES = 10
SLOT_STEP_MINUTES = 15

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

# name, price, duration mins
SERVICES = [
    ("Haircut", 18, 45),
    ("Skin Fade", 22, 60),
    ("Shape Up", 12, 20),
    ("Beard Trim", 10, 20),
    ("Hot Towel Shave", 15, 30),
    ("Kids Cut", 15, 30),
    ("Eyebrow Trim", 6, 10),
    ("Nose Wax", 8, 10),
    ("Ear Wax", 8, 10),
    ("Blow Dry", 10, 15),
]

SERVICE_ALIASES = {
    "haircut": "Haircut",
    "cut": "Haircut",
    "skin fade": "Skin Fade",
    "fade": "Skin Fade",
    "shape up": "Shape Up",
    "line up": "Shape Up",
    "beard": "Beard Trim",
    "beard trim": "Beard Trim",
    "hot towel": "Hot Towel Shave",
    "kids": "Kids Cut",
    "kids cut": "Kids Cut",
    "eyebrow": "Eyebrow Trim",
    "nose wax": "Nose Wax",
    "ear wax": "Ear Wax",
    "blow dry": "Blow Dry",
}

# ---------------- APP ----------------
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# super simple in-memory state
STATE = {}  # phone -> dict


# ---------------- HELPERS ----------------
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def get_service_by_choice(choice: str):
    c = norm(choice)
    # number choice
    if c.isdigit():
        idx = int(c) - 1
        if 0 <= idx < len(SERVICES):
            return SERVICES[idx][0]
    # name / alias
    if c in SERVICE_ALIASES:
        return SERVICE_ALIASES[c]
    # fuzzy contains
    for k, v in SERVICE_ALIASES.items():
        if k in c:
            return v
    return None


def service_meta(service_name: str):
    for name, price, mins in SERVICES:
        if name == service_name:
            return {"name": name, "price": price, "mins": mins}
    return None


def within_open_hours(dt: datetime, mins: int) -> bool:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    dow = dt.strftime("%a").lower()[:3]
    if dow not in OPEN_DAYS:
        return False

    start_t = dt.time()
    end_dt = dt + timedelta(minutes=mins)
    # must start >= open and end <= close
    if start_t < OPEN_TIME:
        return False
    if end_dt.time() > CLOSE_TIME:
        return False
    return True


def parse_datetime_text(text: str):
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }
    dt = dateparser.parse(text, settings=settings)
    return dt


def menu_text():
    lines = []
    lines.append(f"💈 {SHOP_NAME}")
    lines.append("Reply with *number or name*:\n")

    # grouping like your screenshot
    lines.append("*Men’s Cuts*")
    lines.append("1) Haircut — £18")
    lines.append("2) Skin Fade — £22")
    lines.append("3) Shape Up — £12\n")

    lines.append("*Beard / Shaves*")
    lines.append("4) Beard Trim — £10")
    lines.append("5) Hot Towel Shave — £15\n")

    lines.append("*Kids*")
    lines.append("6) Kids Cut — £15\n")

    lines.append("*Grooming*")
    lines.append("7) Eyebrow Trim — £6")
    lines.append("8) Nose Wax — £8")
    lines.append("9) Ear Wax — £8")
    lines.append("10) Blow Dry — £10\n")

    lines.append("Hours: Mon–Sat 9am–6pm | Sun Closed")
    lines.append('\nTip: you can also type: *Book haircut tomorrow at 2pm*')
    return "\n".join(lines)


def looks_like_booking_text(t: str) -> bool:
    t = norm(t)
    if re.search(r"\b(today|tomorrow|mon|tue|wed|thu|fri|sat|sun)\b", t):
        return True
    if re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", t):
        return True
    if re.search(r"\b\d{1,2}[\/\-]\d{1,2}\b", t):
        return True
    keywords = ["book", "appointment", "haircut", "fade", "beard", "kids", "shape up"]
    return any(k in t for k in keywords)


def call_openai_booking_extract(user_text: str):
    """
    Returns dict like:
    {
      "service": "Haircut" | null,
      "datetime_text": "tomorrow 2pm" | null
    }
    """
    if not client:
        return None

    schema = {
        "type": "object",
        "properties": {
            "service": {"type": ["string", "null"], "description": "Service name if present"},
            "datetime_text": {"type": ["string", "null"], "description": "Date/time phrase if present"},
        },
        "required": ["service", "datetime_text"],
        "additionalProperties": False,
    }

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Extract booking info from the user's message.\n"
                        f"Valid services: {', '.join([s[0] for s in SERVICES])}.\n"
                        "Return null if missing.\n"
                        "Do NOT invent details.\n"
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            # IMPORTANT: Structured output for Responses API uses text.format,
            # and MUST include a name.
            text={
                "format": {
                    "type": "json_schema",
                    "name": "booking_extract",   # <-- this fixes your 400 error
                    "strict": True,
                    "schema": schema,
                }
            },
            timeout=OPENAI_TIMEOUT,
        )

        # Responses API: easiest is to read output_text then json-load it.
        out = (resp.output_text or "").strip()
        if DEBUG_LLM:
            print("[DEBUG] LLM raw:", out)

        import json
        data = json.loads(out) if out else None
        if DEBUG_LLM:
            print("[DEBUG] LLM json:", data)
        return data

    except Exception as e:
        if DEBUG_LLM:
            print("[DEBUG] OPENAI ERROR:", repr(e))
        return None


def ask_for_time(service_name: str):
    return (
        f"✍️ *{service_name}*\n\n"
        "What day & time?\n"
        "Examples:\n"
        "• Tomorrow 2pm\n"
        "• Mon 3:15pm\n"
        "• 10/02 15:30\n\n"
        "Reply *BACK* to change service."
    )


def offer_next_slots(service_name: str, duration_mins: int):
    slots = next_available_slots(
        CALENDAR_ID,
        TZ,
        duration_mins,
        count=3,
        step_mins=SLOT_STEP_MINUTES,
        open_days=OPEN_DAYS,
        open_time=OPEN_TIME,
        close_time=CLOSE_TIME,
    )
    if not slots:
        return "Sorry — no availability found. Try another day/time."

    lines = [f"❌ That time is taken. Next available:"]
    for dt in slots:
        lines.append(f"• {dt.strftime('%a %d %b %H:%M')}")
    lines.append("\nReply with one option (e.g. *Tomorrow 3pm*)")
    return "\n".join(lines)


# ---------------- ROUTES ----------------
@app.get("/")
def health():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp():
    from_number = request.values.get("From", "")
    body = request.values.get("Body", "") or ""
    text = body.strip()

    resp = MessagingResponse()
    msg = resp.message()

    phone = from_number.replace("whatsapp:", "").strip()
    st = STATE.get(phone, {"step": "menu"})
    STATE[phone] = st

    tnorm = norm(text)

    # universal commands
    if tnorm in {"menu", "start", "hi", "hello"}:
        st["step"] = "menu"
        msg.body(menu_text())
        return str(resp)

    if tnorm == "back":
        st["step"] = "menu"
        st.pop("service", None)
        st.pop("hold_until", None)
        msg.body(menu_text())
        return str(resp)

    # 1) LLM FIRST: if message looks like a full booking request and we are not mid-flow
    if st.get("step") in {"menu", "idle"} and looks_like_booking_text(text):
        data = call_openai_booking_extract(text)
        if data:
            svc = data.get("service")
            dt_text = data.get("datetime_text")

            svc = get_service_by_choice(svc) if svc else None
            if not svc:
                # try to detect service from raw message
                svc = get_service_by_choice(text)

            dt = parse_datetime_text(dt_text) if dt_text else None
            if not dt:
                # fallback: try parse whole message
                dt = parse_datetime_text(text)

            if svc and dt:
                meta = service_meta(svc)
                mins = meta["mins"]

                # validate shop hours
                if not within_open_hours(dt, mins):
                    msg.body("That time is outside opening hours (Mon–Sat 9am–6pm). Try another time.")
                    return str(resp)

                # check availability
                if not is_time_available(CALENDAR_ID, dt, mins, TZ):
                    msg.body(offer_next_slots(svc, mins))
                    st["step"] = "awaiting_slot_choice"
                    st["service"] = svc
                    return str(resp)

                # book it
                end_dt = dt + timedelta(minutes=mins)
                create_booking_event(
                    calendar_id=CALENDAR_ID,
                    start_dt=dt,
                    end_dt=end_dt,
                    summary=f"{svc} - WhatsApp Booking",
                    description=f"Booked via {BUSINESS_NAME}",
                    phone=phone,  # safe now
                    service_name=svc,
                )
                st["step"] = "idle"
                msg.body(f"✅ Booked: *{svc}*\n📅 {dt.strftime('%a %d %b %H:%M')}")
                return str(resp)

            # partial extraction -> drop into normal flow
            if svc and not dt:
                st["step"] = "awaiting_time"
                st["service"] = svc
                msg.body(ask_for_time(svc))
                return str(resp)

    # 2) NORMAL FLOW
    if st.get("step") == "menu":
        svc = get_service_by_choice(text)
        if not svc:
            msg.body(menu_text())
            return str(resp)

        st["service"] = svc
        st["step"] = "awaiting_time"
        msg.body(ask_for_time(svc))
        return str(resp)

    if st.get("step") == "awaiting_time":
        svc = st.get("service")
        if not svc:
            st["step"] = "menu"
            msg.body(menu_text())
            return str(resp)

        dt = parse_datetime_text(text)
        if not dt:
            msg.body("I didn’t understand the time.\nTry: Tomorrow 2pm / Mon 3:15pm / 10/02 15:30\n\nReply BACK to change service.")
            return str(resp)

        meta = service_meta(svc)
        mins = meta["mins"]

        if not within_open_hours(dt, mins):
            msg.body("That time is outside opening hours (Mon–Sat 9am–6pm). Try another time.")
            return str(resp)

        if not is_time_available(CALENDAR_ID, dt, mins, TZ):
            msg.body(offer_next_slots(svc, mins))
            st["step"] = "awaiting_slot_choice"
            return str(resp)

        end_dt = dt + timedelta(minutes=mins)
        create_booking_event(
            calendar_id=CALENDAR_ID,
            start_dt=dt,
            end_dt=end_dt,
            summary=f"{svc} - WhatsApp Booking",
            description=f"Booked via {BUSINESS_NAME}",
            phone=phone,
            service_name=svc,
        )
        st["step"] = "idle"
        msg.body(f"✅ Booked: *{svc}*\n📅 {dt.strftime('%a %d %b %H:%M')}")
        return str(resp)

    if st.get("step") == "awaiting_slot_choice":
        svc = st.get("service")
        if not svc:
            st["step"] = "menu"
            msg.body(menu_text())
            return str(resp)

        dt = parse_datetime_text(text)
        if not dt:
            msg.body("Reply with one of the offered options (e.g. Tomorrow 3pm).")
            return str(resp)

        meta = service_meta(svc)
        mins = meta["mins"]

        if not within_open_hours(dt, mins):
            msg.body("That time is outside opening hours (Mon–Sat 9am–6pm). Try another time.")
            return str(resp)

        if not is_time_available(CALENDAR_ID, dt, mins, TZ):
            msg.body(offer_next_slots(svc, mins))
            return str(resp)

        end_dt = dt + timedelta(minutes=mins)
        create_booking_event(
            calendar_id=CALENDAR_ID,
            start_dt=dt,
            end_dt=end_dt,
            summary=f"{svc} - WhatsApp Booking",
            description=f"Booked via {BUSINESS_NAME}",
            phone=phone,
            service_name=svc,
        )
        st["step"] = "idle"
        msg.body(f"✅ Booked: *{svc}*\n📅 {dt.strftime('%a %d %b %H:%M')}")
        return str(resp)

    # fallback
    st["step"] = "menu"
    msg.body(menu_text())
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
