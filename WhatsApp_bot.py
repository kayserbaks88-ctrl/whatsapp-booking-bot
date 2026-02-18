import os
import re
import json
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import dateparser
from dotenv import load_dotenv
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# Optional LLM (OpenAI SDK v1+)
USE_LLM = True
try:
    from openai import OpenAI
except Exception:
    USE_LLM = False

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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20"))

# Services: (name, price, minutes)
SERVICES = [
    ("Haircut", 18, 45),
    ("Skin Fade", 22, 60),
    ("Shape Up", 12, 20),
    ("Beard Trim", 10, 20),
    ("Kids Cut", 15, 30),
]
SERVICE_ALIASES = {
    "haircut": "Haircut",
    "cut": "Haircut",
    "mens cut": "Haircut",
    "men's cut": "Haircut",
    "skin fade": "Skin Fade",
    "fade": "Skin Fade",
    "shape up": "Shape Up",
    "line up": "Shape Up",
    "lineup": "Shape Up",
    "beard": "Beard Trim",
    "beard trim": "Beard Trim",
    "kids": "Kids Cut",
    "kid": "Kids Cut",
    "kids cut": "Kids Cut",
    "child": "Kids Cut",
}

# ---------------- SIMPLE SESSION (in-memory) ----------------
# Memory option 1: local runtime state (best for now)
SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(6 * 60 * 60)))  # 6 hours

def _now() -> datetime:
    return datetime.now(tz=TZ)

def normalize_phone(raw: str) -> str:
    # Twilio gives "whatsapp:+44..."
    return (raw or "").strip().lower()

def load_session(phone: str) -> dict:
    st = SESSIONS.get(phone)
    if not st:
        return {"step": "menu"}
    if int(st.get("_ts", 0)) < int(_now().timestamp()) - SESSION_TTL_SECONDS:
        return {"step": "menu"}
    return st

def save_session(phone: str, st: dict):
    st["_ts"] = int(_now().timestamp())
    SESSIONS[phone] = st

def reset_session(phone: str):
    save_session(phone, {"step": "menu"})

# ---------------- HELPERS ----------------
def menu_text() -> str:
    lines = [
        f"💈 *{SHOP_NAME}*",
        "Welcome! Reply with a *number* or *name*:\n",
        "*Men’s Cuts*",
    ]
    for i, (name, price, _mins) in enumerate(SERVICES, start=1):
        lines.append(f"{i}) {name} — £{price}")
    lines += [
        "",
        "Hours: Mon–Sat 9am–6pm | Sun Closed",
        "",
        "Tip: you can type a full sentence like:",
        "• Book a skin fade tomorrow at 2pm",
        "• Can I get a haircut Wednesday around 4?",
        "",
        "Type *MENU* anytime to see this again.",
    ]
    return "\n".join(lines)

def extract_first_int(text: str) -> int | None:
    # Handles weird invisible chars from WhatsApp like "3\u200e"
    m = re.search(r"\b(\d{1,2})\b", (text or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

def pick_service_from_text(text: str) -> str | None:
    t = (text or "").strip().lower()

    # number selection
    n = extract_first_int(t)
    if n is not None and 1 <= n <= len(SERVICES):
        return SERVICES[n - 1][0]

    # exact service names
    for name, _, _ in SERVICES:
        if name.lower() in t:
            return name

    # aliases
    for k, v in SERVICE_ALIASES.items():
        if k in t:
            return v

    return None

def service_minutes(service_name: str) -> int:
    for n, _, mins in SERVICES:
        if n == service_name:
            return mins
    return 45

def round_to_step(dt: datetime) -> datetime:
    step = SLOT_STEP_MINUTES
    dt = dt.replace(second=0, microsecond=0)
    rounded = int(round(dt.minute / step) * step)
    if rounded == 60:
        dt = (dt.replace(minute=0) + timedelta(hours=1))
    else:
        dt = dt.replace(minute=rounded)
    return dt

def parse_datetime(text: str) -> datetime | None:
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": _now(),
    }
    dt = dateparser.parse(text, settings=settings)
    if not dt:
        return None
    dt = dt.astimezone(TZ)
    return round_to_step(dt)

def within_opening_hours(start_dt: datetime, mins: int) -> bool:
    wd = start_dt.strftime("%a").lower()[:3]
    if wd not in OPEN_DAYS:
        return False

    open_dt = start_dt.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute, second=0, microsecond=0)
    close_dt = start_dt.replace(hour=CLOSE_TIME.hour, minute=CLOSE_TIME.minute, second=0, microsecond=0)

    end_dt = start_dt + timedelta(minutes=mins)

    # Must start at/after opening, and finish at/before closing
    return (start_dt >= open_dt) and (end_dt <= close_dt)

def match_offered_slot(text: str, offered_iso: list[str]) -> datetime | None:
    if not offered_iso:
        return None

    offered = []
    for iso in offered_iso:
        try:
            offered.append(datetime.fromisoformat(iso).astimezone(TZ))
        except Exception:
            pass
    if not offered:
        return None

    # If user typed a time-only like 09:15
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text or "")
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        for d in offered:
            if d.hour == hh and d.minute == mm:
                return d

    # Else parse and choose closest offered within 45 mins
    parsed = parse_datetime(text or "")
    if parsed:
        best = min(offered, key=lambda d: abs((d - parsed).total_seconds()))
        if abs((best - parsed).total_seconds()) <= 45 * 60:
            return best

    return None

# ---------------- LLM (Structured extraction) ----------------
client = OpenAI(api_key=OPENAI_API_KEY) if (USE_LLM and OPENAI_API_KEY) else None

def llm_extract(text: str) -> dict:
    """
    Returns: {"intent": "...", "service": "...|None", "datetime_text": "...|None"}
    """
    if not client:
        return {"intent": "unknown", "service": None, "datetime_text": None}

    sys = (
        "You are a booking assistant for a UK barbershop.\n"
        "Extract booking intent from the user's message.\n"
        "Return STRICT JSON ONLY (no markdown) with keys:\n"
        "intent: one of [book, menu, help, unknown]\n"
        "service: one of [Haircut, Skin Fade, Shape Up, Beard Trim, Kids Cut] or null\n"
        "datetime_text: a short natural language date+time string or null\n"
        "If user message is just a number like '2', treat as service selection (service should be Skin Fade).\n"
        "If user asks 'Wednesday at 4', datetime_text should be 'Wednesday 4pm'.\n"
    )

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": sys},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_output_tokens=180,
            timeout=OPENAI_TIMEOUT,
        )
        out = (resp.output_text or "").strip()
        out = out.strip("` \n")
        out = re.sub(r"^json\s*", "", out.strip(), flags=re.I)
        data = json.loads(out)

        intent = data.get("intent", "unknown")
        service = data.get("service", None)
        dt_text = data.get("datetime_text", None)

        valid = {s[0] for s in SERVICES}
        if service not in valid:
            service = None
        if intent not in {"book", "menu", "help", "unknown"}:
            intent = "unknown"

        return {"intent": intent, "service": service, "datetime_text": dt_text}
    except Exception:
        return {"intent": "unknown", "service": None, "datetime_text": None}

# ---------------- APP ----------------
app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

@app.post("/whatsapp")
def whatsapp():
    from_number = normalize_phone(request.values.get("From", ""))
    body = (request.values.get("Body", "") or "").strip()
    body_l = body.lower().strip()

    resp = MessagingResponse()
    msg = resp.message()

    st = load_session(from_number)

    # Global commands
    if body_l in {"menu", "start", "hi", "hello"}:
        reset_session(from_number)
        msg.body(menu_text())
        return str(resp)

    if body_l in {"reset", "restart"}:
        reset_session(from_number)
        msg.body("✅ Reset.\n\n" + menu_text())
        return str(resp)

    llm = llm_extract(body) if USE_LLM else {"intent": "unknown", "service": None, "datetime_text": None}

    # If user typed "menu" vibes
    if llm.get("intent") == "menu":
        reset_session(from_number)
        msg.body(menu_text())
        return str(resp)

    # ---------- STEP: menu ----------
    if st.get("step") == "menu":
        service = pick_service_from_text(body) or llm.get("service")
        dt_text = llm.get("datetime_text")

        # Full sentence booking in menu
        if service and dt_text:
            mins = service_minutes(service)
            dt = parse_datetime(dt_text)

            if not dt:
                save_session(from_number, {"step": "ask_time", "service": service})
                msg.body(
                    f"✍️ *{service}*\n\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Wed 4:15pm\n• 10/02 15:30\n\nReply *BACK* to change service."
                )
                return str(resp)

            if not within_opening_hours(dt, mins):
                save_session(from_number, {"step": "ask_time", "service": service})
                msg.body("That time isn’t within opening hours (Mon–Sat 9–6). What time would you like instead?")
                return str(resp)

            if not is_time_available(CALENDAR_ID, dt, mins):
                slots = next_available_slots(
                    CALENDAR_ID, mins, start_from=dt, tz=TZ, step_mins=SLOT_STEP_MINUTES, limit=3
                )
                save_session(from_number, {"step": "pick_slot", "service": service, "mins": mins, "offered": [s.isoformat() for s in slots]})
                lines = ["❌ That time is taken. Next available:"]
                for s in slots:
                    lines.append(f"• {s.strftime('%a %d %b %H:%M')}")
                lines.append("")
                lines.append("Reply with one option (e.g. *09:15* or *Tue 17 Feb 09:15*)")
                msg.body("\n".join(lines))
                return str(resp)

            # Book
            end_dt = dt + timedelta(minutes=mins)
            create_booking_event(
                calendar_id=CALENDAR_ID,
                start_dt=dt,
                end_dt=end_dt,
                summary=f"{service} - WhatsApp Booking",
                description=f"Booked via {BUSINESS_NAME}",
                phone=from_number,
                service_name=service,
            )
            reset_session(from_number)
            msg.body(
                f"✅ Booked: *{service}*\n🗓️ {dt.strftime('%a %d %b %H:%M')}\n\nAnything else? Type *MENU* to book another."
            )
            return str(resp)

        # Normal selection (number/name)
        if service:
            save_session(from_number, {"step": "ask_time", "service": service})
            msg.body(
                f"✍️ *{service}*\n\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Wed 4:15pm\n• 10/02 15:30\n\nReply *BACK* to change service."
            )
            return str(resp)

        # If they typed a time without picking service, guide once instead of looping menu forever
        maybe_dt = parse_datetime(body) or parse_datetime(llm.get("datetime_text") or "")
        if maybe_dt:
            msg.body("Which service is that for?\n\nReply with a number (1–5) or name.\n\n" + menu_text())
            return str(resp)

        msg.body(menu_text())
        return str(resp)

    # ---------- STEP: ask_time ----------
    if st.get("step") == "ask_time":
        if body_l == "back":
            reset_session(from_number)
            msg.body(menu_text())
            return str(resp)

        service = st.get("service") or pick_service_from_text(body) or llm.get("service")
        if not service:
            # Keep them in ask_time but clarify service
            msg.body("Which service would you like? Reply 1–5 or name.\n\n" + menu_text())
            return str(resp)

        # allow switching service by number/name while in ask_time
        switched = pick_service_from_text(body) or llm.get("service")
        if switched:
            service = switched

        mins = service_minutes(service)

        dt_text = llm.get("datetime_text") or body
        dt = parse_datetime(dt_text)

        if not dt:
            save_session(from_number, {"step": "ask_time", "service": service})
            msg.body("I didn’t understand the time.\nTry:\n• Tomorrow 2pm\n• Wed 4:15pm\n• 10/02 15:30\n\nReply *BACK* to change service.")
            return str(resp)

        if not within_opening_hours(dt, mins):
            save_session(from_number, {"step": "ask_time", "service": service})
            msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
            return str(resp)

        if not is_time_available(CALENDAR_ID, dt, mins):
            slots = next_available_slots(
                CALENDAR_ID, mins, start_from=dt, tz=TZ, step_mins=SLOT_STEP_MINUTES, limit=3
            )
            save_session(from_number, {"step": "pick_slot", "service": service, "mins": mins, "offered": [s.isoformat() for s in slots]})
            lines = ["❌ That time is taken. Next available:"]
            for s in slots:
                lines.append(f"• {s.strftime('%a %d %b %H:%M')}")
            lines.append("")
            lines.append("Reply with one option (e.g. *09:15* or *Tue 17 Feb 09:15*)")
            msg.body("\n".join(lines))
            return str(resp)

        # Book
        end_dt = dt + timedelta(minutes=mins)
        create_booking_event(
            calendar_id=CALENDAR_ID,
            start_dt=dt,
            end_dt=end_dt,
            summary=f"{service} - WhatsApp Booking",
            description=f"Booked via {BUSINESS_NAME}",
            phone=from_number,
            service_name=service,
        )
        reset_session(from_number)
        msg.body(
            f"✅ Booked: *{service}*\n🗓️ {dt.strftime('%a %d %b %H:%M')}\n\nAnything else? Type *MENU* to book another."
        )
        return str(resp)

    # ---------- STEP: pick_slot ----------
    if st.get("step") == "pick_slot":
        service = st.get("service")
        mins = int(st.get("mins", service_minutes(service)))
        offered = st.get("offered", [])

        chosen = match_offered_slot(body, offered)
        if not chosen:
            # allow free-typed time too
            dt = parse_datetime(llm.get("datetime_text") or body)
            if dt:
                chosen = dt

        if not chosen:
            msg.body("Reply with one of the offered options (e.g. *09:15* or *Tue 17 Feb 09:15*).")
            return str(resp)

        chosen = chosen.astimezone(TZ).replace(second=0, microsecond=0)

        if not within_opening_hours(chosen, mins):
            msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another.")
            return str(resp)

        if not is_time_available(CALENDAR_ID, chosen, mins):
            slots = next_available_slots(
                CALENDAR_ID, mins, start_from=chosen, tz=TZ, step_mins=SLOT_STEP_MINUTES, limit=3
            )
            save_session(from_number, {"step": "pick_slot", "service": service, "mins": mins, "offered": [s.isoformat() for s in slots]})
            lines = ["❌ That time just got taken. Next available:"]
            for s in slots:
                lines.append(f"• {s.strftime('%a %d %b %H:%M')}")
            lines.append("")
            lines.append("Reply with one option (e.g. *09:15*)")
            msg.body("\n".join(lines))
            return str(resp)

        end_dt = chosen + timedelta(minutes=mins)
        create_booking_event(
            calendar_id=CALENDAR_ID,
            start_dt=chosen,
            end_dt=end_dt,
            summary=f"{service} - WhatsApp Booking",
            description=f"Booked via {BUSINESS_NAME}",
            phone=from_number,
            service_name=service,
        )
        reset_session(from_number)
        msg.body(
            f"✅ Booked: *{service}*\n🗓️ {chosen.strftime('%a %d %b %H:%M')}\n\nAnything else? Type *MENU* to book another."
        )
        return str(resp)

    # Fallback
    reset_session(from_number)
    msg.body(menu_text())
    return str(resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
