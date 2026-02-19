import os
import re
import json
import sqlite3
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates
from dotenv import load_dotenv
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# Optional LLM (OpenAI SDK v1)
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
CLOSE_TIME = time(18, 0)  # appointment must FINISH by 18:00

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
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
    "men cut": "Haircut",
    "skin fade": "Skin Fade",
    "fade": "Skin Fade",
    "shape up": "Shape Up",
    "line up": "Shape Up",
    "lineup": "Shape Up",
    "beard": "Beard Trim",
    "beard trim": "Beard Trim",
    "kids": "Kids Cut",
    "kids cut": "Kids Cut",
    "child": "Kids Cut",
}

# ---------------- SESSION STORE (SQLite) ----------------
DB_PATH = os.getenv("SESSION_DB_PATH", "sessions.db")


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions (phone TEXT PRIMARY KEY, data TEXT, updated INTEGER)"
    )
    return conn


def load_session(phone: str) -> dict:
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT data FROM sessions WHERE phone=?", (phone,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return {"step": "menu"}

    try:
        st = json.loads(row[0])
        if isinstance(st, dict):
            return st
    except Exception:
        pass

    return {"step": "menu"}


def save_session(phone: str, st: dict):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions(phone, data, updated) VALUES(?,?,?) "
        "ON CONFLICT(phone) DO UPDATE SET data=excluded.data, updated=excluded.updated",
        (phone, json.dumps(st), int(datetime.now(tz=TZ).timestamp())),
    )
    conn.commit()
    conn.close()


def reset_session(phone: str):
    save_session(phone, {"step": "menu"})


# ---------------- HELPERS ----------------
def normalize_phone(raw: str) -> str:
    return (raw or "").strip().lower()


def menu_text() -> str:
    lines = [
        f"💈 *{SHOP_NAME}*",
        "Welcome! Reply with a number or name:\n",
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
        "• Can I get a haircut Friday at 2pm?",
        "",
        "Type *MENU* anytime to see this again.",
    ]
    return "\n".join(lines)


def pick_service_from_text(text: str) -> str | None:
    t = (text or "").strip().lower()

    # number selection: "1".."5"
    if re.fullmatch(r"\d+", t):
        idx = int(t)
        if 1 <= idx <= len(SERVICES):
            return SERVICES[idx - 1][0]

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
    new_dt = dt.replace(second=0, microsecond=0)
    minutes = new_dt.minute
    rounded = int(round(minutes / step) * step)
    if rounded == 60:
        new_dt = (new_dt.replace(minute=0) + timedelta(hours=1))
    else:
        new_dt = new_dt.replace(minute=rounded)
    return new_dt


def parse_datetime_any(text: str) -> datetime | None:
    """
    Tries dateparser.parse first, then dateparser.search.search_dates.
    Works even when text contains extra words like:
    "can I book haircut 2pm Friday"
    """
    base = datetime.now(tz=TZ)
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": base,
    }

    t = (text or "").strip()
    if not t:
        return None

    dt = dateparser.parse(t, settings=settings)
    if dt:
        return round_to_step(dt.astimezone(TZ))

    # fallback: search for a date inside the text
    found = search_dates(t, settings=settings)
    if found:
        _, dt2 = found[0]
        if dt2:
            return round_to_step(dt2.astimezone(TZ))

    return None


def within_opening_hours_for_service(start_dt: datetime, mins: int) -> bool:
    wd = start_dt.strftime("%a").lower()[:3]
    if wd not in OPEN_DAYS:
        return False

    start_local = start_dt.astimezone(TZ)
    end_local = start_local + timedelta(minutes=mins)

    if start_local.time() < OPEN_TIME:
        return False

    # must FINISH by close time
    if end_local.time() > CLOSE_TIME:
        return False

    return True


def extract_time_only(text: str) -> tuple[int, int] | None:
    t = (text or "").lower()

    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm

    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        hh = int(m.group(1))
        ap = m.group(2)
        if 1 <= hh <= 12:
            if ap == "pm" and hh != 12:
                hh += 12
            if ap == "am" and hh == 12:
                hh = 0
            return hh, 0

    return None


def match_offered_slot(text: str, offered: list[str]) -> datetime | None:
    if not offered:
        return None

    offered_dt = []
    for iso in offered:
        try:
            offered_dt.append(datetime.fromisoformat(iso).astimezone(TZ))
        except Exception:
            pass

    if not offered_dt:
        return None

    raw = (text or "").strip()

    # index 1/2/3
    if re.fullmatch(r"[1-9]\d*", raw):
        idx = int(raw)
        if 1 <= idx <= len(offered_dt):
            return offered_dt[idx - 1]

    # time-only match
    hm = extract_time_only(raw)
    if hm:
        hh, mm = hm
        for d in offered_dt:
            if d.hour == hh and d.minute == mm:
                return d

    # parsed datetime, match closest offered within 60 mins
    parsed = parse_datetime_any(raw)
    if parsed:
        best = min(offered_dt, key=lambda d: abs((d - parsed).total_seconds()))
        if abs((best - parsed).total_seconds()) <= 60 * 60:
            return best

    return None


def ask_time_text(service: str) -> str:
    return (
        f"✍️ *{service}*\n\n"
        "What day & time?\n"
        "Examples:\n"
        "• Tomorrow 2pm\n"
        "• Fri 2pm\n"
        "• 10/02 15:30\n\n"
        "Reply *BACK* to change service."
    )


# ---------------- LLM (Assist only) ----------------
client = None
if USE_LLM and OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        client = None


def llm_extract(text: str) -> dict:
    """
    Assist only. If OpenAI fails -> return None fields and continue normally.
    Returns: {service, datetime_text}
    """
    if not client:
        return {"service": None, "datetime_text": None}

    sys = (
        "You are a booking assistant for a barbershop.\n"
        "Extract ONLY these fields from the user's message.\n"
        "Return STRICT JSON with keys:\n"
        "service: one of [Haircut, Skin Fade, Shape Up, Beard Trim, Kids Cut] or null\n"
        "datetime_text: a short natural language date+time string or null\n"
        "If user message is only a number, service should be null.\n"
    )

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": sys},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_output_tokens=120,
            timeout=OPENAI_TIMEOUT,
        )
        out = (resp.output_text or "").strip()
    except Exception:
        # IMPORTANT: never crash the webhook
        return {"service": None, "datetime_text": None}

    out = out.strip("` \n")
    out = re.sub(r"^json\s*", "", out.strip(), flags=re.I)

    try:
        data = json.loads(out)
    except Exception:
        return {"service": None, "datetime_text": None}

    service = data.get("service")
    dt_text = data.get("datetime_text")

    valid = {s[0] for s in SERVICES}
    if service not in valid:
        service = None

    if isinstance(dt_text, str):
        dt_text = dt_text.strip() or None
    else:
        dt_text = None

    return {"service": service, "datetime_text": dt_text}


# ---------------- APP ----------------
app = Flask(__name__)


@app.get("/")
def health():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp():
    resp = MessagingResponse()
    msg = resp.message()

    try:
        from_number = normalize_phone(request.values.get("From", ""))
        body = (request.values.get("Body", "") or "").strip()
        body_l = body.lower().strip()

        st = load_session(from_number)
        step = st.get("step", "menu")

        # Global commands
        if body_l in {"menu", "start", "hi", "hello"}:
            save_session(from_number, {"step": "menu"})
            msg.body(menu_text())
            return str(resp)

        if body_l in {"reset", "restart"}:
            reset_session(from_number)
            msg.body("✅ Reset.\n\n" + menu_text())
            return str(resp)

        if body_l == "help":
            msg.body("Type *MENU* to see services, or say: *Book haircut Friday 2pm*.")
            return str(resp)

        llm = llm_extract(body) if USE_LLM else {"service": None, "datetime_text": None}

        # -------- STEP: menu --------
        if step == "menu":
            service = pick_service_from_text(body) or llm.get("service")

            # Try parse datetime from LLM OR from the whole message (works without LLM)
            dt = None
            if service:
                dt = parse_datetime_any(llm.get("datetime_text") or body)

            if service and dt:
                mins = service_minutes(service)

                if not within_opening_hours_for_service(dt, mins):
                    save_session(from_number, {"step": "ask_time", "service": service})
                    msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
                    return str(resp)

                if not is_time_available(CALENDAR_ID, dt, mins):
                    slots = next_available_slots(
                        CALENDAR_ID, mins, start_from=dt, tz=TZ, step_mins=SLOT_STEP_MINUTES, limit=3
                    )
                    offered = [s.isoformat() for s in slots]
                    save_session(from_number, {"step": "pick_slot", "service": service, "mins": mins, "offered": offered})

                    lines = ["❌ That time is taken. Next available:"]
                    for i, s in enumerate(slots, start=1):
                        lines.append(f"{i}) {s.strftime('%a %d %b %H:%M')}")
                    lines += [
                        "",
                        "Reply with *1/2/3* or the time (e.g. *09:15* or *Tomorrow 9am*).",
                        "Reply *BACK* to change service.",
                    ]
                    msg.body("\n".join(lines))
                    return str(resp)

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
                msg.body(f"✅ Booked: *{service}*\n🗓️ {dt.strftime('%a %d %b %H:%M')}\n\nType *MENU* to book another.")
                return str(resp)

            if service:
                save_session(from_number, {"step": "ask_time", "service": service})
                msg.body(ask_time_text(service))
                return str(resp)

            msg.body("Reply with *1–5* or type: *Book haircut Friday 2pm*.\n\n" + menu_text())
            return str(resp)

        # -------- STEP: ask_time --------
        if step == "ask_time":
            if body_l == "back":
                save_session(from_number, {"step": "menu"})
                msg.body(menu_text())
                return str(resp)

            service = st.get("service")
            if not service:
                save_session(from_number, {"step": "menu"})
                msg.body(menu_text())
                return str(resp)

            switched = pick_service_from_text(body) or llm.get("service")
            if switched:
                service = switched

            mins = service_minutes(service)
            dt = parse_datetime_any(llm.get("datetime_text") or body)
            if not dt:
                save_session(from_number, {"step": "ask_time", "service": service})
                msg.body("I didn’t understand the time.\nTry: *Fri 2pm* / *Tomorrow 2pm* / *10/02 15:30*.\n\nReply *BACK* to change service.")
                return str(resp)

            if not within_opening_hours_for_service(dt, mins):
                save_session(from_number, {"step": "ask_time", "service": service})
                msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
                return str(resp)

            if not is_time_available(CALENDAR_ID, dt, mins):
                slots = next_available_slots(
                    CALENDAR_ID, mins, start_from=dt, tz=TZ, step_mins=SLOT_STEP_MINUTES, limit=3
                )
                offered = [s.isoformat() for s in slots]
                save_session(from_number, {"step": "pick_slot", "service": service, "mins": mins, "offered": offered})

                lines = ["❌ That time is taken. Next available:"]
                for i, s in enumerate(slots, start=1):
                    lines.append(f"{i}) {s.strftime('%a %d %b %H:%M')}")
                lines += [
                    "",
                    "Reply with *1/2/3* or the time (e.g. *09:15* or *Tomorrow 9am*).",
                    "Reply *BACK* to change service.",
                ]
                msg.body("\n".join(lines))
                return str(resp)

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
            msg.body(f"✅ Booked: *{service}*\n🗓️ {dt.strftime('%a %d %b %H:%M')}\n\nAnything else? Type *MENU* to book another.")
            return str(resp)

        # -------- STEP: pick_slot --------
        if step == "pick_slot":
            if body_l == "back":
                save_session(from_number, {"step": "menu"})
                msg.body(menu_text())
                return str(resp)

            service = st.get("service")
            mins = int(st.get("mins", service_minutes(service or "Haircut")))
            offered = st.get("offered", [])

            chosen = match_offered_slot(body, offered) or parse_datetime_any(llm.get("datetime_text") or body)
            if not chosen:
                msg.body("Reply with *1/2/3* or a time like *09:15* / *Tomorrow 9am*. (Or *BACK* to change service.)")
                return str(resp)

            chosen = chosen.astimezone(TZ).replace(second=0, microsecond=0)

            if not within_opening_hours_for_service(chosen, mins):
                msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another.")
                return str(resp)

            if not is_time_available(CALENDAR_ID, chosen, mins):
                slots = next_available_slots(
                    CALENDAR_ID, mins, start_from=chosen, tz=TZ, step_mins=SLOT_STEP_MINUTES, limit=3
                )
                offered = [s.isoformat() for s in slots]
                save_session(from_number, {"step": "pick_slot", "service": service, "mins": mins, "offered": offered})

                lines = ["❌ That time just got taken. Next available:"]
                for i, s in enumerate(slots, start=1):
                    lines.append(f"{i}) {s.strftime('%a %d %b %H:%M')}")
                lines += ["", "Reply with *1/2/3* or the time (e.g. *09:15*)."]
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
            msg.body(f"✅ Booked: *{service}*\n🗓️ {chosen.strftime('%a %d %b %H:%M')}\n\nAnything else? Type *MENU* to book another.")
            return str(resp)

        # Unknown step recovery
        reset_session(from_number)
        msg.body(menu_text())
        return str(resp)

    except Exception:
        # Last-resort safety: never break Twilio
        msg.body("Sorry—something went wrong. Type *MENU* to start again.")
        return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
