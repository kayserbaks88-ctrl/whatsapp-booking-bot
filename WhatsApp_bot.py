import os
import re
import json
from datetime import datetime, timedelta
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
    list_bookings_for_phone,
    cancel_booking_by_index,
    reschedule_booking_by_index,
)

load_dotenv()

# =========================
# ENV
# =========================
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Trimtech AI").strip()
TIMEZONE_HINT = os.getenv("TIMEZONE_HINT", "Europe/London").strip()
TZ = ZoneInfo(TIMEZONE_HINT)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20").strip())

# If you don't want the long Google event link sent after booking:
SEND_CALENDAR_LINK = os.getenv("SEND_CALENDAR_LINK", "0").strip() == "1"

# Opening hours: Mon-Sat 09:00–18:00, Sun closed
OPEN_HOUR = 9
CLOSE_HOUR = 18  # 18:00 is last end boundary (slots must start < 18:00)

# How long each service takes (minutes)
DEFAULT_DURATION_MIN = int(os.getenv("DEFAULT_DURATION_MIN", "30").strip())


# =========================
# MENU (the full list you showed)
# Edit prices/names here only.
# =========================
SERVICES = [
    # Men's cuts
    {"key": 1, "name": "Haircut", "price": 18, "duration": 30, "aliases": ["hair cut", "mens haircut", "men haircut"]},
    {"key": 2, "name": "Skin Fade", "price": 22, "duration": 30, "aliases": ["fade", "skin", "skin-fade"]},
    {"key": 3, "name": "Shape Up", "price": 12, "duration": 15, "aliases": ["shapeup", "line up", "lineup", "shape"]},
    # Beard
    {"key": 4, "name": "Beard Trim", "price": 10, "duration": 15, "aliases": ["beard", "trim", "beard trimming"]},
    # Other
    {"key": 5, "name": "Hot Towel Shave", "price": 25, "duration": 30, "aliases": ["hot towel", "shave", "hot towel shaves"]},
    {"key": 6, "name": "Blow Dry", "price": 20, "duration": 30, "aliases": ["blowdry", "blow"]},
    {"key": 7, "name": "Boy's Cut", "price": 15, "duration": 30, "aliases": ["boys cut", "boy cut", "kids boy"]},
    {"key": 8, "name": "Children's Cut", "price": 15, "duration": 30, "aliases": ["childrens cut", "children cut", "kids cut", "kids haircut"]},
    {"key": 9, "name": "Ear Waxing", "price": 8, "duration": 15, "aliases": ["ear wax", "ears waxing"]},
    {"key": 10, "name": "Eyebrow Trim", "price": 8, "duration": 15, "aliases": ["eyebrow", "brows", "brow trim"]},
    {"key": 11, "name": "Nose Waxing", "price": 8, "duration": 15, "aliases": ["nose wax", "nose"]},
    {"key": 12, "name": "Male Grooming", "price": 20, "duration": 30, "aliases": ["grooming", "male grooming"]},
    {"key": 13, "name": "Wedding Package", "price": 0, "duration": 60, "aliases": ["wedding"]},
]

SERVICE_BY_KEY = {s["key"]: s for s in SERVICES}


def menu_text() -> str:
    lines = [f"💈 *{BUSINESS_NAME}*\nWelcome! Reply with a number or name:\n"]
    for s in SERVICES:
        price = f"£{s['price']}" if s["price"] else "Ask"
        lines.append(f"{s['key']}) {s['name']} — {price}")
    lines.append("")
    lines.append(f"Hours: Mon–Sat {OPEN_HOUR}am–{CLOSE_HOUR}pm | Sun Closed")
    lines.append("")
    lines.append("Commands: MENU | MY BOOKINGS | CANCEL | RESCHEDULE")
    lines.append("")
    lines.append("Tip: you can type a full sentence like:")
    lines.append("• Book a skin fade tomorrow at 2pm")
    lines.append("• Can I get a haircut Friday at 2pm?")
    lines.append("")
    lines.append("Type MENU anytime to see this again.")
    return "\n".join(lines)


# =========================
# STATE (in-memory). For production, swap to Redis.
# =========================
STATE = {}
# STATE[from_number] = {
#   "step": "idle" | "await_service" | "await_time" | "offer_slots" | "await_cancel_pick" | "await_resched_pick" | "await_resched_time",
#   "service_key": int|None,
#   "pending_dt": datetime|None,
#   "offered_slots": [datetime, ...],
#   "bookings_cache": [ {event_id, summary, start_dt}, ... ]
# }

def get_state(num: str) -> dict:
    st = STATE.get(num)
    if not st:
        st = {"step": "idle", "service_key": None, "pending_dt": None, "offered_slots": [], "bookings_cache": []}
        STATE[num] = st
    return st


# =========================
# Parsing helpers
# =========================
THANKS_RE = re.compile(r"\b(thanks|thank you|thx|cheers|ty|please)\b", re.I)

def normalize_text(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t

def parse_datetime_from_text(text: str) -> datetime | None:
    """Parse a datetime in the BUSINESS timezone. Prefer future dates."""
    settings = {
        "TIMEZONE": TIMEZONE_HINT,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.now(TZ),
    }
    dt = dateparser.parse(text, settings=settings)
    if not dt:
        return None
    # Ensure tz-aware in TZ
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    else:
        dt = dt.astimezone(TZ)
    return dt

def within_opening_hours(dt: datetime) -> bool:
    """Mon-Sat 09:00 <= start < 18:00, Sun closed."""
    dt = dt.astimezone(TZ)
    # Monday=0 ... Sunday=6
    if dt.weekday() == 6:
        return False
    if dt.hour < OPEN_HOUR:
        return False
    if dt.hour > CLOSE_HOUR:
        return False
    # If exactly 18:xx => not allowed
    if dt.hour == CLOSE_HOUR and dt.minute > 0:
        return False
    # If exactly 18:00 start is not allowed (closing time). Require < 18:00
    if dt.hour == CLOSE_HOUR and dt.minute == 0:
        return False
    return True

def pick_service_rule_based(text: str) -> dict | None:
    """Try to match service by number or keywords/aliases."""
    t = normalize_text(text).lower()

    # number selection (e.g. "2")
    if t.isdigit():
        k = int(t)
        return SERVICE_BY_KEY.get(k)

    # exact name match / contains
    for s in SERVICES:
        if s["name"].lower() == t:
            return s

    # contains name
    for s in SERVICES:
        if s["name"].lower() in t:
            return s

    # aliases
    for s in SERVICES:
        for a in s.get("aliases", []):
            if a.lower() in t:
                return s

    return None


# =========================
# LLM fallback (ONLY if rule-based fails)
# =========================
LLM_SYSTEM = f"""You extract booking intent for a barbershop.

Return JSON only with keys:
- service: string or null (must be one of the provided services or null)
- datetime_text: string or null (the part of the message that indicates day/time)

If the message is not a booking request, set both null.
Do NOT invent.
"""

def llm_extract(text: str) -> dict:
    """Returns {"service": str|None, "datetime_text": str|None}."""
    if not OPENAI_API_KEY:
        return {"service": None, "datetime_text": None}

    # Provide the service list to constrain output
    service_names = [s["name"] for s in SERVICES]
    user_prompt = f"""Message: {text}

Allowed services: {service_names}
Timezone: {TIMEZONE_HINT}
"""

    try:
        # Using Responses API via HTTP (no SDK dependency)
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "input": [
                    {"role": "system", "content": LLM_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                "max_output_tokens": 120,
            },
            timeout=OPENAI_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()

        # Responses API returns output in a structured form; pull text safely
        out_text = ""
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    out_text += c.get("text", "")
        out_text = out_text.strip()

        # Must be JSON
        obj = json.loads(out_text)

        service = obj.get("service")
        datetime_text = obj.get("datetime_text")

        if service is not None and service not in service_names:
            service = None
        if isinstance(datetime_text, str) and not datetime_text.strip():
            datetime_text = None

        return {"service": service, "datetime_text": datetime_text}
    except Exception:
        return {"service": None, "datetime_text": None}


# =========================
# Conversation actions
# =========================
def friendly_ack_if_needed(text: str) -> str | None:
    if THANKS_RE.search(text or ""):
        return "😊 You're welcome! Type *MENU* to book, or tell me what you’d like (e.g. *Book haircut tomorrow 2pm*)."
    return None

def prompt_for_time(service: dict) -> str:
    return (
        f"✍️ *{service['name']}*\n\n"
        "What day & time?\nExamples:\n"
        "• Tomorrow 2pm\n"
        "• Fri 2pm\n"
        "• 10/02 15:30\n\n"
        "Reply *BACK* to change service."
    )

def format_slot_list(slots: list[datetime]) -> str:
    lines = ["❌ That time is taken. Next available:"]
    for i, dt in enumerate(slots[:3], start=1):
        lines.append(f"{i}) {dt.strftime('%a %d %b %H:%M')}")
    lines.append("")
    lines.append("Reply with *1/2/3* or the exact time (e.g. *09:15* or *Tomorrow 9am*).")
    lines.append("Reply *BACK* to change service.")
    return "\n".join(lines)

def booked_message(service: dict, dt: datetime) -> str:
    when = dt.strftime("%a %d %b %H:%M")
    extra = "Anything else? Type *MENU* to book another, or *MY BOOKINGS* to view."
    if SEND_CALENDAR_LINK:
        # calendar_helper returns a link; we add it if enabled
        return f"✅ Booked: *{service['name']}*\n🗓️ {when}\n\n{extra}"
    else:
        return f"✅ Booked: *{service['name']}*\n🗓️ {when}\n\n{extra}"


# =========================
# Flask app
# =========================
app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "ok", 200

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    from_number = request.form.get("From", "")
    body = normalize_text(request.form.get("Body", ""))

    resp = MessagingResponse()
    msg = resp.message()

    st = get_state(from_number)

    # Quick thanks/please acknowledgement
    ack = friendly_ack_if_needed(body)
    if ack and st["step"] == "idle":
        msg.body(ack)
        return str(resp)

    # Commands
    upper = body.upper()
    if upper in ("MENU", "HELP", "START"):
        st.update({"step": "await_service", "service_key": None, "pending_dt": None, "offered_slots": []})
        msg.body(menu_text())
        return str(resp)

    if upper in ("BACK",):
        st.update({"step": "await_service", "service_key": None, "pending_dt": None, "offered_slots": []})
        msg.body(menu_text())
        return str(resp)

    if upper in ("MY BOOKINGS", "MYBOOKINGS", "VIEW"):
        bookings = list_bookings_for_phone(from_number)
        if not bookings:
            msg.body("You have no upcoming bookings. Type *MENU* to book.")
            st["step"] = "idle"
            return str(resp)
        lines = ["📋 *Your bookings:*"]
        for i, b in enumerate(bookings, start=1):
            lines.append(f"{i}) {b['summary']} — {b['start'].astimezone(TZ).strftime('%a %d %b %H:%M')}")
        lines.append("")
        lines.append("To cancel: reply *CANCEL*")
        lines.append("To reschedule: reply *RESCHEDULE*")
        msg.body("\n".join(lines))
        st["bookings_cache"] = bookings
        st["step"] = "idle"
        return str(resp)

    if upper == "CANCEL":
        bookings = list_bookings_for_phone(from_number)
        if not bookings:
            msg.body("No upcoming bookings to cancel. Type *MENU* to book.")
            st["step"] = "idle"
            return str(resp)
        st["bookings_cache"] = bookings
        st["step"] = "await_cancel_pick"
        lines = ["Reply with the number to cancel:"]
        for i, b in enumerate(bookings, start=1):
            lines.append(f"{i}) {b['summary']} — {b['start'].astimezone(TZ).strftime('%a %d %b %H:%M')}")
        msg.body("\n".join(lines))
        return str(resp)

    if upper == "RESCHEDULE":
        bookings = list_bookings_for_phone(from_number)
        if not bookings:
            msg.body("No upcoming bookings to reschedule. Type *MENU* to book.")
            st["step"] = "idle"
            return str(resp)
        st["bookings_cache"] = bookings
        st["step"] = "await_resched_pick"
        lines = ["Reply with the number to reschedule:"]
        for i, b in enumerate(bookings, start=1):
            lines.append(f"{i}) {b['summary']} — {b['start'].astimezone(TZ).strftime('%a %d %b %H:%M')}")
        msg.body("\n".join(lines))
        return str(resp)

    # -------------------------
    # RESCHEDULE flow
    # -------------------------
    if st["step"] == "await_resched_pick":
        if body.isdigit():
            idx = int(body)
            if 1 <= idx <= len(st["bookings_cache"]):
                st["resched_index"] = idx
                st["step"] = "await_resched_time"
                msg.body("What new day & time? (e.g. *Tomorrow 2pm*)\nReply *BACK* to stop.")
                return str(resp)
        msg.body("Please reply with a valid number from your bookings list, or type *BACK*.")
        return str(resp)

    if st["step"] == "await_resched_time":
        dt = parse_datetime_from_text(body)
        if not dt:
            msg.body("I didn’t understand the time. Try: *Tomorrow 2pm* or *Fri 15:30*.\nReply *BACK* to stop.")
            return str(resp)
        if not within_opening_hours(dt):
            msg.body(f"That time isn’t within opening hours (Mon–Sat {OPEN_HOUR}–{CLOSE_HOUR}). Try another time.")
            return str(resp)

        # Determine duration from the booking summary (best-effort)
        idx = st.get("resched_index")
        try:
            ok, info = reschedule_booking_by_index(from_number, idx, dt)
            st["step"] = "idle"
            if ok:
                msg.body(f"✅ Rescheduled.\n🗓️ {dt.strftime('%a %d %b %H:%M')}\n\nType *MENU* for more.")
            else:
                msg.body(f"❌ Couldn’t reschedule: {info}\nType *MENU* to try again.")
            return str(resp)
        except Exception as e:
            st["step"] = "idle"
            msg.body("❌ Something went wrong rescheduling. Type *RESCHEDULE* to try again.")
            return str(resp)

    # -------------------------
    # CANCEL flow
    # -------------------------
    if st["step"] == "await_cancel_pick":
        if body.isdigit():
            idx = int(body)
            if 1 <= idx <= len(st["bookings_cache"]):
                ok, info = cancel_booking_by_index(from_number, idx)
                st["step"] = "idle"
                if ok:
                    msg.body("✅ Cancelled. Type *MENU* to book again.")
                else:
                    msg.body(f"❌ Couldn’t cancel: {info}\nType *CANCEL* to try again.")
                return str(resp)
        msg.body("Please reply with a valid number from the list, or type *BACK*.")
        return str(resp)

    # -------------------------
    # Main booking flow
    # -------------------------
    # 1) If idle and they say "hi" -> show menu
    if st["step"] == "idle":
        if body.lower() in ("hi", "hello", "hey"):
            st["step"] = "await_service"
            msg.body(menu_text())
            return str(resp)

        # Try rule-based quick booking in one message
        svc = pick_service_rule_based(body)
        dt = parse_datetime_from_text(body)

        # If rule-based didn't find service, try LLM extraction once
        if not svc:
            ex = llm_extract(body)
            if ex.get("service"):
                svc = next((s for s in SERVICES if s["name"] == ex["service"]), None)
            if not dt and ex.get("datetime_text"):
                dt = parse_datetime_from_text(ex["datetime_text"])

        if svc and dt:
            # proceed to booking attempt
            return _attempt_booking(resp, msg, st, from_number, svc, dt)

        # If only service found
        if svc and not dt:
            st["step"] = "await_time"
            st["service_key"] = svc["key"]
            msg.body(prompt_for_time(svc))
            return str(resp)

        # If only datetime found -> ask for service
        if dt and not svc:
            st["step"] = "await_service"
            st["pending_dt"] = dt
            msg.body("Got the time ✅\nNow reply with the service number/name.\n\n" + menu_text())
            return str(resp)

        # Otherwise ask menu
        st["step"] = "await_service"
        msg.body(menu_text())
        return str(resp)

    # 2) Await service
    if st["step"] == "await_service":
        svc = pick_service_rule_based(body)
        if not svc:
            # LLM fallback to find service only
            ex = llm_extract(body)
            if ex.get("service"):
                svc = next((s for s in SERVICES if s["name"] == ex["service"]), None)

        if not svc:
            msg.body("I didn’t catch that service.\nReply with a number (e.g. *1*) or type the name.\n\nType *MENU* to see the list again.")
            return str(resp)

        st["service_key"] = svc["key"]

        # If we already captured a pending time earlier, try booking now
        if st.get("pending_dt"):
            dt = st["pending_dt"]
            st["pending_dt"] = None
            return _attempt_booking(resp, msg, st, from_number, svc, dt)

        st["step"] = "await_time"
        msg.body(prompt_for_time(svc))
        return str(resp)

    # 3) Await time
    if st["step"] == "await_time":
        svc = SERVICE_BY_KEY.get(st.get("service_key"))
        if not svc:
            st["step"] = "await_service"
            msg.body(menu_text())
            return str(resp)

        dt = parse_datetime_from_text(body)
        if not dt:
            msg.body("I didn’t understand the time.\nTry:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply *BACK* to change service.")
            return str(resp)

        return _attempt_booking(resp, msg, st, from_number, svc, dt)

    # 4) Offered slots selection
    if st["step"] == "offer_slots":
        svc = SERVICE_BY_KEY.get(st.get("service_key"))
        if not svc:
            st["step"] = "await_service"
            msg.body(menu_text())
            return str(resp)

        offered = st.get("offered_slots") or []
        chosen_dt = None

        # Pick by 1/2/3
        if body.isdigit():
            i = int(body)
            if 1 <= i <= len(offered):
                chosen_dt = offered[i - 1]

        # Or parse new time
        if not chosen_dt:
            dt2 = parse_datetime_from_text(body)
            if dt2:
                chosen_dt = dt2

        if not chosen_dt:
            msg.body("Reply with *1/2/3* or a time like *Tomorrow 9am*.\nReply *BACK* to change service.")
            return str(resp)

        return _attempt_booking(resp, msg, st, from_number, svc, chosen_dt)

    # Fallback
    st["step"] = "await_service"
    msg.body(menu_text())
    return str(resp)


def _attempt_booking(resp, msg, st, from_number, svc, dt: datetime):
    dt = dt.astimezone(TZ)

    # Opening hours
    if not within_opening_hours(dt):
        msg.body(f"That time isn’t within opening hours (Mon–Sat {OPEN_HOUR}–{CLOSE_HOUR}). Try another time.")
        return str(resp)

    duration = int(svc.get("duration") or DEFAULT_DURATION_MIN)

    # Availability + booking
    if not is_time_available(dt, duration_min=duration):
        slots = next_available_slots(dt, duration_min=duration, count=3)
        st["step"] = "offer_slots"
        st["service_key"] = svc["key"]
        st["offered_slots"] = slots
        msg.body(format_slot_list(slots))
        return str(resp)

    # Create booking
    created = create_booking_event(
        start_dt=dt,
        duration_min=duration,
        service_name=svc["name"],
        phone=from_number,
        price=svc.get("price"),
    )

    # Reset state
    st.update({"step": "idle", "service_key": None, "pending_dt": None, "offered_slots": []})

    if SEND_CALENDAR_LINK and created.get("htmlLink"):
        msg.body(
            f"✅ Booked: *{svc['name']}*\n🗓️ {dt.strftime('%a %d %b %H:%M')}\n\n"
            f"🔗 Calendar link: {created['htmlLink']}\n\n"
            "Anything else? Type *MENU* to book another, or *MY BOOKINGS* to view."
        )
    else:
        msg.body(booked_message(svc, dt))

    return str(resp)


if __name__ == "__main__":
    PORT = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=PORT)