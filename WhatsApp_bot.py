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

# OpenAI (new SDK)
from openai import OpenAI

load_dotenv()

# ---------------- CONFIG ----------------
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))

SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))
HOLD_EXPIRE_MINUTES = int(os.getenv("HOLD_EXPIRE_MINUTES", "10"))

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

DEBUG_LLM = os.getenv("DEBUG_LLM", "0") == "1"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "25"))

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Services: (Name, Price, Minutes)
SERVICES = [
    ("Haircut", 18, 45),
    ("Skin Fade", 22, 60),
    ("Shape Up", 12, 20),
    ("Beard Trim", 10, 20),
    ("Kids Cut", 15, 45),
    ("Hot Towel Shave", 15, 30),
    ("Eyebrow Trim", 6, 10),
    ("Nose Wax", 8, 10),
    ("Ear Wax", 8, 10),
    ("Blow Dry", 10, 15),
]

# Aliases → canonical
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
    "kids": "Kids Cut",
    "kids cut": "Kids Cut",
    "hot towel": "Hot Towel Shave",
    "hot towel shave": "Hot Towel Shave",
    "eyebrow": "Eyebrow Trim",
    "nose wax": "Nose Wax",
    "ear wax": "Ear Wax",
    "blow dry": "Blow Dry",
}

SERVICE_BY_NUM = {str(i + 1): s[0] for i, s in enumerate(SERVICES)}
SERVICE_INFO = {name: {"price": price, "minutes": mins} for name, price, mins in SERVICES}


# ---------------- APP ----------------
app = Flask(__name__)

# VERY SIMPLE in-memory session (good enough for now)
user_state = {}  # { from_number: { "step": "...", "service": "...", "pending_services": [...]} }


# ---------------- HELPERS ----------------
def normalize(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def within_hours(dt: datetime, duration_min: int) -> bool:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    end_dt = dt + timedelta(minutes=duration_min)
    day = dt.strftime("%a").lower()[:3]
    if day not in OPEN_DAYS:
        return False
    if not (OPEN_TIME <= dt.time() < CLOSE_TIME):
        return False
    if not (OPEN_TIME < end_dt.time() <= CLOSE_TIME):
        return False
    return True


def menu_text() -> str:
    parts = [f"💈 {SHOP_NAME}\nReply with *number or name:*\n"]
    parts.append("*Men’s Cuts*")
    # Show a short menu (top 5), you can keep the long one if you want
    for i, (name, price, _) in enumerate(SERVICES[:5], start=1):
        parts.append(f"{i}) {name} — £{price}")
    parts.append("\nType *MENU* anytime.")
    parts.append("\nTip: you can also type: *Book haircut tomorrow at 2pm*")
    return "\n".join(parts)


def resolve_services(raw_services):
    """raw_services can be list[str] from LLM, or user text"""
    resolved = []
    if not raw_services:
        return resolved
    if isinstance(raw_services, str):
        raw_services = [raw_services]

    for s in raw_services:
        key = normalize(s)
        if key in SERVICE_ALIASES:
            resolved.append(SERVICE_ALIASES[key])
            continue
        # Try contains match
        for alias, canon in SERVICE_ALIASES.items():
            if alias in key:
                resolved.append(canon)
                break
        else:
            # Try exact service match
            for name in SERVICE_INFO.keys():
                if normalize(name) == key:
                    resolved.append(name)
                    break

    # dedupe while preserving order
    out = []
    for x in resolved:
        if x not in out:
            out.append(x)
    return out


def services_total_minutes(services):
    return sum(SERVICE_INFO[s]["minutes"] for s in services if s in SERVICE_INFO)


def parse_dt(text: str):
    if not text:
        return None
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }
    dt = dateparser.parse(text, settings=settings)
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def looks_like_booking_text(t: str) -> bool:
    t = normalize(t)
    if not t:
        return False
    # has time or date words
    if re.search(r"\b(tomorrow|today|mon|tue|wed|thu|fri|sat|sun)\b", t):
        return True
    if re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", t):
        return True
    if re.search(r"\b\d{1,2}[\/\-]\d{1,2}\b", t):
        return True
    keywords = ["book", "booking", "appointment", "haircut", "fade", "beard", "kids", "shape"]
    return any(k in t for k in keywords)


# ---------------- LLM EXTRACTOR ----------------
def call_openai_json(user_text: str):
    """
    Returns dict like:
      {"services":["Haircut"], "datetime_text":"tomorrow 2pm"}
    Uses Responses API with json_schema.
    FIXED: includes text.format.name (your error).
    """
    if not client:
        return None

    schema = {
        "type": "object",
        "properties": {
            "services": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of services requested (e.g. Haircut, Skin Fade, Beard Trim, Kids Cut...)",
            },
            "datetime_text": {
                "type": "string",
                "description": "User requested date/time text exactly (e.g. 'tomorrow 2pm', 'Tue 14:00')",
            },
        },
        "required": ["services", "datetime_text"],
        "additionalProperties": False,
    }

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": (
                        f"You extract booking details for a barbershop.\n"
                        f"Return JSON only.\n"
                        f"Valid services: {', '.join(SERVICE_INFO.keys())}.\n"
                        f"If service not clear, return empty services [].\n"
                        f"If datetime not clear, return empty string ''."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            # IMPORTANT: name is required
            text={
                "format": {
                    "type": "json_schema",
                    "name": "booking_extract",
                    "schema": schema,
                }
            },
            timeout=OPENAI_TIMEOUT,
        )

        # The SDK returns content in output_text sometimes; easiest is parse the first text chunk
        text_out = resp.output_text
        if not text_out:
            return None
        data = json.loads(text_out)
        return data

    except Exception as e:
        if DEBUG_LLM:
            print(f"[DEBUG] OPENAI ERROR: {e}")
        return None


# ---------------- ROUTES ----------------
@app.get("/")
def health():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp():
    from_ = request.values.get("From", "")
    body = (request.values.get("Body") or "").strip()

    st = user_state.get(from_, {"step": "START"})
    step = st.get("step", "START")

    resp = MessagingResponse()
    msg = resp.message()

    t = normalize(body)

    # Global commands
    if t in ("menu", "start"):
        st = {"step": "START"}
        user_state[from_] = st
        msg.body(menu_text())
        return str(resp)

    if t == "back":
        st = {"step": "START"}
        user_state[from_] = st
        msg.body("No worries — send *MENU* to pick again, or type like: *Book haircut tomorrow 2pm*")
        return str(resp)

    # ---------------- LLM FIRST (only when at START) ----------------
    if step == "START" and looks_like_booking_text(body):
        data = call_openai_json(body)
        if data:
            raw_services = data.get("services") or []
            datetime_text = data.get("datetime_text") or ""

            services = resolve_services(raw_services)
            dt = parse_dt(datetime_text)

            # If it found both, try book immediately
            if services and dt:
                total_min = services_total_minutes(services)
                if not within_hours(dt, total_min):
                    msg.body(f"⏰ We’re open Mon–Sat 9am–6pm.\nTry another time (e.g. *Tomorrow 2pm*).")
                    return str(resp)

                end_dt = dt + timedelta(minutes=total_min)
                if is_time_available(CALENDAR_ID, dt, end_dt):
                    create_booking_event(
                        CALENDAR_ID,
                        dt,
                        end_dt,
                        summary=f"{services[0]} ({SHOP_NAME})",
                        description=f"Services: {', '.join(services)}\nFrom: {from_}",
                        phone=from_,  # safe now (calendar helper accepts it)
                    )
                    msg.body(f"✅ Booked: {', '.join(services)}\n📅 {dt.strftime('%a %d %b %H:%M')}")
                    return str(resp)
                else:
                    slots = next_available_slots(CALENDAR_ID, dt, total_min, step_minutes=SLOT_STEP_MINUTES, limit=3)
                    if slots:
                        lines = ["❌ That time is taken. Next available:"]
                        for sdt in slots:
                            lines.append(f"• {sdt.strftime('%a %d %b %H:%M')}")
                        lines.append("\nReply with one option (e.g. *Tomorrow 3pm*)")
                        msg.body("\n".join(lines))
                        # keep selected services in state
                        st = {"step": "ASK_TIME", "services": services}
                        user_state[from_] = st
                        return str(resp)
                    msg.body("❌ That time is taken. Reply with another time (e.g. *Tomorrow 3pm*).")
                    st = {"step": "ASK_TIME", "services": services}
                    user_state[from_] = st
                    return str(resp)

            # If it found service but not time -> ask time
            if services and not dt:
                st = {"step": "ASK_TIME", "services": services}
                user_state[from_] = st
                msg.body(f"✍️ *{services[0]}*\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply *BACK* to change service.")
                return str(resp)

            # If it found time but not service -> ask service
            if dt and not services:
                st = {"step": "ASK_SERVICE", "dt_text": datetime_text}
                user_state[from_] = st
                msg.body(menu_text())
                return str(resp)

        # If LLM failed or returned nothing usable → continue normal flow below

    # ---------------- NORMAL FLOW ----------------
    if step == "START":
        # Pick service by number or name
        if t in SERVICE_BY_NUM:
            service = SERVICE_BY_NUM[t]
            st = {"step": "ASK_TIME", "services": [service]}
            user_state[from_] = st
            msg.body(f"✍️ *{service}*\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply *BACK* to change service.")
            return str(resp)

        # try name match
        maybe = resolve_services([body])
        if maybe:
            st = {"step": "ASK_TIME", "services": [maybe[0]]}
            user_state[from_] = st
            msg.body(f"✍️ *{maybe[0]}*\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply *BACK* to change service.")
            return str(resp)

        msg.body(menu_text())
        return str(resp)

    if step == "ASK_SERVICE":
        dt_text = st.get("dt_text", "")
        if t in SERVICE_BY_NUM:
            service = SERVICE_BY_NUM[t]
        else:
            maybe = resolve_services([body])
            service = maybe[0] if maybe else None

        if not service:
            msg.body("Reply with a *number* or *service name*.\n\n" + menu_text())
            return str(resp)

        # now parse dt
        dt = parse_dt(dt_text)
        if not dt:
            st = {"step": "ASK_TIME", "services": [service]}
            user_state[from_] = st
            msg.body(f"✍️ *{service}*\nWhat day & time? (e.g. Tomorrow 2pm)")
            return str(resp)

        st = {"step": "ASK_TIME", "services": [service]}
        user_state[from_] = st
        # fall through to time booking using dt_text? simplest: ask user to confirm time again
        msg.body(f"Got it: *{service}*.\nNow reply with the time again (e.g. *Tomorrow 2pm*).")
        return str(resp)

    if step == "ASK_TIME":
        services = st.get("services", [])
        dt = parse_dt(body)
        if not dt:
            msg.body("I didn’t understand the time.\nTry:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply *BACK* to change service.")
            return str(resp)

        total_min = services_total_minutes(services)
        if not within_hours(dt, total_min):
            msg.body("⏰ We’re open Mon–Sat 9am–6pm.\nTry another time (e.g. *Tomorrow 2pm*).")
            return str(resp)

        end_dt = dt + timedelta(minutes=total_min)
        if not is_time_available(CALENDAR_ID, dt, end_dt):
            slots = next_available_slots(CALENDAR_ID, dt, total_min, step_minutes=SLOT_STEP_MINUTES, limit=3)
            if slots:
                lines = ["❌ That time is taken. Next available:"]
                for sdt in slots:
                    lines.append(f"• {sdt.strftime('%a %d %b %H:%M')}")
                lines.append("\nReply with one option (e.g. *Tomorrow 3pm*)")
                msg.body("\n".join(lines))
                return str(resp)
            msg.body("❌ That time is taken. Reply with another time (e.g. *Tomorrow 3pm*).")
            return str(resp)

        create_booking_event(
            CALENDAR_ID,
            dt,
            end_dt,
            summary=f"{services[0]} ({SHOP_NAME})",
            description=f"Services: {', '.join(services)}\nFrom: {from_}",
            phone=from_,
        )

        user_state[from_] = {"step": "START"}
        msg.body(f"✅ Booked: {', '.join(services)}\n📅 {dt.strftime('%a %d %b %H:%M')}")
        return str(resp)

    # Fallback
    user_state[from_] = {"step": "START"}
    msg.body(menu_text())
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
