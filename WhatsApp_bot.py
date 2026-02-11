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

# ---------------- CONFIG ----------------

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = "BBC Barbers"

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))

HOLD_EXPIRE_MINUTES = 10
SLOT_STEP_MINUTES = 15

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
    txt = ["💈 BBC Barbers", "Reply with number or name:\n"]
    for i, (n, p, m) in enumerate(SERVICES, 1):
        txt.append(f"{i}) {n} — £{p}")
    txt.append("\nHours: Mon–Sat 9am–6pm | Sun Closed")
    return "\n".join(txt)


# ---------------- WEBHOOK ----------------

@app.post("/whatsapp")
def whatsapp():

    from_ = request.form.get("From")
    body = request.form.get("Body")
    t = norm(body)

    resp = MessagingResponse()
    msg = resp.message()

    st = user_state.get(from_, {"step": "START"})

    # ---- GLOBAL ----

    if t == "menu":
        user_state[from_] = {"step": "SERVICE"}
        msg.body(menu())
        return str(resp)

    if t == "cancel":
        eid = st.get("event_id")
        if eid:
            delete_booking_event(CALENDAR_ID, eid)
            user_state[from_] = {"step": "SERVICE"}
            msg.body("✅ Booking cancelled. Reply MENU to book again.")
        else:
            msg.body("No confirmed booking to cancel.")
        return str(resp)

    if t == "view":
        link = st.get("html_link")
        if link:
            msg.body(f"Your booking link:\n👉 {link}")
        else:
            msg.body("No booking link yet.")
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

        user_state[from_] = {
            "step": "TIME",
            "service": service
        }

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

        if not within_hours(dt):
            msg.body("⛔ We’re closed then. Mon–Sat 9–6 only.")
            return str(resp)

        duration = SERVICE_META[service]["minutes"]

        # 🔥 REAL AVAILABILITY CHECK
        ok, reason = is_time_available(
            dt, CALENDAR_ID, duration, TIMEZONE
        )

        if not ok:
            alts = next_available_slots(
                dt + timedelta(minutes=SLOT_STEP_MINUTES),
                CALENDAR_ID,
                duration,
                TIMEZONE,
            )

            if alts:
                txt = "\n".join([f"• {fmt(x)}" for x in alts])
                msg.body(f"❌ Not available ({reason})\n\nNext:\n{txt}")
            else:
                msg.body("❌ Not available. Try another time.")
            return str(resp)

        # 🔒 HOLD ONLY – NO EVENT CREATED
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

        # ⏱ check hold expiry
        if now() > st.get("hold_until", now()):
            user_state[from_] = {"step": "TIME", "service": st["service"]}
            msg.body("⏳ Hold expired. Send time again.")
            return str(resp)

        # 🔁 DOUBLE CHECK AVAILABILITY
        s = st["service"]
        dt = st["dt"]
        duration = SERVICE_META[s]["minutes"]

        ok, reason = is_time_available(
            dt, CALENDAR_ID, duration, TIMEZONE
        )

        if not ok:
            user_state[from_] = {"step": "TIME", "service": s}
            msg.body(f"❌ Just got taken ({reason}). Send new time.")
            return str(resp)

        # 🎯 CREATE EVENT NOW ONLY
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
