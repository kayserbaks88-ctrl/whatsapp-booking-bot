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
    update_booking_time,
    list_user_bookings,
)

load_dotenv()

# ---------------- CONFIG ----------------
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))
SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

# LLM optional (safe off)
ENABLE_LLM = os.getenv("ENABLE_LLM", "0") == "1"

# ---------------- MENU (edit anytime) ----------------
# name, price, minutes, category, aliases
SERVICES = [
    ("Haircut", 18, 45, "Men’s Cuts", ["haircut", "hair cut", "cut"]),
    ("Skin Fade", 22, 60, "Men’s Cuts", ["skin fade", "fade", "skinfade"]),
    ("Shape Up", 12, 20, "Men’s Cuts", ["shape up", "shapeup", "line up", "lineup"]),
    ("Beard Trim", 10, 20, "Beard Trimming", ["beard", "beard trim"]),
    ("Hot Towel Shave", 25, 40, "Hot Towel Shaves", ["hot towel", "hot towel shave", "shave"]),
    ("Blow Dry", 20, 30, "Blow Dry", ["blow dry", "blowdry"]),
    ("Boy's Cut", 15, 40, "Boy’s Cuts", ["boys cut", "boy cut", "boys haircut"]),
    ("Children’s Cut", 15, 40, "Children’s Cuts", ["childrens cut", "children cut", "kids cut", "kid cut"]),
    ("Ear Waxing", 8, 10, "Waxing", ["ear wax", "ear waxing"]),
    ("Nose Waxing", 8, 10, "Waxing", ["nose wax", "nose waxing"]),
    ("Eyebrow Trim", 6, 10, "Grooming", ["eyebrow", "brows", "eyebrow trim"]),
    ("Wedding Package", 80, 90, "Packages", ["wedding", "wedding package"]),
]
SERVICE_BY_INDEX = {str(i + 1): s for i, s in enumerate(SERVICES)}

# ---------------- STATE ----------------
STATE = {}
# state schema:
# {
#   "step": "idle" | "await_time" | "offer_slots" | "cancel_pick" | "resched_pick" | "resched_time",
#   "service": svc_tuple or None,
#   "offered_slots": [dt,dt,dt] or None,
#   "pending_bookings": [ {id,when,service,htmlLink}, ... ] or None,
#   "resched_event": {id, service, minutes} or None
# }

def st(from_number: str):
    if from_number not in STATE:
        STATE[from_number] = {
            "step": "idle",
            "service": None,
            "offered_slots": None,
            "pending_bookings": None,
            "resched_event": None,
        }
    return STATE[from_number]

def reset(from_number: str):
    STATE[from_number] = {
        "step": "idle",
        "service": None,
        "offered_slots": None,
        "pending_bookings": None,
        "resched_event": None,
    }

# ---------------- HELPERS ----------------
def normalize(s: str) -> str:
    return (s or "").strip()

def is_greeting(msg: str) -> bool:
    m = msg.lower().strip()
    return m in {"hi", "hello", "hey", "hiya", "yo", "good morning", "good afternoon", "good evening"}

def is_thanks(msg: str) -> bool:
    m = msg.lower().strip()
    return any(x in m for x in ["thank", "thanks", "thx", "appreciate", "nice one"])

def menu_text() -> str:
    cats = {}
    for idx, (name, price, mins, cat, aliases) in enumerate(SERVICES, start=1):
        cats.setdefault(cat, []).append((idx, name, price))

    lines = [
        f"💈 *{SHOP_NAME}*",
        "Welcome! Reply with a *number* or *name*:\n",
    ]
    for cat, items in cats.items():
        lines.append(f"*{cat}*")
        for idx, name, price in items:
            lines.append(f"{idx}) {name} — £{price}")
        lines.append("")

    lines.append("Hours: *Mon–Sat 9am–6pm* | Sun Closed\n")
    lines.append("Tip: you can type a full sentence like:")
    lines.append("• Book a skin fade *tomorrow at 2pm*")
    lines.append("• Can I get a haircut *Friday at 2pm*?\n")
    lines.append("Commands: *MENU* | *MY BOOKINGS* | *CANCEL* | *RESCHEDULE*")
    return "\n".join(lines).strip()

def parse_service(text: str):
    t = text.lower().strip()
    if t in SERVICE_BY_INDEX:
        return SERVICE_BY_INDEX[t]
    for svc in SERVICES:
        name, price, mins, cat, aliases = svc
        for a in aliases:
            if re.search(rf"\b{re.escape(a)}\b", t):
                return svc
        if re.search(rf"\b{re.escape(name.lower())}\b", t):
            return svc
    return None

def cleanup_time_shorthand(text: str) -> str:
    t = text.strip()
    t = re.sub(r"\b(\d{1,2})\s*p\b", r"\1pm", t, flags=re.IGNORECASE)  # 2p -> 2pm
    t = re.sub(r"\b(\d{1,2})\s*a\b", r"\1am", t, flags=re.IGNORECASE)  # 2a -> 2am
    return t

def parse_dt(text: str):
    text = cleanup_time_shorthand(text)
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
    return dt.astimezone(TZ)

def within_opening(dt: datetime) -> bool:
    wd = dt.strftime("%a").lower()[:3]
    if wd not in OPEN_DAYS:
        return False
    if dt.time() < OPEN_TIME or dt.time() >= CLOSE_TIME:
        return False
    return True

def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%a %d %b %H:%M")

def ask_time_for_service(svc) -> str:
    return (
        f"✍🏽 *{svc[0]}*\n\n"
        "What day & time?\nExamples:\n"
        "• Tomorrow 2pm\n"
        "• Fri 2pm\n"
        "• 10/02 15:30\n\n"
        "Reply *BACK* to change service."
    )

def friendly_after_booking() -> str:
    return "Anything else? Type *MENU* to book another, or *MY BOOKINGS* to view."

# ---------------- APP ----------------
app = Flask(__name__)

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    resp = MessagingResponse()
    msg = resp.message()

    from_number = request.form.get("From", "")
    body = normalize(request.form.get("Body", ""))

    s = st(from_number)
    low = body.lower()

    # Global commands
    if low in {"menu", "start", "home"}:
        reset(from_number)
        msg.body(menu_text())
        return str(resp)

    if low in {"help", "commands"}:
        msg.body("Commands: *MENU* | *MY BOOKINGS* | *CANCEL* | *RESCHEDULE*")
        return str(resp)

    if is_greeting(body) and s["step"] == "idle":
        msg.body(menu_text())
        return str(resp)

    if is_thanks(body):
        msg.body("🙏 No problem! If you need anything else, type *MENU* or *MY BOOKINGS*.")
        return str(resp)

    # BACK handling
    if low == "back":
        reset(from_number)
        msg.body(menu_text())
        return str(resp)

    # MY BOOKINGS
    if low in {"my bookings", "my booking", "bookings", "view bookings"}:
        reset(from_number)
        try:
            bookings = list_user_bookings(CALENDAR_ID, from_number, limit=10)
        except Exception as e:
            msg.body(f"⚠️ Couldn’t load bookings. ({e})")
            return str(resp)

        if not bookings:
            msg.body("You don’t have any upcoming bookings. Type *MENU* to book.")
            return str(resp)

        lines = ["📅 *Your bookings:*"]
        for i, b in enumerate(bookings, start=1):
            lines.append(f"{i}) {b['service']} — {fmt_dt(b['when'])}")
        lines.append("\nType *CANCEL* or *RESCHEDULE* to manage one.")
        msg.body("\n".join(lines))
        return str(resp)

    # CANCEL flow
    if low == "cancel":
        try:
            bookings = list_user_bookings(CALENDAR_ID, from_number, limit=10)
        except Exception as e:
            msg.body(f"⚠️ Couldn’t load bookings. ({e})")
            return str(resp)

        if not bookings:
            msg.body("No upcoming bookings to cancel. Type *MENU* to book.")
            return str(resp)

        s["step"] = "cancel_pick"
        s["pending_bookings"] = bookings

        lines = ["❌ *Cancel which booking?* Reply with a number:"]
        for i, b in enumerate(bookings, start=1):
            lines.append(f"{i}) {b['service']} — {fmt_dt(b['when'])}")
        msg.body("\n".join(lines))
        return str(resp)

    if s["step"] == "cancel_pick":
        if body.isdigit():
            idx = int(body)
            bookings = s.get("pending_bookings") or []
            if 1 <= idx <= len(bookings):
                chosen = bookings[idx - 1]
                try:
                    delete_booking_event(CALENDAR_ID, chosen["id"])
                except Exception as e:
                    msg.body(f"⚠️ Couldn’t cancel. ({e})")
                    reset(from_number)
                    return str(resp)

                reset(from_number)
                msg.body(f"✅ Cancelled: *{chosen['service']}* — {fmt_dt(chosen['when'])}\n\nType *MENU* to book again.")
                return str(resp)

        msg.body("Reply with a valid number from the list, or type *MENU*.")
        return str(resp)

    # RESCHEDULE flow
    if low == "reschedule":
        try:
            bookings = list_user_bookings(CALENDAR_ID, from_number, limit=10)
        except Exception as e:
            msg.body(f"⚠️ Couldn’t load bookings. ({e})")
            return str(resp)

        if not bookings:
            msg.body("No upcoming bookings to reschedule. Type *MENU* to book.")
            return str(resp)

        s["step"] = "resched_pick"
        s["pending_bookings"] = bookings

        lines = ["🕒 *Reschedule which booking?* Reply with a number:"]
        for i, b in enumerate(bookings, start=1):
            lines.append(f"{i}) {b['service']} — {fmt_dt(b['when'])}")
        msg.body("\n".join(lines))
        return str(resp)

    if s["step"] == "resched_pick":
        if body.isdigit():
            idx = int(body)
            bookings = s.get("pending_bookings") or []
            if 1 <= idx <= len(bookings):
                chosen = bookings[idx - 1]
                # Find duration minutes from SERVICES by name (fallback 45)
                minutes = 45
                for svc in SERVICES:
                    if svc[0].lower() == chosen["service"].lower():
                        minutes = svc[2]
                        break
                s["resched_event"] = {"id": chosen["id"], "service": chosen["service"], "minutes": minutes}
                s["step"] = "resched_time"
                msg.body(
                    f"✍🏽 *{chosen['service']}*\nCurrent: {fmt_dt(chosen['when'])}\n\n"
                    "Send the *new day & time* (e.g. *Tomorrow 3pm*)."
                )
                return str(resp)

        msg.body("Reply with a valid number from the list, or type *MENU*.")
        return str(resp)

    if s["step"] == "resched_time":
        info = s.get("resched_event") or {}
        dt = parse_dt(body)
        if not dt:
            msg.body("I didn’t understand that time. Try like: *Tomorrow 3pm* or *Fri 2pm*.")
            return str(resp)

        if not within_opening(dt):
            msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
            return str(resp)

        minutes = int(info.get("minutes", 45))
        end_dt = dt + timedelta(minutes=minutes)
        if not is_time_available(CALENDAR_ID, dt, end_dt):
            offered = next_available_slots(CALENDAR_ID, minutes, dt + timedelta(minutes=SLOT_STEP_MINUTES), SLOT_STEP_MINUTES, limit=3)
            if not offered:
                msg.body("❌ That time is taken and I couldn’t find alternatives soon. Try another day/time.")
                return str(resp)

            s["step"] = "offer_slots"
            s["offered_slots"] = offered
            # Keep same service in state for booking if needed
            s["service"] = (info.get("service"), 0, minutes, "", [])

            lines = ["❌ That time is taken. Next available:"]
            for i, odt in enumerate(offered, start=1):
                lines.append(f"{i}) {fmt_dt(odt)}")
            lines.append("\nReply with *1/2/3* or type a new time (e.g. *Tomorrow 3pm*).")
            msg.body("\n".join(lines))
            return str(resp)

        try:
            updated = update_booking_time(CALENDAR_ID, info["id"], dt, minutes)
        except Exception as e:
            msg.body(f"⚠️ Couldn’t reschedule. ({e})")
            reset(from_number)
            return str(resp)

        reset(from_number)
        msg.body(
            f"✅ Rescheduled: *{updated.get('summary','Booking')}*\n"
            f"🗓 {fmt_dt(updated['start'])}\n"
            f"🔗 Calendar link: {updated.get('htmlLink','')}\n\n"
            f"{friendly_after_booking()}"
        )
        return str(resp)

    # If user is choosing from offered slots (either from booking or reschedule conflict)
    if s["step"] == "offer_slots":
        offered = s.get("offered_slots") or []
        svc = s.get("service")

        # Accept 1/2/3
        if body.strip() in {"1", "2", "3"}:
            idx = int(body.strip()) - 1
            if 0 <= idx < len(offered):
                dt = offered[idx]
            else:
                msg.body("Pick 1/2/3 or type a new time.")
                return str(resp)
        else:
            dt = parse_dt(body)
            if not dt:
                msg.body("Pick *1/2/3* or type a new time like *Tomorrow 3pm*.")
                return str(resp)

        if not within_opening(dt):
            msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
            return str(resp)

        minutes = int(svc[2]) if svc else 45
        end_dt = dt + timedelta(minutes=minutes)
        if not is_time_available(CALENDAR_ID, dt, end_dt):
            msg.body("Still taken. Try another time or type *MENU*.")
            return str(resp)

        # If we got here from RESCHEDULE conflict, we don't know event id anymore (state reset would have happened),
        # so we treat it as a fresh booking.
        try:
            created = create_booking_event(CALENDAR_ID, dt, minutes, svc[0] if svc else "Booking", from_number)
        except Exception as e:
            msg.body(f"⚠️ Couldn’t book. ({e})")
            reset(from_number)
            return str(resp)

        reset(from_number)
        msg.body(
            f"✅ Booked: *{svc[0] if svc else 'Booking'}*\n"
            f"🗓 {fmt_dt(created['start'])}\n"
            f"🔗 Calendar link: {created.get('htmlLink','')}\n\n"
            f"{friendly_after_booking()}"
        )
        return str(resp)

    # ---------------- MAIN BOOKING FLOW ----------------
    # If idle: try parse service OR parse sentence with service+time
    if s["step"] == "idle":
        # If message contains a service
        svc = parse_service(body)

        # If it contains both service + time, try straight booking
        if svc:
            dt = parse_dt(body)
            if dt:
                # book directly
                if not within_opening(dt):
                    msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
                    return str(resp)

                minutes = int(svc[2])
                end_dt = dt + timedelta(minutes=minutes)
                if not is_time_available(CALENDAR_ID, dt, end_dt):
                    offered = next_available_slots(CALENDAR_ID, minutes, dt + timedelta(minutes=SLOT_STEP_MINUTES), SLOT_STEP_MINUTES, limit=3)
                    s["step"] = "offer_slots"
                    s["service"] = svc
                    s["offered_slots"] = offered

                    if not offered:
                        msg.body("❌ That time is taken. Try another day/time.")
                        return str(resp)

                    lines = ["❌ That time is taken. Next available:"]
                    for i, odt in enumerate(offered, start=1):
                        lines.append(f"{i}) {fmt_dt(odt)}")
                    lines.append("\nReply with *1/2/3* or type a new time (e.g. *Tomorrow 3pm*).")
                    msg.body("\n".join(lines))
                    return str(resp)

                try:
                    created = create_booking_event(CALENDAR_ID, dt, minutes, svc[0], from_number)
                except Exception as e:
                    msg.body(f"⚠️ Couldn’t book. ({e})")
                    reset(from_number)
                    return str(resp)

                reset(from_number)
                msg.body(
                    f"✅ Booked: *{svc[0]}*\n"
                    f"🗓 {fmt_dt(created['start'])}\n"
                    f"🔗 Calendar link: {created.get('htmlLink','')}\n\n"
                    f"{friendly_after_booking()}"
                )
                return str(resp)

            # Service only (no time)
            s["step"] = "await_time"
            s["service"] = svc
            msg.body(ask_time_for_service(svc))
            return str(resp)

        # No service detected — show menu gently
        msg.body("Type *MENU* to see services, or say something like: *Book haircut tomorrow 2pm*.")
        return str(resp)

    # If waiting for time
    if s["step"] == "await_time":
        svc = s.get("service")
        if not svc:
            reset(from_number)
            msg.body(menu_text())
            return str(resp)

        dt = parse_dt(body)
        if not dt:
            msg.body("I didn’t understand the time. Try: *Tomorrow 2pm* or *Fri 2pm*.")
            return str(resp)

        if not within_opening(dt):
            msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
            return str(resp)

        minutes = int(svc[2])
        end_dt = dt + timedelta(minutes=minutes)
        if not is_time_available(CALENDAR_ID, dt, end_dt):
            offered = next_available_slots(CALENDAR_ID, minutes, dt + timedelta(minutes=SLOT_STEP_MINUTES), SLOT_STEP_MINUTES, limit=3)
            s["step"] = "offer_slots"
            s["offered_slots"] = offered

            if not offered:
                msg.body("❌ That time is taken. Try another day/time.")
                return str(resp)

            lines = ["❌ That time is taken. Next available:"]
            for i, odt in enumerate(offered, start=1):
                lines.append(f"{i}) {fmt_dt(odt)}")
            lines.append("\nReply with *1/2/3* or type a new time (e.g. *Tomorrow 3pm*).")
            msg.body("\n".join(lines))
            return str(resp)

        try:
            created = create_booking_event(CALENDAR_ID, dt, minutes, svc[0], from_number)
        except Exception as e:
            msg.body(f"⚠️ Couldn’t book. ({e})")
            reset(from_number)
            return str(resp)

        reset(from_number)
        msg.body(
            f"✅ Booked: *{svc[0]}*\n"
            f"🗓 {fmt_dt(created['start'])}\n"
            f"🔗 Calendar link: {created.get('htmlLink','')}\n\n"
            f"{friendly_after_booking()}"
        )
        return str(resp)

    # Fallback
    msg.body("Type *MENU* to see options, or *HELP*.")
    return str(resp)


if __name__ == "__main__":
    # Render binds PORT automatically; local uses 5000 default
    app.run(host="0.0.0.0", port=PORT)