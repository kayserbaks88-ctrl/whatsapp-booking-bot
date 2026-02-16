import os
import re
import json
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import dateparser
import requests
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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20"))  # MUST be numeric in env
DEBUG_LLM = os.getenv("DEBUG_LLM", "0") == "1"

SLOT_STEP_MINUTES = 15
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)
OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}  # sun closed

SERVICES = [
    ("Haircut", 18, 45),
    ("Skin Fade", 22, 60),
    ("Shape Up", 12, 30),
    ("Beard Trim", 10, 30),
    ("Kids Cut", 15, 30),
]

SERVICE_ALIASES = {
    "haircut": "Haircut",
    "cut": "Haircut",
    "trim": "Haircut",
    "skin fade": "Skin Fade",
    "fade": "Skin Fade",
    "shape up": "Shape Up",
    "line up": "Shape Up",
    "beard": "Beard Trim",
    "beard trim": "Beard Trim",
    "kids": "Kids Cut",
    "kids cut": "Kids Cut",
    "child": "Kids Cut",
}

SERVICE_MAP = {name.lower(): (name, price, mins) for name, price, mins in SERVICES}


# ---------------- STATE (simple in-memory) ----------------
# key: phone -> dict
STATE = {}


def get_state(phone: str) -> dict:
    st = STATE.get(phone)
    if not st:
        st = {"step": "menu", "service": None, "mins": None, "offered": []}
        STATE[phone] = st
    return st


# ---------------- HELPERS ----------------

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def menu_text() -> str:
    lines = [f"💈 *{SHOP_NAME}*",
             "Welcome! Reply with a number or name:",
             "",
             "*Men’s Cuts*"]
    for i, (name, price, _) in enumerate(SERVICES, start=1):
        lines.append(f"{i}) {name} — £{price}")
    lines += [
        "",
        f"Hours: Mon–Sat 9am–6pm | Sun Closed",
        "",
        "Tip: you can type a full sentence like:",
        "• *Book a skin fade tomorrow at 2pm*",
        "• *Can I get a haircut Wednesday around 4?*",
    ]
    return "\n".join(lines)


def looks_like_booking_text(t: str) -> bool:
    t = t.lower()
    if re.search(r"\b(today|tomorrow|mon|tue|wed|thu|fri|sat|sun)\b", t):
        return True
    if re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", t):
        return True
    if re.search(r"\b\d{1,2}[/-]\d{1,2}\b", t):
        return True
    if any(k in t for k in ["book", "appointment", "haircut", "fade", "beard", "kids", "shape"]):
        return True
    return False


def parse_datetime_fallback(text: str) -> datetime | None:
    dt = dateparser.parse(
        text,
        settings={
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": TIMEZONE,
            "PREFER_DATES_FROM": "future",
        },
    )
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def in_open_hours(dt: datetime) -> bool:
    dow = dt.strftime("%a").lower()[:3]  # mon/tue...
    if dow not in OPEN_DAYS:
        return False
    t = dt.time()
    return (t >= OPEN_TIME) and (t <= (datetime.combine(dt.date(), CLOSE_TIME, TZ) - timedelta(minutes=1)).time())


def clamp_to_open_hours(dt: datetime, mins: int) -> tuple[datetime, datetime] | None:
    """
    Returns (start, end) if within opening hours and day open, else None.
    """
    if not in_open_hours(dt):
        return None
    end = dt + timedelta(minutes=mins)
    close_dt = datetime.combine(dt.date(), CLOSE_TIME, tzinfo=TZ)
    if end > close_dt:
        return None
    return dt, end


def format_slot(dt: datetime) -> str:
    return dt.strftime("%a %d %b %H:%M")


def offer_next_slots(svc: str, mins: int, requested_dt: datetime) -> str:
    day_start = datetime.combine(requested_dt.date(), OPEN_TIME, tzinfo=TZ)
    day_end = datetime.combine(requested_dt.date(), CLOSE_TIME, tzinfo=TZ)

    slots = next_available_slots(
        CALENDAR_ID,
        day_start=day_start,
        day_end=day_end,
        duration_minutes=mins,
        step_minutes=SLOT_STEP_MINUTES,
        limit=3,
    )

    if not slots:
        return "❌ No slots left that day. Try another day (e.g. *Tomorrow 2pm*)."

    lines = ["❌ That time is taken. Next available:"]
    for s in slots:
        lines.append(f"• {format_slot(s)}")
    lines.append("")
    lines.append("Reply with one option (e.g. *Tue 17 Feb 09:00*) or just *09:15* or *first option*.")
    return "\n".join(lines)


# ---------------- OPENAI (Proper LLM) ----------------

def openai_responses_json(schema_name: str, schema: dict, prompt: str) -> dict | None:
    """
    Calls OpenAI Responses API with text.format json_schema.
    Fixes: Missing required parameter 'text.format.name'
    """
    if not OPENAI_API_KEY:
        return None

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,     # <-- REQUIRED
                "schema": schema,
            }
        },
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=OPENAI_TIMEOUT)
        if r.status_code >= 400:
            if DEBUG_LLM:
                print("[DEBUG] OPENAI ERROR", r.status_code, r.text)
            return None
        data = r.json()

        # Responses API returns text in output[0].content[0].text (commonly)
        out = data.get("output", [])
        if not out:
            return None
        content = out[0].get("content", [])
        if not content:
            return None
        txt = content[0].get("text")
        if not txt:
            return None

        return json.loads(txt)
    except Exception as e:
        if DEBUG_LLM:
            print("[DEBUG] OPENAI EXCEPTION", repr(e))
        return None


def llm_extract_booking(user_text: str) -> dict | None:
    """
    Returns:
      { "service": "...", "datetime_text": "...", "intent": "book|menu|other" }
    """
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intent": {"type": "string", "enum": ["book", "menu", "other"]},
            "service": {"type": "string"},
            "datetime_text": {"type": "string"},
        },
        "required": ["intent", "service", "datetime_text"],
    }

    prompt = f"""
You are a booking assistant for a barbershop.

Extract the customer's intent, service, and datetime phrase from the message.

Rules:
- intent = "menu" if they ask for menu/services/prices.
- intent = "book" if they want an appointment or mention a time/day.
- service: return the closest service name from this list:
  {", ".join([s[0] for s in SERVICES])}
  If not mentioned, return "".
- datetime_text: the part that indicates date/time ("tomorrow 2pm", "Wednesday at 4", etc). If missing, "".

Customer message:
{user_text}
""".strip()

    return openai_responses_json("booking_extract", schema, prompt)


def llm_pick_offered_slot(user_text: str, offered: list[str]) -> dict | None:
    """
    offered is list of formatted slot strings e.g. 'Tue 17 Feb 09:00'
    Returns:
      { "action": "pick|new_time|unknown",
        "pick_index": 0,
        "datetime_text": "" }
    """
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["pick", "new_time", "unknown"]},
            "pick_index": {"type": "integer"},
            "datetime_text": {"type": "string"},
        },
        "required": ["action", "pick_index", "datetime_text"],
    }

    prompt = f"""
A customer must choose ONE of the offered appointment options.

Offered options (index starting at 1):
{chr(10).join([f"{i+1}) {offered[i]}" for i in range(len(offered))])}

Customer reply:
{user_text}

Decide:
- action="pick" and pick_index = chosen option index (1..N) if they refer to an option (e.g. "first", "option 2", "09:15", "Tue 17 Feb 09:00").
- action="new_time" if they propose a different time/day (set datetime_text to what they said).
- action="unknown" otherwise.
If action != "pick", set pick_index=0.
""".strip()

    return openai_responses_json("slot_pick", schema, prompt)


def match_time_to_offered(user_text: str, offered_dts: list[datetime]) -> int | None:
    """
    If user says "9am" / "09:15", match to offered list by time.
    Returns index or None.
    """
    t = user_text.lower()
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t)
    if not m:
        return None

    hh = int(m.group(1))
    mm = int(m.group(2) or "0")
    ampm = m.group(3)

    if ampm:
        if ampm == "pm" and hh < 12:
            hh += 12
        if ampm == "am" and hh == 12:
            hh = 0

    for i, dt in enumerate(offered_dts):
        if dt.hour == hh and dt.minute == mm:
            return i
    return None


# ---------------- FLASK ----------------

app = Flask(__name__)


@app.get("/")
def health():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp():
    resp = MessagingResponse()
    msg = resp.message()

    from_phone = request.form.get("From", "")
    user_text = normalize(request.form.get("Body", ""))
    text_l = user_text.lower()

    st = get_state(from_phone)

    # Global commands
    if text_l in {"menu", "hi", "hello", "start"}:
        st["step"] = "menu"
        st["service"] = None
        st["mins"] = None
        st["offered"] = []
        msg.body(menu_text())
        return str(resp)

    if text_l == "back":
        st["step"] = "menu"
        st["service"] = None
        st["mins"] = None
        st["offered"] = []
        msg.body(menu_text())
        return str(resp)

    # If we're waiting for offered slot selection
    if st.get("step") == "choose_alt" and st.get("offered"):
        offered_dts = st["offered"]  # list[datetime]
        offered_str = [format_slot(d) for d in offered_dts]

        # 1) try simple match by time
        idx = match_time_to_offered(user_text, offered_dts)
        if idx is not None:
            chosen = offered_dts[idx]
            svc = st["service"]
            mins = st["mins"]
            start_end = clamp_to_open_hours(chosen, mins)
            if not start_end:
                msg.body("That time doesn’t fit opening hours. Reply with one of the offered options.")
                return str(resp)

            start_dt, end_dt = start_end
            create_booking_event(
                calendar_id=CALENDAR_ID,
                start_dt=start_dt,
                end_dt=end_dt,
                summary=f"{svc} - WhatsApp Booking",
                description=f"Booked via {BUSINESS_NAME}",
                phone=from_phone,
                service_name=svc,
            )
            st["step"] = "menu"
            st["offered"] = []
            msg.body(f"✅ Booked: *{svc}*\n📅 {format_slot(start_dt)}")
            return str(resp)

        # 2) LLM pick
        pick = llm_pick_offered_slot(user_text, offered_str)
        if pick and pick.get("action") == "pick":
            pick_index = int(pick.get("pick_index") or 0) - 1
            if 0 <= pick_index < len(offered_dts):
                chosen = offered_dts[pick_index]
                svc = st["service"]
                mins = st["mins"]
                start_end = clamp_to_open_hours(chosen, mins)
                if not start_end:
                    msg.body("That time doesn’t fit opening hours. Reply with one of the offered options.")
                    return str(resp)

                start_dt, end_dt = start_end
                create_booking_event(
                    calendar_id=CALENDAR_ID,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    summary=f"{svc} - WhatsApp Booking",
                    description=f"Booked via {BUSINESS_NAME}",
                    phone=from_phone,
                    service_name=svc,
                )
                st["step"] = "menu"
                st["offered"] = []
                msg.body(f"✅ Booked: *{svc}*\n📅 {format_slot(start_dt)}")
                return str(resp)

        if pick and pick.get("action") == "new_time":
            # treat their text as new booking request while keeping service
            dt = parse_datetime_fallback(pick.get("datetime_text", "") or user_text)
        else:
            dt = parse_datetime_fallback(user_text)

        if not dt:
            msg.body("Reply with one of the offered options (e.g. *Tue 17 Feb 09:00*) or just *09:15*.")
            return str(resp)

        svc = st["service"]
        mins = st["mins"]
        start_end = clamp_to_open_hours(dt, mins)
        if not start_end:
            msg.body("That time isn’t within opening hours. Try another (e.g. *Tomorrow 2pm*).")
            return str(resp)

        start_dt, end_dt = start_end
        if is_time_available(CALENDAR_ID, start_dt, end_dt):
            create_booking_event(
                calendar_id=CALENDAR_ID,
                start_dt=start_dt,
                end_dt=end_dt,
                summary=f"{svc} - WhatsApp Booking",
                description=f"Booked via {BUSINESS_NAME}",
                phone=from_phone,
                service_name=svc,
            )
            st["step"] = "menu"
            st["offered"] = []
            msg.body(f"✅ Booked: *{svc}*\n📅 {format_slot(start_dt)}")
            return str(resp)

        # still taken, offer again
        slots = next_available_slots(
            CALENDAR_ID,
            day_start=datetime.combine(start_dt.date(), OPEN_TIME, tzinfo=TZ),
            day_end=datetime.combine(start_dt.date(), CLOSE_TIME, tzinfo=TZ),
            duration_minutes=mins,
            step_minutes=SLOT_STEP_MINUTES,
            limit=3,
        )
        st["offered"] = slots
        st["step"] = "choose_alt"
        msg.body(offer_next_slots(svc, mins, start_dt))
        return str(resp)

    # Normal flow: try “Proper LLM” booking first
    if looks_like_booking_text(user_text):
        extracted = llm_extract_booking(user_text)

        intent = (extracted or {}).get("intent", "book")
        if intent == "menu":
            msg.body(menu_text())
            return str(resp)

        svc = (extracted or {}).get("service", "") or ""
        dt_text = (extracted or {}).get("datetime_text", "") or ""

        # Service resolution
        svc_norm = svc.strip().lower()
        if svc_norm in SERVICE_MAP:
            svc_name, _, mins = SERVICE_MAP[svc_norm]
        else:
            # try alias match
            svc_name = ""
            for k, v in SERVICE_ALIASES.items():
                if k in user_text.lower():
                    svc_name = v
                    break
            if not svc_name:
                # if still unknown, show menu (friendly)
                msg.body(menu_text())
                return str(resp)
            mins = SERVICE_MAP[svc_name.lower()][2]

        st["service"] = svc_name
        st["mins"] = mins

        dt = parse_datetime_fallback(dt_text) if dt_text else parse_datetime_fallback(user_text)
        if not dt:
            st["step"] = "ask_time"
            msg.body(f"✍️ *{svc_name}*\n\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Wed 4:15pm\n• 10/02 15:30\n\nReply *BACK* to change service.")
            return str(resp)

        start_end = clamp_to_open_hours(dt, mins)
        if not start_end:
            msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time (e.g. *Tomorrow 2pm*).")
            return str(resp)

        start_dt, end_dt = start_end
        if is_time_available(CALENDAR_ID, start_dt, end_dt):
            create_booking_event(
                calendar_id=CALENDAR_ID,
                start_dt=start_dt,
                end_dt=end_dt,
                summary=f"{svc_name} - WhatsApp Booking",
                description=f"Booked via {BUSINESS_NAME}",
                phone=from_phone,
                service_name=svc_name,
            )
            st["step"] = "menu"
            st["offered"] = []
            msg.body(f"✅ Booked: *{svc_name}*\n📅 {format_slot(start_dt)}")
            return str(resp)

        # Taken -> offer alternatives and allow natural replies
        slots = next_available_slots(
            CALENDAR_ID,
            day_start=datetime.combine(start_dt.date(), OPEN_TIME, tzinfo=TZ),
            day_end=datetime.combine(start_dt.date(), CLOSE_TIME, tzinfo=TZ),
            duration_minutes=mins,
            step_minutes=SLOT_STEP_MINUTES,
            limit=3,
        )
        st["offered"] = slots
        st["step"] = "choose_alt"
        msg.body(offer_next_slots(svc_name, mins, start_dt))
        return str(resp)

    # If they’re in ask_time step, parse time + book
    if st.get("step") == "ask_time" and st.get("service"):
        svc = st["service"]
        mins = st["mins"] or SERVICE_MAP[svc.lower()][2]

        dt = parse_datetime_fallback(user_text)
        if not dt:
            msg.body("I didn’t understand the time.\nTry:\n• Tomorrow 2pm\n• Wed 4:15pm\n• 10/02 15:30\n\nReply *BACK* to change service.")
            return str(resp)

        start_end = clamp_to_open_hours(dt, mins)
        if not start_end:
            msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
            return str(resp)

        start_dt, end_dt = start_end
        if is_time_available(CALENDAR_ID, start_dt, end_dt):
            create_booking_event(
                calendar_id=CALENDAR_ID,
                start_dt=start_dt,
                end_dt=end_dt,
                summary=f"{svc} - WhatsApp Booking",
                description=f"Booked via {BUSINESS_NAME}",
                phone=from_phone,
                service_name=svc,
            )
            st["step"] = "menu"
            msg.body(f"✅ Booked: *{svc}*\n📅 {format_slot(start_dt)}")
            return str(resp)

        slots = next_available_slots(
            CALENDAR_ID,
            day_start=datetime.combine(start_dt.date(), OPEN_TIME, tzinfo=TZ),
            day_end=datetime.combine(start_dt.date(), CLOSE_TIME, tzinfo=TZ),
            duration_minutes=mins,
            step_minutes=SLOT_STEP_MINUTES,
            limit=3,
        )
        st["offered"] = slots
        st["step"] = "choose_alt"
        msg.body(offer_next_slots(svc, mins, start_dt))
        return str(resp)

    # Fallback: show friendly menu
    msg.body(menu_text())
    return str(resp)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
