import os
import re
import json
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import dateparser
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

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

SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

DEBUG_LLM = os.getenv("DEBUG_LLM", "").strip() in {"1", "true", "True", "yes", "YES"}

# LLM
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip() or "gpt-5"
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20"))

# ---------------- SERVICES ----------------
# (name, price, duration_minutes)
SERVICES = [
    ("Haircut", 18, 45),
    ("Skin Fade", 22, 60),
    ("Shape Up", 12, 30),
    ("Beard Trim", 10, 30),
    ("Hot Towel Shave", 15, 45),
    ("Kids Cut", 15, 30),
    ("Eyebrow Trim", 6, 15),
    ("Nose Wax", 8, 15),
    ("Ear Wax", 8, 15),
    ("Blow Dry", 10, 15),
]

SERVICE_ALIASES = {
    "haircut": "Haircut",
    "cut": "Haircut",
    "mens cut": "Haircut",
    "men cut": "Haircut",
    "skin fade": "Skin Fade",
    "fade": "Skin Fade",
    "shape up": "Shape Up",
    "line up": "Shape Up",
    "beard": "Beard Trim",
    "beard trim": "Beard Trim",
    "hot towel": "Hot Towel Shave",
    "hot towel shave": "Hot Towel Shave",
    "kids": "Kids Cut",
    "kids cut": "Kids Cut",
    "eyebrow": "Eyebrow Trim",
    "eyebrow trim": "Eyebrow Trim",
    "nose wax": "Nose Wax",
    "ear wax": "Ear Wax",
    "blow dry": "Blow Dry",
}

SERVICE_BY_NUMBER = {str(i + 1): SERVICES[i][0] for i in range(len(SERVICES))}
SERVICE_BY_NAME = {name.lower(): name for (name, _, _) in SERVICES}

# ---------------- STATE (simple in-memory) ----------------
# user_state[phone] = {"step": "...", "service": "Haircut", "dt": datetime|None}
user_state = {}

# ---------------- HELPERS ----------------

def menu_text() -> str:
    lines = [f"💈 *{SHOP_NAME}*", "Reply with *number or name*:\n"]
    # Grouping (same style you like)
    groups = [
        ("Men’s Cuts", ["Haircut", "Skin Fade", "Shape Up"]),
        ("Beard / Shaves", ["Beard Trim", "Hot Towel Shave"]),
        ("Kids", ["Kids Cut"]),
        ("Grooming", ["Eyebrow Trim", "Nose Wax", "Ear Wax", "Blow Dry"]),
    ]

    name_to_meta = {n: (p, d) for (n, p, d) in SERVICES}
    num_by_name = {v: k for k, v in SERVICE_BY_NUMBER.items()}

    idx = 1
    for title, names in groups:
        lines.append(f"*{title}*")
        for n in names:
            price, _dur = name_to_meta[n]
            # Keep original numbering order from SERVICES list
            num = num_by_name.get(n, str(idx))
            lines.append(f"{num}) {n} — £{price}")
            idx += 1
        lines.append("")

    lines.append(f"Hours: Mon–Sat 9am–6pm | Sun Closed\n")
    lines.append('Tip: you can also type: *"Book haircut tomorrow at 2pm"*')
    return "\n".join(lines).strip()

def normalize_text(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()

def looks_like_booking_text(t: str) -> bool:
    t = (t or "").lower()
    if any(w in t for w in ["book", "booking", "appointment", "reserve"]):
        return True
    if any(s in t for s in ["haircut", "fade", "shape", "beard", "kids", "wax", "blow"]):
        return True
    # dates / times
    if re.search(r"\b(today|tomorrow|mon|tue|wed|thu|fri|sat|sun)\b", t):
        return True
    if re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", t):
        return True
    if re.search(r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b", t):
        return True
    return False

def pick_service_from_text(t: str):
    t0 = (t or "").lower().strip()

    # number
    if t0 in SERVICE_BY_NUMBER:
        return SERVICE_BY_NUMBER[t0]

    # exact name
    if t0 in SERVICE_BY_NAME:
        return SERVICE_BY_NAME[t0]

    # alias match
    for k, v in SERVICE_ALIASES.items():
        if k in t0:
            return v

    return None

def parse_datetime(text: str):
    """Parse a date/time like 'tomorrow 2pm' or 'Wednesday 4pm' into aware datetime in TZ."""
    text = normalize_text(text)
    if not text:
        return None

    dt = dateparser.parse(
        text,
        settings={
            "TIMEZONE": TIMEZONE,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "DATE_ORDER": "DMY",
        },
    )
    if not dt:
        return None

    # Ensure tz
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    else:
        dt = dt.astimezone(TZ)

    return dt

def within_open_hours(dt: datetime, duration_min: int) -> bool:
    day = dt.strftime("%a").lower()[:3]  # mon/tue/...
    if day not in OPEN_DAYS:
        return False

    start_t = dt.time()
    end_dt = dt + timedelta(minutes=duration_min)
    end_t = end_dt.time()

    # same-day boundary check
    if start_t < OPEN_TIME:
        return False
    if end_t > CLOSE_TIME:
        return False
    if end_dt.date() != dt.date():
        return False

    return True

def service_duration(service_name: str) -> int:
    for n, _p, d in SERVICES:
        if n == service_name:
            return d
    return 45

# ---------------- LLM (Structured output) ----------------

def call_openai_json(user_text: str):
    """
    Returns dict like:
      {"service":"Haircut"|"", "datetime_text":"tomorrow 2pm"|""}
    Uses Chat Completions structured output (stable), with fallback to None.
    """
    if not OPENAI_API_KEY:
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT)

        schema = {
            "name": "booking_extract",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "datetime_text": {"type": "string"},
                },
                "required": ["service", "datetime_text"],
                "additionalProperties": False,
            },
        }

        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You extract booking info for a barbershop called {SHOP_NAME}. "
                        "Return ONLY JSON that matches the schema. "
                        "If service missing, service should be empty string. "
                        "If datetime missing, datetime_text should be empty string."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            response_format={"type": "json_schema", "json_schema": schema},
        )

        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)

        if DEBUG_LLM:
            print("[DEBUG] LLM raw:", content)
            print("[DEBUG] LLM parsed:", data)

        # normalize
        data["service"] = (data.get("service") or "").strip()
        data["datetime_text"] = (data.get("datetime_text") or "").strip()
        return data

    except Exception as e:
        # IMPORTANT: If you ever switch back to Responses API, ensure text.format includes *name*.
        # Your logs showed missing text.format.name before. :contentReference[oaicite:1]{index=1}
        if DEBUG_LLM:
            print("[DEBUG] OpenAI error:", repr(e))
        return None

# ---------------- FLASK APP ----------------

app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

@app.post("/whatsapp")
def whatsapp():
    from_number = request.values.get("From", "")
    body = normalize_text(request.values.get("Body", ""))

    resp = MessagingResponse()
    msg = resp.message()

    # Identify user
    user_phone = from_number.replace("whatsapp:", "").strip() if from_number else "unknown"

    # state init
    st = user_state.get(user_phone) or {"step": "idle", "service": None, "dt": None}
    user_state[user_phone] = st

    # global commands
    low = body.lower()
    if low in {"menu", "start", "hi", "hello"}:
        st["step"] = "choose_service"
        st["service"] = None
        st["dt"] = None
        msg.body(menu_text())
        return str(resp)

    if low == "back":
        st["step"] = "choose_service"
        st["service"] = None
        st["dt"] = None
        msg.body(menu_text())
        return str(resp)

    # ---------- LLM-first booking attempt (works even in the middle of flow) ----------
    if looks_like_booking_text(body):
        llm = call_openai_json(body)
        if llm:
            service_guess = pick_service_from_text(llm.get("service", "")) or pick_service_from_text(body)
            dt_guess = parse_datetime(llm.get("datetime_text", "")) or parse_datetime(body)

            if service_guess and dt_guess:
                dur = service_duration(service_guess)

                if not within_open_hours(dt_guess, dur):
                    msg.body("⏰ We’re open Mon–Sat 9am–6pm. Try a time within those hours.")
                    return str(resp)

                end_dt = dt_guess + timedelta(minutes=dur)
                ok = is_time_available(CALENDAR_ID, dt_guess, end_dt)

                if not ok:
                    slots = next_available_slots(CALENDAR_ID, dt_guess, dur, step_minutes=SLOT_STEP_MINUTES, limit=3)
                    if not slots:
                        msg.body("❌ That time is taken, and I couldn’t find slots soon. Try another time.")
                        return str(resp)

                    lines = ["❌ That time is taken. Next available:"]
                    for s in slots:
                        lines.append(f"• {s.strftime('%a %d %b %H:%M')}")
                    lines.append("\nReply with one option (e.g. Tomorrow 3pm)")
                    st["step"] = "choose_time"
                    st["service"] = service_guess
                    st["dt"] = None
                    msg.body("\n".join(lines))
                    return str(resp)

                # book it
                create_booking_event(
                    CALENDAR_ID,
                    start_dt=dt_guess,
                    end_dt=end_dt,
                    service_name=service_guess,
                    phone=user_phone,
                )
                msg.body(f"✅ Booked: {service_guess}\n📅 {dt_guess.strftime('%a %d %b %H:%M')}")
                st["step"] = "idle"
                st["service"] = None
                st["dt"] = None
                return str(resp)

    # ---------- Normal flow ----------
    if st["step"] in {"idle", "choose_service"}:
        # Try select service
        service = pick_service_from_text(body)
        if not service:
            st["step"] = "choose_service"
            msg.body(menu_text())
            return str(resp)

        st["service"] = service
        st["step"] = "choose_time"
        msg.body(
            f"✍️ *{service}*\n\nWhat day & time?\nExamples:\n"
            "• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply BACK to change service."
        )
        return str(resp)

    if st["step"] == "choose_time":
        service = st.get("service")
        dur = service_duration(service)

        dt = parse_datetime(body)

        if not dt:
            msg.body("I didn’t understand the time.\nTry:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply BACK to change service.")
            return str(resp)

        if not within_open_hours(dt, dur):
            msg.body("⏰ We’re open Mon–Sat 9am–6pm. Try a time within those hours.")
            return str(resp)

        end_dt = dt + timedelta(minutes=dur)
        ok = is_time_available(CALENDAR_ID, dt, end_dt)

        if not ok:
            slots = next_available_slots(CALENDAR_ID, dt, dur, step_minutes=SLOT_STEP_MINUTES, limit=3)
            if not slots:
                msg.body("❌ That time is taken. Try another time.")
                return str(resp)

            lines = ["❌ That time is taken. Next available:"]
            for s in slots:
                lines.append(f"• {s.strftime('%a %d %b %H:%M')}")
            lines.append("\nReply with one option (e.g. Tomorrow 3pm)")
            msg.body("\n".join(lines))
            return str(resp)

        create_booking_event(
            CALENDAR_ID,
            start_dt=dt,
            end_dt=end_dt,
            service_name=service,
            phone=user_phone,
        )
        msg.body(f"✅ Booked: {service}\n📅 {dt.strftime('%a %d %b %H:%M')}")
        st["step"] = "idle"
        st["service"] = None
        st["dt"] = None
        return str(resp)

    # fallback
    msg.body(menu_text())
    return str(resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
