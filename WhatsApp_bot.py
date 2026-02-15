# WhatsApp_bot.py
import os
import re
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
    delete_booking_event,
)

load_dotenv()

app = Flask(__name__)

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

# (Name, Price, DurationMinutes)
SERVICES = [
    ("Haircut", 18, 45),
    ("Skin Fade", 22, 60),
    ("Shape Up", 12, 30),
    ("Beard Trim", 10, 15),
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
    "lineup": "Shape Up",
    "beard": "Beard Trim",
    "beard trim": "Beard Trim",
    "hot towel": "Hot Towel Shave",
    "shave": "Hot Towel Shave",
    "kids": "Kids Cut",
    "kids cut": "Kids Cut",
    "eyebrow": "Eyebrow Trim",
    "brow": "Eyebrow Trim",
    "nose": "Nose Wax",
    "ear": "Ear Wax",
    "blow": "Blow Dry",
    "blow dry": "Blow Dry",
}

# Simple session memory (in-code state)
user_state = {}  # { from_id: {"step": str, "service": tuple|None} }


# ---------------- HELPERS ----------------

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def menu_text() -> str:
    lines = [
        f"💈 *{SHOP_NAME}*",
        "Reply with *number or name:*",
        "",
        "*Men’s Cuts*",
        "1) Haircut — £18",
        "2) Skin Fade — £22",
        "3) Shape Up — £12",
        "",
        "*Beard / Shaves*",
        "4) Beard Trim — £10",
        "5) Hot Towel Shave — £15",
        "",
        "*Kids*",
        "6) Kids Cut — £15",
        "",
        "*Grooming*",
        "7) Eyebrow Trim — £6",
        "8) Nose Wax — £8",
        "9) Ear Wax — £8",
        "10) Blow Dry — £10",
        "",
        "Hours: Mon–Sat 9am–6pm | Sun Closed",
        "",
        'Tip: you can also type: "book haircut tomorrow 3pm"',
    ]
    return "\n".join(lines)


def get_service_by_number(n: int):
    if 1 <= n <= len(SERVICES):
        return SERVICES[n - 1]
    return None


def get_service_by_name(text: str):
    t = text.lower().strip()
    # direct match to canonical service names
    for name, price, dur in SERVICES:
        if name.lower() == t:
            return (name, price, dur)

    # alias contains match (prefer longer aliases)
    for alias in sorted(SERVICE_ALIASES.keys(), key=len, reverse=True):
        if alias in t:
            canonical = SERVICE_ALIASES[alias]
            for name, price, dur in SERVICES:
                if name == canonical:
                    return (name, price, dur)

    # partial name contains
    for name, price, dur in SERVICES:
        if name.lower() in t:
            return (name, price, dur)

    return None


def round_up_to_step(dt: datetime, step_minutes: int) -> datetime:
    dt = dt.astimezone(TZ)
    seconds = step_minutes * 60
    epoch = int(dt.timestamp())
    rounded = ((epoch + seconds - 1) // seconds) * seconds
    return datetime.fromtimestamp(rounded, TZ)


def is_open_day(dt: datetime) -> bool:
    return dt.strftime("%a").lower()[:3] in OPEN_DAYS


def is_within_hours(start_dt: datetime, end_dt: datetime) -> bool:
    start_dt = start_dt.astimezone(TZ)
    end_dt = end_dt.astimezone(TZ)

    if not is_open_day(start_dt):
        return False

    # Must start after/open and end before/close
    start_t = start_dt.time()
    end_t = end_dt.time()

    if start_t < OPEN_TIME:
        return False
    if end_t > CLOSE_TIME:
        return False
    return True


def parse_datetime_from_message(message: str):
    dt = dateparser.parse(
        message,
        settings={
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": TIMEZONE,
            "RETURN_AS_TIMEZONE_AWARE": True,
        },
    )
    if not dt:
        return None
    return dt.astimezone(TZ)


def format_dt(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    return dt.strftime("%a %d %b at %I:%M%p").replace(" 0", " ")


def safe_next_slots(start_dt: datetime, duration_minutes: int, limit: int = 5):
    """
    calendar_helper.next_available_slots() signature may vary.
    We try a couple of common forms and return a list (datetimes or strings).
    """
    try:
        return next_available_slots(start_dt, duration_minutes, limit)
    except TypeError:
        try:
            return next_available_slots(start_dt, duration_minutes)
        except TypeError:
            return next_available_slots(start_dt)


def slots_to_lines(slots):
    lines = []
    for s in (slots or []):
        if isinstance(s, datetime):
            lines.append(f"• {format_dt(s)}")
        else:
            lines.append(f"• {str(s)}")
    return lines


def detect_service_and_datetime(message: str):
    service = get_service_by_name(message)
    dt = parse_datetime_from_message(message)
    if service and dt:
        return service, dt
    return None, None


def get_user_id():
    # Twilio WhatsApp From looks like: "whatsapp:+447..."
    return request.values.get("From", "").strip() or "unknown"


def reset_state(uid: str):
    user_state.pop(uid, None)


# ---------------- ROUTES ----------------

@app.get("/")
def health():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp():
    incoming = normalize(request.values.get("Body", ""))
    uid = get_user_id()

    resp = MessagingResponse()
    msg = resp.message()

    if not incoming:
        msg.body(menu_text())
        return str(resp)

    text = incoming.lower()

    # quick commands
    if text in {"menu", "help", "start"}:
        reset_state(uid)
        msg.body(menu_text())
        return str(resp)

    if text in {"cancel", "cancel booking"}:
        reset_state(uid)
        msg.body("Reply with the *date/time* you want to cancel (e.g. “tomorrow 3pm”).")
        user_state[uid] = {"step": "cancel_datetime", "service": None}
        return str(resp)

    # 1) ✅ Natural message: "book haircut tomorrow 3pm"
    service, dt = detect_service_and_datetime(incoming)
    if service and dt:
        name, price, duration = service
        start = round_up_to_step(dt, SLOT_STEP_MINUTES)
        end = start + timedelta(minutes=duration)

        if not is_within_hours(start, end):
            msg.body(
                f"⏰ We’re open Mon–Sat 9am–6pm.\n"
                f"Pick a time within hours (e.g. “tomorrow 2pm”)."
            )
            return str(resp)

        if is_time_available(start, end):
            create_booking_event(CALENDAR_ID, name, start, end, uid)
            reset_state(uid)
            msg.body(f"✅ *{name}* booked for *{format_dt(start)}*.")
            return str(resp)

        slots = safe_next_slots(start, duration, limit=5)
        lines = slots_to_lines(slots)
        if not lines:
            msg.body("❌ That time isn’t available. Try another time (e.g. “tomorrow 4pm”).")
            return str(resp)

        msg.body(
            "❌ That time isn’t available.\n"
            "Next free slots:\n" + "\n".join(lines) +
            "\n\nReply with one of the times above (or type another time)."
        )
        # keep service in state so they can just reply with time
        user_state[uid] = {"step": "await_datetime", "service": service}
        return str(resp)

    # 2) Continue stateful flow
    state = user_state.get(uid, {"step": None, "service": None})
    step = state.get("step")
    selected_service = state.get("service")

    # Cancel flow: user gives datetime to cancel
    if step == "cancel_datetime":
        dt2 = parse_datetime_from_message(incoming)
        if not dt2:
            msg.body("I couldn’t understand the time. Try: “tomorrow 3pm” or “Sat 11am”.")
            return str(resp)

        # You likely have delete_booking_event implemented to find by time/uid,
        # but signatures vary. Here we try the simplest: delete by uid + start time.
        try:
            ok = delete_booking_event(CALENDAR_ID, dt2, uid)
        except TypeError:
            try:
                ok = delete_booking_event(CALENDAR_ID, dt2)
            except TypeError:
                ok = delete_booking_event(CALENDAR_ID)

        reset_state(uid)
        if ok:
            msg.body(f"✅ Booking cancelled for *{format_dt(dt2)}*.")
        else:
            msg.body("⚠️ I couldn’t find that booking to cancel. If you want, send the exact time again.")
        return str(resp)

    # If we have a service saved and user sends only time/date
    if step == "await_datetime" and selected_service:
        dt3 = parse_datetime_from_message(incoming)
        if not dt3:
            msg.body("Send a date/time like: “tomorrow 3pm” or “Sat 11:30am”.")
            return str(resp)

        name, price, duration = selected_service
        start = round_up_to_step(dt3, SLOT_STEP_MINUTES)
        end = start + timedelta(minutes=duration)

        if not is_within_hours(start, end):
            msg.body("⏰ We’re open Mon–Sat 9am–6pm. Try a time within hours.")
            return str(resp)

        if is_time_available(start, end):
            create_booking_event(CALENDAR_ID, name, start, end, uid)
            reset_state(uid)
            msg.body(f"✅ *{name}* booked for *{format_dt(start)}*.")
            return str(resp)

        slots = safe_next_slots(start, duration, limit=5)
        lines = slots_to_lines(slots)
        msg.body(
            "❌ Not available.\nNext free slots:\n" +
            ("\n".join(lines) if lines else "Try another time.") +
            "\n\nReply with a time."
        )
        return str(resp)

    # 3) Service selection by number or name
    m = re.fullmatch(r"\s*(\d{1,2})\s*", incoming)
    if m:
        svc = get_service_by_number(int(m.group(1)))
        if not svc:
            msg.body("That number isn’t on the menu. Reply with 1–10, or type the service name.")
            return str(resp)

        user_state[uid] = {"step": "await_datetime", "service": svc}
        name, price, dur = svc
        msg.body(f"✅ *{name}* selected.\nNow send a date/time (e.g. “tomorrow 3pm”).")
        return str(resp)

    svc2 = get_service_by_name(incoming)
    if svc2:
        user_state[uid] = {"step": "await_datetime", "service": svc2}
        name, price, dur = svc2
        msg.body(f"✅ *{name}* selected.\nNow send a date/time (e.g. “tomorrow 3pm”).")
        return str(resp)

    # 4) If they typed "book" without details, show menu
    if "book" in text:
        reset_state(uid)
        msg.body(menu_text())
        return str(resp)

    # Default fallback
    msg.body(
        "I didn’t catch that.\n\n" +
        menu_text()
    )
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
