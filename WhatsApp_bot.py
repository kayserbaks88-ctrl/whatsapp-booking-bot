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
    list_upcoming_bookings_for_number,
    delete_booking_event,
    reschedule_booking_event,
)
from llm_helper import llm_extract  # safe JSON extractor (optional)

load_dotenv()

# ---------------- CONFIG ----------------
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))

ENABLE_LLM = os.getenv("ENABLE_LLM", "0").strip() == "1"

HOLD_EXPIRE_MINUTES = int(os.getenv("HOLD_EXPIRE_MINUTES", "10"))
SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}  # sun closed
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

# --- Full Menu (edit this to match your “full list”) ---
# name, price, duration_minutes, category
SERVICES = [
    ("Haircut", 18, 45, "Men's Cuts"),
    ("Skin Fade", 22, 60, "Men's Cuts"),
    ("Shape Up", 12, 20, "Men's Cuts"),
    ("Beard Trim", 10, 20, "Beard Trimming"),
    ("Hot Towel Shave", 25, 45, "Hot Towel Shaves"),
    ("Blow Dry", 20, 30, "Blow Dry"),
    ("Boy's Cut", 15, 45, "Boy's Cuts"),
    ("Children's Cut", 15, 45, "Children's Cuts"),
    ("Eyebrow Trim", 5, 10, "Waxing"),
    ("Ear Waxing", 7, 10, "Waxing"),
    ("Nose Waxing", 7, 10, "Waxing"),
]

# simple aliases (add more if you want)
SERVICE_ALIASES = {
    "kids cut": "Children's Cut",
    "childrens cut": "Children's Cut",
    "children cut": "Children's Cut",
    "boy cut": "Boy's Cut",
    "beard trimming": "Beard Trim",
    "hot towel": "Hot Towel Shave",
    "fade": "Skin Fade",
}

# -------------- State (simple local memory) --------------
# STATE[from_number] = dict(step=..., service=..., suggestions=[...], cancel_list=[...], resched_list=[...], resched_event_id=...)
STATE = {}

app = Flask(__name__)

# ----------------- Helpers -----------------

def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def lower_clean(s: str) -> str:
    return normalize_text(s).lower()

def is_thanks(text: str) -> bool:
    t = lower_clean(text)
    return any(x in t for x in ["thank you", "thanks", "thx", "cheers", "appreciate"])

def menu_text() -> str:
    # group by category
    by_cat = {}
    for i, (name, price, dur, cat) in enumerate(SERVICES, start=1):
        by_cat.setdefault(cat, []).append((i, name, price, dur))

    lines = []
    lines.append(f"💈 *{SHOP_NAME}*")
    lines.append("Welcome! Reply with a *number* or *name*:")
    lines.append("")
    for cat, items in by_cat.items():
        lines.append(f"*{cat}*")
        for i, name, price, _dur in items:
            lines.append(f"{i}) {name} — £{price}")
        lines.append("")

    lines.append(f"Hours: Mon–Sat 9am–6pm | Sun Closed")
    lines.append("")
    lines.append("Commands: *MENU* | *MY BOOKINGS* | *CANCEL* | *RESCHEDULE*")
    lines.append("")
    lines.append("Tip: you can type a full sentence like:")
    lines.append("• Book a skin fade *tomorrow at 2pm*")
    lines.append("• Can I get a haircut *Friday at 2pm*?")
    lines.append("")
    lines.append("Type *MENU* anytime to see this again.")
    return "\n".join(lines)

def help_compact() -> str:
    return "Type *MENU* to see services, or say: *Book haircut tomorrow 2pm*."

def find_service_by_number(n: int):
    if 1 <= n <= len(SERVICES):
        return SERVICES[n - 1][0]
    return None

def find_service_by_name(text: str):
    t = lower_clean(text)

    # alias mapping
    if t in SERVICE_ALIASES:
        t = lower_clean(SERVICE_ALIASES[t])

    # direct contains match
    for (name, _price, _dur, _cat) in SERVICES:
        if lower_clean(name) == t:
            return name

    for (name, _price, _dur, _cat) in SERVICES:
        if lower_clean(name) in t:
            return name

    # try alias contains
    for k, v in SERVICE_ALIASES.items():
        if k in t:
            return v

    return None

def service_duration_minutes(service_name: str) -> int:
    for (name, _price, dur, _cat) in SERVICES:
        if name == service_name:
            return int(dur)
    return 45

def parse_datetime_from_text(text: str):
    """Parse datetime in local TZ using dateparser. Returns aware datetime or None."""
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.now(TZ),
    }
    dt = dateparser.parse(text, settings=settings)
    if not dt:
        return None
    # ensure tz-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    else:
        dt = dt.astimezone(TZ)
    return dt

def within_opening_hours(dt: datetime, duration_min: int) -> bool:
    # day check
    day = dt.strftime("%a").lower()[:3]  # mon/tue...
    if day not in OPEN_DAYS:
        return False

    start_t = dt.time()
    end_dt = dt + timedelta(minutes=duration_min)

    # must start >= OPEN_TIME and end <= CLOSE_TIME
    if start_t < OPEN_TIME:
        return False
    if end_dt.time() > CLOSE_TIME:
        return False
    return True

def format_dt(dt: datetime) -> str:
    return dt.strftime("%a %d %b %H:%M")

def ensure_state(from_number: str):
    if from_number not in STATE:
        STATE[from_number] = {"step": "idle"}
    return STATE[from_number]

def reset_flow(st: dict):
    st.clear()
    st["step"] = "idle"

def set_choose_service(st: dict):
    st["step"] = "choose_service"
    st.pop("service", None)
    st.pop("suggestions", None)
    st.pop("pending_action", None)
    st.pop("cancel_list", None)
    st.pop("resched_list", None)
    st.pop("resched_event_id", None)

def set_ask_time(st: dict, service_name: str):
    st["step"] = "ask_time"
    st["service"] = service_name
    st.pop("suggestions", None)

def build_ask_time_message(service_name: str) -> str:
    return (
        f"✍🏽 *{service_name}*\n\n"
        "What day & time?\nExamples:\n"
        "• Tomorrow 2pm\n"
        "• Fri 2pm\n"
        "• 10/02 15:30\n\n"
        "Reply *BACK* to change service."
    )

def apply_global_commands(text: str, st: dict):
    t = lower_clean(text)

    if t in ["menu", "help", "start"]:
        set_choose_service(st)
        return "MENU"

    if t in ["back", "change", "change service"]:
        set_choose_service(st)
        return "MENU"

    if t in ["my bookings", "my booking", "bookings", "view", "view bookings"]:
        st["step"] = "my_bookings"
        return "MY_BOOKINGS"

    if t in ["cancel", "cancel booking"]:
        st["step"] = "cancel_choose"
        return "CANCEL"

    if t in ["reschedule", "resched", "move booking", "change booking"]:
        st["step"] = "resched_choose"
        return "RESCHEDULE"

    return None

# ---------------- Main WhatsApp Route ----------------

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    from_number = request.form.get("From", "")
    body = normalize_text(request.form.get("Body", ""))

    resp = MessagingResponse()
    msg = resp.message()

    st = ensure_state(from_number)

    # Friendly thanks acknowledgement
    if is_thanks(body) and st.get("step") in ["idle", "choose_service"]:
        msg.body("🙏 No worries! Type *MENU* anytime if you want to book.")
        return str(resp)

    # Global commands
    cmd = apply_global_commands(body, st)
    if cmd == "MENU":
        msg.body(menu_text())
        return str(resp)
    if cmd == "MY_BOOKINGS":
        bookings = list_upcoming_bookings_for_number(CALENDAR_ID, from_number, TZ)
        if not bookings:
            msg.body("You don’t have any upcoming bookings. Type *MENU* to book.")
        else:
            lines = ["📅 *Your bookings:*"]
            for i, b in enumerate(bookings, start=1):
                lines.append(f"{i}) {b['service']} — {format_dt(b['start'])}")
            lines.append("")
            lines.append("Type *CANCEL* or *RESCHEDULE* to manage.")
            msg.body("\n".join(lines))
        st["step"] = "idle"
        return str(resp)
    if cmd == "CANCEL":
        bookings = list_upcoming_bookings_for_number(CALENDAR_ID, from_number, TZ)
        if not bookings:
            msg.body("No upcoming bookings to cancel. Type *MENU* to book.")
            st["step"] = "idle"
        else:
            st["cancel_list"] = bookings
            st["step"] = "cancel_pick"
            lines = ["❌ *Cancel which booking?* Reply with 1/2/3:"]
            for i, b in enumerate(bookings, start=1):
                lines.append(f"{i}) {b['service']} — {format_dt(b['start'])}")
            lines.append("\nReply *BACK* to go to menu.")
            msg.body("\n".join(lines))
        return str(resp)
    if cmd == "RESCHEDULE":
        bookings = list_upcoming_bookings_for_number(CALENDAR_ID, from_number, TZ)
        if not bookings:
            msg.body("No upcoming bookings to reschedule. Type *MENU* to book.")
            st["step"] = "idle"
        else:
            st["resched_list"] = bookings
            st["step"] = "resched_pick"
            lines = ["🔁 *Reschedule which booking?* Reply with 1/2/3:"]
            for i, b in enumerate(bookings, start=1):
                lines.append(f"{i}) {b['service']} — {format_dt(b['start'])}")
            lines.append("\nReply *BACK* to go to menu.")
            msg.body("\n".join(lines))
        return str(resp)

    # If idle, show menu on any greeting
    if st.get("step") == "idle":
        # Try “book …” directly (LLM optional), otherwise show menu
        if lower_clean(body).startswith("book ") or ("book" in lower_clean(body)):
            # attempt direct parse
            extracted = None
            if ENABLE_LLM:
                extracted = llm_extract(body, SERVICES)
            service = find_service_by_name(extracted.get("service", "")) if extracted else find_service_by_name(body)
            when_text = (extracted.get("when", "") if extracted else "")
            if not when_text:
                when_text = body

            if service:
                dt = parse_datetime_from_text(when_text)
                if not dt:
                    set_ask_time(st, service)
                    msg.body(build_ask_time_message(service))
                    return str(resp)
                return _attempt_booking(msg, st, from_number, service, dt)
            else:
                set_choose_service(st)
                msg.body(menu_text())
                return str(resp)

        set_choose_service(st)
        msg.body(menu_text())
        return str(resp)

    # ---------------- choose_service ----------------
    if st.get("step") == "choose_service":
        t = lower_clean(body)

        # IMPORTANT: if they send a number, do NOT parse time here (fixes your bug)
        if t.isdigit():
            n = int(t)
            svc = find_service_by_number(n)
            if not svc:
                msg.body(f"Please reply with a number *1–{len(SERVICES)}* or type a service name.\n\nType *MENU* to see options.")
                return str(resp)
            set_ask_time(st, svc)
            msg.body(build_ask_time_message(svc))
            return str(resp)

        svc = find_service_by_name(body)
        if not svc:
            msg.body("I didn’t catch that service. Reply with a number (e.g. 1) or type the name.\n\nType *MENU* to see the list again.")
            return str(resp)

        set_ask_time(st, svc)
        msg.body(build_ask_time_message(svc))
        return str(resp)

    # ---------------- ask_time ----------------
    if st.get("step") == "ask_time":
        svc = st.get("service")
        if not svc:
            set_choose_service(st)
            msg.body(menu_text())
            return str(resp)

        # If they reply 1/2/3 from suggestions
        if body.strip().isdigit() and st.get("suggestions"):
            idx = int(body.strip()) - 1
            sug = st.get("suggestions", [])
            if 0 <= idx < len(sug):
                dt = sug[idx]
                return _attempt_booking(msg, st, from_number, svc, dt)

        # Parse actual datetime
        dt = parse_datetime_from_text(body)
        if not dt:
            msg.body("I didn’t understand the time.\nTry:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\nReply *BACK* to change service.")
            return str(resp)

        return _attempt_booking(msg, st, from_number, svc, dt)

    # ---------------- cancel_pick ----------------
    if st.get("step") == "cancel_pick":
        if body.strip().isdigit():
            idx = int(body.strip()) - 1
            items = st.get("cancel_list", [])
            if 0 <= idx < len(items):
                ev = items[idx]
                delete_booking_event(CALENDAR_ID, ev["event_id"])
                msg.body(f"✅ Cancelled: *{ev['service']}* — {format_dt(ev['start'])}\n\nType *MENU* to book again.")
                reset_flow(st)
                return str(resp)

        msg.body("Reply with the booking number to cancel (e.g. 1). Or type *BACK* for menu.")
        return str(resp)

    # ---------------- resched_pick ----------------
    if st.get("step") == "resched_pick":
        if body.strip().isdigit():
            idx = int(body.strip()) - 1
            items = st.get("resched_list", [])
            if 0 <= idx < len(items):
                ev = items[idx]
                st["resched_event_id"] = ev["event_id"]
                st["service"] = ev["service"]
                st["step"] = "resched_time"
                msg.body(
                    f"🔁 Reschedule *{ev['service']}* (currently {format_dt(ev['start'])}).\n\n"
                    "What new day & time?\nExamples:\n• Tomorrow 2pm\n• Fri 2pm\n• 10/02 15:30\n\n"
                    "Reply *BACK* to go to menu."
                )
                return str(resp)

        msg.body("Reply with the booking number to reschedule (e.g. 1). Or type *BACK* for menu.")
        return str(resp)

    # ---------------- resched_time ----------------
    if st.get("step") == "resched_time":
        event_id = st.get("resched_event_id")
        svc = st.get("service")
        if not event_id or not svc:
            reset_flow(st)
            msg.body("Something went wrong. Type *RESCHEDULE* to try again.")
            return str(resp)

        dt = parse_datetime_from_text(body)
        if not dt:
            msg.body("I didn’t understand the time.\nTry:\n• Tomorrow 2pm\n• Fri 2pm\n• 10/02 15:30\n\nReply *BACK* to go to menu.")
            return str(resp)

        duration = service_duration_minutes(svc)
        if not within_opening_hours(dt, duration):
            msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
            return str(resp)

        # availability check
        if not is_time_available(CALENDAR_ID, dt, duration, TZ, ignore_event_id=event_id):
            suggestions = next_available_slots(CALENDAR_ID, dt, duration, TZ, limit=3, step_minutes=SLOT_STEP_MINUTES)
            if suggestions:
                st["suggestions"] = suggestions
                lines = ["❌ That time is taken. Next available:"]
                for i, sdt in enumerate(suggestions, start=1):
                    lines.append(f"{i}) {format_dt(sdt)}")
                lines.append("\nReply with 1/2/3 or type another time.")
                msg.body("\n".join(lines))
            else:
                msg.body("❌ That time is taken and I couldn’t find alternatives. Try another time.")
            return str(resp)

        updated = reschedule_booking_event(CALENDAR_ID, event_id, dt, duration, TZ)
        msg.body(
            f"✅ Rescheduled: *{svc}*\n📅 {format_dt(dt)}\n\n"
            f"Anything else? Type *MENU* or *MY BOOKINGS*."
        )
        reset_flow(st)
        return str(resp)

    # Fallback
    reset_flow(st)
    msg.body(help_compact())
    return str(resp)


def _attempt_booking(msg, st, from_number, service_name, dt):
    duration = service_duration_minutes(service_name)

    if not within_opening_hours(dt, duration):
        msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
        return str(MessagingResponse())  # not used


    # check availability
    if not is_time_available(CALENDAR_ID, dt, duration, TZ):
        suggestions = next_available_slots(CALENDAR_ID, dt, duration, TZ, limit=3, step_minutes=SLOT_STEP_MINUTES)
        if suggestions:
            st["suggestions"] = suggestions
            lines = ["❌ That time is taken. Next available:"]
            for i, sdt in enumerate(suggestions, start=1):
                lines.append(f"{i}) {format_dt(sdt)}")
            lines.append("\nReply with 1/2/3 or type another time (or *BACK* to change service).")
            msg.body("\n".join(lines))
            return str(MessagingResponse())  # not used
        else:
            msg.body("❌ That time is taken. Try another time.")
            return str(MessagingResponse())  # not used

    # create event
    event = create_booking_event(
        calendar_id=CALENDAR_ID,
        from_number=from_number,
        service_name=service_name,
        start_dt=dt,
        duration_minutes=duration,
        tz=TZ,
        shop_name=SHOP_NAME,
    )

    st.clear()
    st["step"] = "idle"

    calendar_link = event.get("htmlLink") or ""
    text = f"✅ Booked: *{service_name}*\n📅 {format_dt(dt)}"
    if calendar_link:
        text += f"\n\n🔗 Calendar link: {calendar_link}"
    text += "\n\nAnything else? Type *MENU* to book another, or *MY BOOKINGS* to view."
    msg.body(text)
    return str(MessagingResponse())  # not used


if __name__ == "__main__":
    # Render binds PORT automatically; local uses 5000 by default
    app.run(host="0.0.0.0", port=PORT)