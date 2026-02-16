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

app = Flask(__name__)

# ---------------- CONFIG ----------------
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "10000"))

SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))
BUFFER_MINUTES = int(os.getenv("BUFFER_MINUTES", "0"))

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "15"))
DEBUG_LLM = os.getenv("DEBUG_LLM", "").strip() in {"1", "true", "True", "YES", "yes"}

SERVICES = [
    ("Haircut", 18, 45),
    ("Skin Fade", 22, 60),
    ("Shape Up", 12, 30),
    ("Beard Trim", 10, 30),
    ("Hot Towel Shave", 15, 45),
    ("Kids Cut", 15, 45),
    ("Eyebrow Trim", 6, 15),
    ("Nose Wax", 8, 15),
    ("Ear Wax", 8, 15),
    ("Blow Dry", 10, 30),
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
    "shave": "Hot Towel Shave",
    "kids": "Kids Cut",
    "kids cut": "Kids Cut",
    "eyebrow": "Eyebrow Trim",
    "nose wax": "Nose Wax",
    "ear wax": "Ear Wax",
    "blow dry": "Blow Dry",
}

SERVICE_MAP = {name.lower(): (name, price, mins) for (name, price, mins) in SERVICES}

# simple in-memory state (ok for now)
user_state = {}


def menu_text():
    lines = [f"💈 {SHOP_NAME}\nReply with number or name:\n"]
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
    lines.append("Hours: Mon–Sat 9am–6pm | Sun Closed")
    lines.append('\nTip: you can also type: "Book haircut tomorrow at 2pm"')
    return "\n".join(lines)


def looks_like_booking_text(t: str) -> bool:
    t = (t or "").lower()
    if re.search(r"\b(tomorrow|today|mon|tue|wed|thu|fri|sat|sun)\b", t):
        return True
    if re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", t):
        return True
    if re.search(r"\b\d{1,2}[/-]\d{1,2}\b", t):
        return True
    keywords = ["book", "appointment", "haircut", "fade", "beard", "shave", "kids", "wax", "blow"]
    return any(k in t for k in keywords)


def parse_dt(text: str):
    if not text:
        return None
    dt = dateparser.parse(
        text,
        settings={
            "TIMEZONE": TIMEZONE,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )
    return dt.astimezone(TZ) if dt else None


def within_hours(dt: datetime, minutes_len: int = 0) -> bool:
    if not dt:
        return False
    day = dt.strftime("%a").lower()[:3]
    if day not in OPEN_DAYS:
        return False
    start_ok = OPEN_TIME <= dt.time() < CLOSE_TIME
    end_dt = dt + timedelta(minutes=minutes_len)
    end_ok = end_dt.time() <= CLOSE_TIME
    return start_ok and end_ok


def services_total_minutes(services):
    total = 0
    for s in services:
        name = s.lower()
        if name in SERVICE_MAP:
            total += SERVICE_MAP[name][2]
    return total


def resolve_services(raw_services):
    out = []
    for s in (raw_services or []):
        if not s:
            continue
        k = s.strip().lower()
        if k in SERVICE_MAP:
            out.append(SERVICE_MAP[k][0])
            continue
        if k in SERVICE_ALIASES:
            out.append(SERVICE_ALIASES[k])
            continue
        # best-effort partial match
        for alias, canon in SERVICE_ALIASES.items():
            if alias in k:
                out.append(canon)
                break
    # de-dupe keep order
    seen = set()
    final = []
    for s in out:
        if s not in seen:
            seen.add(s)
            final.append(s)
    return final


def call_openai_json(user_text: str):
    """
    Returns dict: {"services":[...], "datetime_text":"..."} or None
    Uses OpenAI Responses API with a JSON schema.
    """
    if not OPENAI_API_KEY:
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
        "Return services and the requested date/time text exactly as the user means it. "
        "If time is missing, datetime_text should be null."
    )

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "response_format": {"type": "json_schema", "json_schema": schema},
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=OPENAI_TIMEOUT,
        )
        if r.status_code >= 400:
            if DEBUG_LLM:
                print("LLM HTTP", r.status_code, r.text[:500])
            return None

        data = r.json()
        # Responses API puts text in output[].content[].text OR output_text
        # We handle both safely:
        raw_text = data.get("output_text")
        if not raw_text:
            out = data.get("output", [])
            if out and out[0].get("content"):
                raw_text = out[0]["content"][0].get("text")

        if not raw_text:
            if DEBUG_LLM:
                print("LLM no output_text")
            return None

        parsed = json.loads(raw_text)
        if DEBUG_LLM:
            print("LLM parsed:", parsed)
        return parsed

    except Exception as e:
        if DEBUG_LLM:
            print("LLM exception:", repr(e))
        return None


@app.get("/")
def health():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp():
    from_ = request.values.get("From", "")
    body = (request.values.get("Body") or "").strip()
    resp = MessagingResponse()
    msg = resp.message()

    # quick commands
    if body.lower() in {"menu", "start", "hi", "hello"}:
        user_state[from_] = {"step": "START"}
        msg.body(menu_text())
        return str(resp)

    # ---------------- LLM FIRST ----------------
    if looks_like_booking_text(body):
        data = call_openai_json(body)
        if data:
            raw_services = data.get("services") or []
            datetime_text = data.get("datetime_text")

            services = resolve_services(raw_services)

            # If LLM found services but no datetime -> ask time
            if services and not datetime_text:
                user_state[from_] = {"step": "AWAIT_TIME", "services": services}
                msg.body(
                    f"✍️ {services[0] if len(services)==1 else 'Services selected'}\n\n"
                    "What day & time?\nExamples:\n"
                    "• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\n"
                    "Reply BACK to change service."
                )
                return str(resp)

            # If datetime provided -> try parse and book
            if services and datetime_text:
                dt = parse_dt(datetime_text)

                # FINAL safety fallback: manually extract common patterns
                if not dt:
                    possible = re.search(r"(tomorrow.*|today.*|\b\d{1,2}(:\d{2})?\s?(am|pm)\b.*)", body.lower())
                    if possible:
                        dt = parse_dt(possible.group(1))

                if not dt:
                    msg.body(
                        "I didn’t understand the time.\nTry:\n"
                        "• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\n"
                        "Reply BACK to change service."
                    )
                    return str(resp)

                total_minutes = services_total_minutes(services)
                if not within_hours(dt, total_minutes):
                    msg.body("That time is outside opening hours (Mon–Sat 9am–6pm). Try another time.")
                    return str(resp)

                ok, why = is_time_available(
                    dt, CALENDAR_ID, total_minutes, TIMEZONE, buffer_minutes=BUFFER_MINUTES
                )
                if not ok:
                    slots = next_available_slots(
                        dt + timedelta(minutes=SLOT_STEP_MINUTES),
                        CALENDAR_ID,
                        total_minutes,
                        TIMEZONE,
                        step_minutes=SLOT_STEP_MINUTES,
                        max_results=5,
                        buffer_minutes=BUFFER_MINUTES,
                    )
                    if slots:
                        pretty = "\n".join([f"• {s.strftime('%a %d %b %H:%M')}" for s in slots])
                        msg.body(f"That slot is taken ({why}). Next available:\n{pretty}\n\nReply with one of these times.")
                    else:
                        msg.body("That slot is taken. Please try another day/time.")
                    return str(resp)

                # book it
                customer_name = "Customer"
                phone = from_.replace("whatsapp:", "")
                res = create_booking_event(
                    CALENDAR_ID,
                    services[0] if len(services) == 1 else " + ".join(services),
                    customer_name,
                    dt,
                    total_minutes,
                    phone=phone,
                    timezone=TIMEZONE,
                )
                msg.body(f"✅ Booked: {', '.join(services)}\n📅 {dt.strftime('%a %d %b %H:%M')}")
                user_state[from_] = {"step": "START"}
                return str(resp)

    # --------------- STATE MACHINE (menu flow) ---------------
    st = user_state.get(from_, {"step": "START"})
    step = st.get("step", "START")

    if body.lower() == "back":
        user_state[from_] = {"step": "START"}
        msg.body(menu_text())
        return str(resp)

    if step == "AWAIT_TIME":
        services = st.get("services", [])
        dt = parse_dt(body)
        if not dt:
            msg.body(
                "I didn’t understand the time.\nTry:\n"
                "• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\n"
                "Reply BACK to change service."
            )
            return str(resp)

        total_minutes = services_total_minutes(services)
        if not within_hours(dt, total_minutes):
            msg.body("That time is outside opening hours (Mon–Sat 9am–6pm). Try another time.")
            return str(resp)

        ok, why = is_time_available(
            dt, CALENDAR_ID, total_minutes, TIMEZONE, buffer_minutes=BUFFER_MINUTES
        )
        if not ok:
            slots = next_available_slots(
                dt + timedelta(minutes=SLOT_STEP_MINUTES),
                CALENDAR_ID,
                total_minutes,
                TIMEZONE,
                step_minutes=SLOT_STEP_MINUTES,
                max_results=5,
                buffer_minutes=BUFFER_MINUTES,
            )
            if slots:
                pretty = "\n".join([f"• {s.strftime('%a %d %b %H:%M')}" for s in slots])
                msg.body(f"That slot is taken ({why}). Next available:\n{pretty}\n\nReply with one of these times.")
            else:
                msg.body("That slot is taken. Please try another day/time.")
            return str(resp)

        customer_name = "Customer"
        phone = from_.replace("whatsapp:", "")
        create_booking_event(
            CALENDAR_ID,
            services[0] if len(services) == 1 else " + ".join(services),
            customer_name,
            dt,
            total_minutes,
            phone=phone,
            timezone=TIMEZONE,
        )
        msg.body(f"✅ Booked: {', '.join(services)}\n📅 {dt.strftime('%a %d %b %H:%M')}")
        user_state[from_] = {"step": "START"}
        return str(resp)

    # START step: expect service selection
    # allow numbers 1-10 or names
    if step == "START":
        choice = body.lower()

        num_map = {
            "1": "Haircut",
            "2": "Skin Fade",
            "3": "Shape Up",
            "4": "Beard Trim",
            "5": "Hot Towel Shave",
            "6": "Kids Cut",
            "7": "Eyebrow Trim",
            "8": "Nose Wax",
            "9": "Ear Wax",
            "10": "Blow Dry",
        }

        picked = None
        if choice in num_map:
            picked = num_map[choice]
        else:
            picked_list = resolve_services([choice])
            if picked_list:
                picked = picked_list[0]

        if not picked:
            msg.body(menu_text())
            return str(resp)

        user_state[from_] = {"step": "AWAIT_TIME", "services": [picked]}
        msg.body(
            f"✍️ {picked}\n\n"
            "What day & time?\nExamples:\n"
            "• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\n"
            "Reply BACK to change service."
        )
        return str(resp)

    # fallback
    msg.body(menu_text())
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
