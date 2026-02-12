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
    update_booking_event_time,
    read_event,
    find_next_booking_by_phone,
)

load_dotenv()

# ---------------- CONFIG ----------------

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = "BBC Barbers"

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))

HOLD_EXPIRE_MINUTES = 10
SLOT_STEP_MINUTES = 15

# Optional cleanup/buffer time (0 = back-to-back)
BOOKING_BUFFER_MINUTES = int(os.getenv("BOOKING_BUFFER_MINUTES", "0"))

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

SERVICES = [
    ("Haircut", 18, 45),
    ("Skin Fade", 22, 60),
    ("Beard Trim", 10, 30),
    ("Kids Cut", 15, 30),
    ("Shape Up", 12, 30),
]
SERVICE_META = {n: {"price": p, "minutes": m} for n, p, m in SERVICES}

ALIASES = {
    "1": "Haircut",
    "2": "Skin Fade",
    "3": "Beard Trim",
    "4": "Kids Cut",
    "5": "Shape Up",
    "haircut": "Haircut",
    "fade": "Skin Fade",
    "beard": "Beard Trim",
    "kids": "Kids Cut",
    "shape": "Shape Up",
    "shape up": "Shape Up",
}

user_state = {}
app = Flask(__name__)

# ---------------- HELPERS ----------------

def norm(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())

def now():
    return datetime.now(TZ)

def fmt(dt):
    return dt.strftime("%a %d %b, %I:%M %p")

def within_hours(dt):
    day = dt.strftime("%a").lower()[:3]
    if day not in OPEN_DAYS:
        return False
    return OPEN_TIME <= dt.time() < CLOSE_TIME

def end_within_hours(start_dt, minutes):
    end_dt = start_dt + timedelta(minutes=minutes)
    return end_dt.time() <= CLOSE_TIME and start_dt.date() == end_dt.date()

def parse_dt(text):
    s = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now(),
    }
    dt = dateparser.parse(text, settings=s)
    if not dt:
        return None
    return dt.astimezone(TZ)

def menu():
    # price only (no durations shown)
    txt = ["💈 BBC Barbers", "Reply with number or name:\n"]
    for i, (n, p, m) in enumerate(SERVICES, 1):
        txt.append(f"{i}) {n} — £{p}")
    txt.append("\nHours: Mon–Sat 9am–6pm | Sun Closed")
    return "\n".join(txt)

def extract_service_from_summary(summary: str):
    # "Haircut — John" -> "Haircut"
    if not summary:
        return None
    parts = [p.strip() for p in summary.split("—")]
    if parts:
        name = parts[0].strip()
        return name if name in SERVICE_META else None
    return None

# ---------------- WEBHOOK ----------------

@app.post("/whatsapp")
def whatsapp():
    from_ = request.form.get("From")
    body = request.form.get("Body")
    t = norm(body)

    resp = MessagingResponse()
    msg = resp.message()

    st = user_state.get(from_, {"step": "START"})

    # ---- GLOBAL COMMANDS ----

    if t == "menu":
        user_state[from_] = {"step": "SERVICE"}
        msg.body(menu())
        return str(resp)

    if t == "cancel":
        eid = st.get("event_id")
        if not eid:
            # try find next booking by phone
            found = find_next_booking_by_phone(CALENDAR_ID, from_, TIMEZONE)
            eid = found["event_id"] if found else None

        if eid:
            delete_booking_event(CALENDAR_ID, eid)
            user_state[from_] = {"step": "SERVICE"}
            msg.body("✅ Booking cancelled. Reply MENU to book again.")
        else:
            msg.body("No confirmed booking found to cancel.")
        return str(resp)

    if t == "view":
        link = st.get("html_link")
        if not link:
            found = find_next_booking_by_phone(CALENDAR_ID, from_, TIMEZONE)
            link = found["html_link"] if found else None

        if link:
            msg.body(f"Your booking link:\n👉 {link}")
        else:
            msg.body("No booking link yet.")
        return str(resp)

    if t == "reschedule":
        # find existing booking (prefer in-memory)
        eid = st.get("event_id")
        old_start = st.get("dt")
        service = st.get("service")
        name = st.get("name")

        if not eid:
            found = find_next_booking_by_phone(CALENDAR_ID, from_, TIMEZONE)
            if not found:
                msg.body("I couldn’t find an upcoming booking for this number.\nReply MENU to book.")
                return str(resp)
            eid = found["event_id"]
            old_start = found["start_dt"]
            service = extract_service_from_summary(found.get("summary", "")) or service
            # name best-effort from summary
            name = name or (found.get("summary", "").split("—")[-1].strip() if "—" in found.get("summary", "") else "Customer")
            st["html_link"] = found.get("html_link")

        # read event to get exact old interval
        ev = read_event(CALENDAR_ID, eid)
        sraw = ev.get("start", {}).get("dateTime")
        eraw = ev.get("end", {}).get("dateTime")
        if not sraw or not eraw:
            msg.body("I found your booking, but couldn’t read its time. Reply MENU to book again.")
            return str(resp)

        old_start = datetime.fromisoformat(sraw.replace("Z", "+00:00")).astimezone(TZ)
        old_end = datetime.fromisoformat(eraw.replace("Z", "+00:00")).astimezone(TZ)

        service = service or extract_service_from_summary(ev.get("summary", "")) or "Haircut"
        duration = SERVICE_META.get(service, {"minutes": 45})["minutes"]

        user_state[from_] = {
            "step": "RESCHEDULE_TIME",
            "event_id": eid,
            "service": service,
            "name": name or "Customer",
            "old_start": old_start,
            "old_end": old_end,
            "duration": duration,
            "html_link": st.get("html_link"),
        }

        msg.body(
            f"🔁 Reschedule your booking:\n"
            f"Current: {fmt(old_start)}\n\n"
            f"Send a new day & time:\n"
            f"Examples:\n• Tomorrow 3pm\n• Fri 2:30pm\n• 13/02 15:15"
        )
        return str(resp)

    # ---- FLOW ----

    step = st.get("step")

    if step == "START":
        user_state[from_] = {"step": "SERVICE"}
        msg.body(menu())
        return str(resp)

    # ----- CHOOSE SERVICE -----

    if step == "SERVICE":
        service = ALIASES.get(t)

        if not service:
            msg.body("Please choose a valid option:\n\n" + menu())
            return str(resp)

        user_state[from_] = {"step": "TIME", "service": service}

        msg.body(
            f"🪒 {service}\n\n"
            f"What day & time?\n"
            f"Examples:\n• Tomorrow 2pm\n• Sat 7 Feb 1pm\n• 10/02 15:30"
        )
        return str(resp)

    # ----- CHOOSE TIME -----

    if step == "TIME":
        service = st.get("service")
        dt = parse_dt(body)

        if not dt:
            msg.body("I didn’t understand the time. Try: Tomorrow 2pm")
            return str(resp)

        if not within_hours(dt) or not end_within_hours(dt, SERVICE_META[service]["minutes"]):
            msg.body("⛔ We’re closed then. Mon–Sat 9–6 only.")
            return str(resp)

        duration = SERVICE_META[service]["minutes"]

        ok, reason = is_time_available(
            dt, CALENDAR_ID, duration, TIMEZONE,
            buffer_minutes=BOOKING_BUFFER_MINUTES
        )

        if not ok:
            alts = next_available_slots(
                dt + timedelta(minutes=SLOT_STEP_MINUTES),
                CALENDAR_ID,
                duration,
                TIMEZONE,
                step_minutes=SLOT_STEP_MINUTES,
                max_results=5,
                search_days=7,
                buffer_minutes=BOOKING_BUFFER_MINUTES
            )

            # filter alts to opening hours on bot side
            alts = [x for x in alts if within_hours(x) and end_within_hours(x, duration)]

            if alts:
                txt = "\n".join([f"• {fmt(x)}" for x in alts])
                msg.body(f"❌ Not available ({reason})\n\nNext:\n{txt}")
            else:
                msg.body("❌ Not available. Try another time.")
            return str(resp)

        # HOLD
        user_state[from_] = {
            "step": "NAME",
            "service": service,
            "dt": dt,
            "hold_until": now() + timedelta(minutes=HOLD_EXPIRE_MINUTES)
        }

        msg.body(f"✅ Time held: {fmt(dt)}\n\nWhat’s your name?")
        return str(resp)

    # ----- ASK NAME -----

    if step == "NAME":
        name = body.strip()
        if len(name) < 2:
            msg.body("Please send your name.")
            return str(resp)

        st["name"] = name
        st["step"] = "CONFIRM"
        user_state[from_] = st

        s = st["service"]
        dt = st["dt"]
        p = SERVICE_META[s]["price"]

        msg.body(
            f"Confirm:\n\n"
            f"💈 {s} — £{p}\n"
            f"📅 {fmt(dt)}\n\n"
            f"Reply YES to confirm or NO to change time."
        )
        return str(resp)

    # ----- CONFIRM -----

    if step == "CONFIRM":
        if t == "no":
            user_state[from_]["step"] = "TIME"
            msg.body("Ok — send new time.")
            return str(resp)

        if t != "yes":
            msg.body("Reply YES or NO")
            return str(resp)

        if now() > st.get("hold_until", now()):
            user_state[from_] = {"step": "TIME", "service": st["service"]}
            msg.body("⏳ Hold expired. Send time again.")
            return str(resp)

        s = st["service"]
        dt = st["dt"]
        duration = SERVICE_META[s]["minutes"]

        ok, reason = is_time_available(
            dt, CALENDAR_ID, duration, TIMEZONE,
            buffer_minutes=BOOKING_BUFFER_MINUTES
        )
        if not ok:
            user_state[from_] = {"step": "TIME", "service": s}
            msg.body(f"❌ Just got taken ({reason}). Send new time.")
            return str(resp)

        event = create_booking_event(
            CALENDAR_ID,
            s,
            st["name"],
            dt,
            duration,
            from_,
            TIMEZONE,
        )

        st["event_id"] = event["event_id"]
        st["html_link"] = event["html_link"]
        st["step"] = "DONE"
        user_state[from_] = st

        p = SERVICE_META[s]["price"]

        msg.body(
            f"✅ BOOKED!\n\n"
            f"💈 {s} — £{p}\n"
            f"📅 {fmt(dt)}\n\n"
            f"Reply RESCHEDULE to change time\n"
            f"Reply CANCEL to cancel\n"
            f"Reply VIEW for link\n\n"
            f"Powered by {BUSINESS_NAME}"
        )
        return str(resp)

    # ----- RESCHEDULE FLOW -----

    if step == "RESCHEDULE_TIME":
        dt = parse_dt(body)
        if not dt:
            msg.body("I didn’t understand the time. Try: Tomorrow 3pm")
            return str(resp)

        service = st["service"]
        duration = st["duration"]
        old_start = st["old_start"]
        old_end = st["old_end"]
        eid = st["event_id"]

        if not within_hours(dt) or not end_within_hours(dt, duration):
            msg.body("⛔ We’re closed then. Mon–Sat 9–6 only.")
            return str(resp)

        ok, reason = is_time_available(
            dt, CALENDAR_ID, duration, TIMEZONE,
            buffer_minutes=BOOKING_BUFFER_MINUTES,
            ignore_interval=(old_start, old_end),
        )

        if not ok:
            alts = next_available_slots(
                dt + timedelta(minutes=SLOT_STEP_MINUTES),
                CALENDAR_ID,
                duration,
                TIMEZONE,
                step_minutes=SLOT_STEP_MINUTES,
                max_results=5,
                search_days=7,
                buffer_minutes=BOOKING_BUFFER_MINUTES
            )
            alts = [x for x in alts if within_hours(x) and end_within_hours(x, duration)]
            if alts:
                txt = "\n".join([f"• {fmt(x)}" for x in alts])
                msg.body(f"❌ Not available ({reason})\n\nNext:\n{txt}")
            else:
                msg.body("❌ Not available. Try another time.")
            return str(resp)

        # update event time
        updated = update_booking_event_time(
            CALENDAR_ID,
            eid,
            dt,
            duration,
            TIMEZONE,
        )

        st["dt"] = dt
        st["html_link"] = updated.get("html_link") or st.get("html_link")
        st["step"] = "DONE"
        user_state[from_] = st

        p = SERVICE_META.get(service, {"price": ""})["price"]

        msg.body(
            f"✅ RESCHEDULED!\n\n"
            f"💈 {service} — £{p}\n"
            f"📅 {fmt(dt)}\n\n"
            f"Reply RESCHEDULE to change again\n"
            f"Reply CANCEL to cancel\n"
            f"Reply VIEW for link\n\n"
            f"Powered by {BUSINESS_NAME}"
        )
        return str(resp)

    # fallback
    user_state[from_] = {"step": "SERVICE"}
    msg.body(menu())
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
