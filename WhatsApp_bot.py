import os
import re
import json
import inspect
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import dateparser
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

# Try imports (your calendar_helper may not have all functions)
try:
    from calendar_helper import (
        is_time_available,
        next_available_slots,
        create_booking_event,
        delete_booking_event,
    )
except Exception:
    from calendar_helper import create_booking_event  # minimum
    is_time_available = None
    next_available_slots = None
    delete_booking_event = None

load_dotenv()

app = Flask(__name__)

# ---------------- CONFIG ----------------
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "25"))
DEBUG_LLM = os.getenv("DEBUG_LLM", "0") == "1"

HOLD_EXPIRE_MINUTES = int(os.getenv("HOLD_EXPIRE_MINUTES", "10"))
SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

# name, price, minutes
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
    "mens cut": "Haircut",
    "men cut": "Haircut",
    "skin fade": "Skin Fade",
    "fade": "Skin Fade",
    "shape": "Shape Up",
    "shape up": "Shape Up",
    "line up": "Shape Up",
    "beard": "Beard Trim",
    "beard trim": "Beard Trim",
    "hot towel": "Hot Towel Shave",
    "shave": "Hot Towel Shave",
    "kids": "Kids Cut",
    "kids cut": "Kids Cut",
    "child": "Kids Cut",
    "eyebrow": "Eyebrow Trim",
    "brow": "Eyebrow Trim",
    "nose": "Nose Wax",
    "ear": "Ear Wax",
    "blow": "Blow Dry",
    "blow dry": "Blow Dry",
}

SERVICE_MAP = {name: {"price": price, "minutes": minutes} for name, price, minutes in SERVICES}

# Simple in-memory state (good for now)
user_state = {}  # {from_number: {"step": "...", "service": "..."}}


# ---------------- HELPERS ----------------
def dlog(*args):
    if DEBUG_LLM:
        print("[DEBUG]", *args, flush=True)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def within_hours(dt: datetime) -> bool:
    day = dt.strftime("%a").lower()[:3]
    if day not in OPEN_DAYS:
        return False
    t = dt.timetz().replace(tzinfo=None)
    return OPEN_TIME <= t < CLOSE_TIME


def parse_dt(text: str) -> datetime | None:
    """
    Parse UK-ish natural language: "tomorrow 2pm", "Mon 3:15pm", "10/02 15:30"
    Returns timezone-aware datetime (Europe/London).
    """
    text = normalize(text)
    if not text:
        return None

    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.now(TZ),
    }
    dt = dateparser.parse(text, settings=settings)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    else:
        dt = dt.astimezone(TZ)
    # round to slot step
    minute = (dt.minute // SLOT_STEP_MINUTES) * SLOT_STEP_MINUTES
    dt = dt.replace(minute=minute, second=0, microsecond=0)
    return dt


def menu_text() -> str:
    lines = []
    lines.append(f"💈 {SHOP_NAME}")
    lines.append("Reply with number or name:\n")

    lines.append("Men’s Cuts")
    lines.append("1) Haircut — £18")
    lines.append("2) Skin Fade — £22")
    lines.append("3) Shape Up — £12\n")

    lines.append("Beard / Shaves")
    lines.append("4) Beard Trim — £10")
    lines.append("5) Hot Towel Shave — £15\n")

    lines.append("Kids")
    lines.append("6) Kids Cut — £15\n")

    lines.append("Grooming")
    lines.append("7) Eyebrow Trim — £6")
    lines.append("8) Nose Wax — £8")
    lines.append("9) Ear Wax — £8")
    lines.append("10) Blow Dry — £10\n")

    lines.append("Hours: Mon–Sat 9am–6pm | Sun Closed\n")
    lines.append('Tip: you can also type: "book haircut tomorrow at 2pm"')
    return "\n".join(lines)


def looks_like_booking_text(t: str) -> bool:
    t = normalize(t).lower()
    # If it contains a time/date indicator AND a booking/service hint, treat as booking text
    has_dateish = bool(re.search(r"\b(tomorrow|today|mon|tue|wed|thu|fri|sat|sun)\b", t)) or bool(
        re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", t)
    ) or bool(re.search(r"\b\d{1,2}[/-]\d{1,2}\b", t))
    has_intent = any(k in t for k in ["book", "booking", "appointment", "appt", "haircut", "fade", "beard", "kids", "shave", "shape"])
    return has_dateish and has_intent


def resolve_services(raw_services: list[str]) -> list[str]:
    resolved = []
    for s in raw_services or []:
        s_norm = normalize(s).lower()
        if not s_norm:
            continue
        # direct match
        for name in SERVICE_MAP.keys():
            if s_norm == name.lower():
                resolved.append(name)
                break
        else:
            # alias match
            for alias, name in SERVICE_ALIASES.items():
                if alias in s_norm:
                    resolved.append(name)
                    break
    # dedupe preserve order
    out = []
    for x in resolved:
        if x not in out:
            out.append(x)
    return out


def services_total_minutes(services: list[str]) -> int:
    return sum(SERVICE_MAP[s]["minutes"] for s in services if s in SERVICE_MAP) or 0


def safe_create_event(**kwargs):
    """
    Adapter: calls create_booking_event with only parameters it supports.
    Fixes: TypeError unexpected keyword argument 'phone' etc.
    """
    sig = inspect.signature(create_booking_event)
    allowed = set(sig.parameters.keys())
    filtered = {k: v for k, v in kwargs.items() if k in allowed}
    return create_booking_event(**filtered)


# ---------------- LLM (OpenAI Responses API) ----------------
def call_openai_json(user_text: str) -> dict | None:
    """
    Returns dict: {"services":[...], "datetime_text":"..."} or None
    """
    if not OPENAI_API_KEY:
        dlog("OPENAI_API_KEY missing -> skipping LLM")
        return None

    schema = {
        "name": "booking_extract",
        "schema": {
            "type": "object",
            "properties": {
                "services": {"type": "array", "items": {"type": "string"}},
                "datetime_text": {"type": ["string", "null"]},
            },
            "required": ["services", "datetime_text"],
            "additionalProperties": False,
        },
        "strict": True,
    }

    system = (
        "You extract booking info for a UK barbershop. "
        "Return the service(s) and the date/time text exactly as the user implied. "
        "If service not clear, return empty services list. "
        "If time not clear, return datetime_text null."
    )

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "text": {"format": {"type": "json_schema", "json_schema": schema}},
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=OPENAI_TIMEOUT,
        )
        if r.status_code >= 400:
            dlog("OPENAI ERROR", r.status_code, r.text[:500])
            return None

        data = r.json()
        # Responses API returns output text in output[...].content[...].text
        # We'll collect the first text chunk
        out_text = None
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text" and c.get("text"):
                    out_text = c["text"]
                    break
            if out_text:
                break

        if not out_text:
            dlog("OPENAI no output_text")
            return None

        parsed = json.loads(out_text)
        dlog("LLM JSON:", parsed)
        if not isinstance(parsed, dict):
            return None
        return parsed
    except Exception as e:
        dlog("OPENAI EXCEPTION:", repr(e))
        return None


# ---------------- ROUTES ----------------
@app.get("/")
def health():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp():
    from_ = request.values.get("From", "")
    body = normalize(request.values.get("Body", ""))
    lower = body.lower()

    resp = MessagingResponse()
    msg = resp.message()

    st = user_state.get(from_, {"step": "START"})
    step = st.get("step", "START")

    # Global commands
    if lower in {"menu", "start", "hi", "hello"}:
        st = {"step": "START"}
        user_state[from_] = st
        msg.body(menu_text())
        return str(resp)

    if lower == "back":
        st = {"step": "START"}
        user_state[from_] = st
        msg.body(menu_text())
        return str(resp)

    # =========================
    # 1) LLM-FIRST FREE TEXT
    # =========================
    # Only attempt if it "looks like booking" OR contains "book ..."
    if looks_like_booking_text(body) or lower.startswith("book "):
        data = call_openai_json(body)

        if data:
            raw_services = data.get("services") or []
            datetime_text = data.get("datetime_text")

            services = resolve_services(raw_services)
            dt = parse_dt(datetime_text) if datetime_text else None

            # If LLM got both service and time -> book now
            if services and dt:
                total_minutes = services_total_minutes(services)
                end_dt = dt + timedelta(minutes=total_minutes)

                if not within_hours(dt) or not within_hours(end_dt - timedelta(minutes=1)):
                    msg.body("⏰ That time is outside opening hours (Mon–Sat 9am–6pm). Try another time.")
                    return str(resp)

                # Availability check if you have it
                if is_time_available:
                    ok = is_time_available(CALENDAR_ID, dt, end_dt)
                    if not ok:
                        if next_available_slots:
                            slots = next_available_slots(CALENDAR_ID, dt, total_minutes, limit=3)
                            if slots:
                                pretty = "\n".join([s.strftime("%a %d %b %H:%M") for s in slots])
                                msg.body(f"❌ That slot isn’t free.\nNext available:\n{pretty}\n\nReply with one.")
                                st = {"step": "AWAIT_TIME_AFTER_LLM", "services": services}
                                user_state[from_] = st
                                return str(resp)
                        msg.body("❌ That slot isn’t free. Try another time.")
                        return str(resp)

                # Create event (SAFE adapter prevents 'phone' kwarg crashes)
                summary = f"{SHOP_NAME} - {', '.join(services)}"
                description = f"Booked via WhatsApp.\nFrom: {from_}\nServices: {', '.join(services)}"
                safe_create_event(
                    calendar_id=CALENDAR_ID,
                    start_dt=dt,
                    end_dt=end_dt,
                    summary=summary,
                    description=description,
                    phone=from_,  # will only be passed if your helper supports it
                )

                msg.body(f"✅ Booked: {', '.join(services)}\n📅 {dt.strftime('%a %d %b %H:%M')}")
                st = {"step": "START"}
                user_state[from_] = st
                return str(resp)

            # If service found but time missing -> ask time
            if services and not dt:
                st = {"step": "AWAIT_TIME", "services": services}
                user_state[from_] = st
                msg.body(
                    f"✍️ {services[0]}\n\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply BACK to change service."
                )
                return str(resp)

            # If time found but service missing -> show menu (so they pick service)
            if dt and not services:
                st = {"step": "START"}
                user_state[from_] = st
                msg.body("Which service would you like?\n\n" + menu_text())
                return str(resp)

        # If LLM fails, we do NOT break the old flow. Fall through.

    # =========================
    # 2) NORMAL MENU FLOW
    # =========================
    if step == "START":
        # Expect service selection
        # number?
        m = re.match(r"^\s*(\d+)\s*$", body)
        if m:
            idx = int(m.group(1))
            names = [s[0] for s in SERVICES]
            if 1 <= idx <= len(names):
                service = names[idx - 1]
                user_state[from_] = {"step": "AWAIT_TIME", "services": [service]}
                msg.body(
                    f"✍️ {service}\n\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply BACK to change service."
                )
                return str(resp)

        # name?
        chosen = None
        low = lower
        for name in SERVICE_MAP.keys():
            if name.lower() in low:
                chosen = name
                break
        if not chosen:
            # alias
            for alias, name in SERVICE_ALIASES.items():
                if alias in low:
                    chosen = name
                    break

        if chosen:
            user_state[from_] = {"step": "AWAIT_TIME", "services": [chosen]}
            msg.body(
                f"✍️ {chosen}\n\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply BACK to change service."
            )
            return str(resp)

        msg.body(menu_text())
        return str(resp)

    if step in {"AWAIT_TIME", "AWAIT_TIME_AFTER_LLM"}:
        services = st.get("services") or []
        dt = parse_dt(body)
        if not dt:
            msg.body("I didn’t understand the time.\nTry:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply BACK to change service.")
            return str(resp)

        total_minutes = services_total_minutes(services)
        end_dt = dt + timedelta(minutes=total_minutes)

        if not within_hours(dt) or not within_hours(end_dt - timedelta(minutes=1)):
            msg.body("⏰ That time is outside opening hours (Mon–Sat 9am–6pm). Try another time.")
            return str(resp)

        if is_time_available:
            ok = is_time_available(CALENDAR_ID, dt, end_dt)
            if not ok:
                if next_available_slots:
                    slots = next_available_slots(CALENDAR_ID, dt, total_minutes, limit=3)
                    if slots:
                        pretty = "\n".join([s.strftime("%a %d %b %H:%M") for s in slots])
                        msg.body(f"❌ That slot isn’t free.\nNext available:\n{pretty}\n\nReply with one.")
                        user_state[from_] = {"step": "AWAIT_TIME_AFTER_LLM", "services": services}
                        return str(resp)
                msg.body("❌ That slot isn’t free. Try another time.")
                return str(resp)

        summary = f"{SHOP_NAME} - {', '.join(services)}"
        description = f"Booked via WhatsApp.\nFrom: {from_}\nServices: {', '.join(services)}"
        safe_create_event(
            calendar_id=CALENDAR_ID,
            start_dt=dt,
            end_dt=end_dt,
            summary=summary,
            description=description,
            phone=from_,
        )

        msg.body(f"✅ Booked: {', '.join(services)}\n📅 {dt.strftime('%a %d %b %H:%M')}")
        user_state[from_] = {"step": "START"}
        return str(resp)

    # Fallback
    user_state[from_] = {"step": "START"}
    msg.body(menu_text())
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
