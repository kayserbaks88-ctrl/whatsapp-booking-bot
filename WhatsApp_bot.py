import os
import re
import threading
import time as time_mod
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import dateparser
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

from calendar_helper import (
    create_booking_event,
    delete_booking_event,
    is_time_available,
    next_available_slots,
)

load_dotenv()

# ----------------------------
# Config
# ----------------------------
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))

DEFAULT_SERVICE_MINUTES = int(os.getenv("DEFAULT_SERVICE_MINUTES", "45"))
SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))

# Reminders: 1 hour + 30 mins
REMINDER_1 = int(os.getenv("REMINDER_1", "60"))
REMINDER_2 = int(os.getenv("REMINDER_2", "30"))
ENABLE_REMINDERS = os.getenv("ENABLE_REMINDERS", "1") == "1"

# Twilio REST (for outbound reminders)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")  # e.g. "whatsapp:+14155238886" or your approved number

try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

# Shop hours: Mon–Sat 9–6, Sun closed
SHOP_HOURS = {
    0: (time(9, 0), time(18, 0)),  # Mon
    1: (time(9, 0), time(18, 0)),  # Tue
    2: (time(9, 0), time(18, 0)),  # Wed
    3: (time(9, 0), time(18, 0)),  # Thu
    4: (time(9, 0), time(18, 0)),  # Fri
    5: (time(9, 0), time(18, 0)),  # Sat
    6: None,                       # Sun closed
}

SERVICES = ["Haircut", "Skin Fade", "Beard Trim", "Kids Cut", "Shape Up"]

SERVICE_DURATIONS = {
    "Haircut": 45,
    "Skin Fade": 60,
    "Beard Trim": 30,
    "Kids Cut": 30,
    "Shape Up": 30,
}

# Prices (shown in menu + confirmation)
SERVICE_PRICES = {
    "Haircut": "£18",
    "Skin Fade": "£22",
    "Beard Trim": "£10",
    "Kids Cut": "£12",
    "Shape Up": "£8",
}

# NOTE: match longer phrases first (fixes "kids cut" being detected as "cut")
SERVICE_ALIASES = {
    "skin fade": "Skin Fade",
    "beard trim": "Beard Trim",
    "kids cut": "Kids Cut",
    "kid cut": "Kids Cut",
    "kids haircut": "Kids Cut",
    "child cut": "Kids Cut",
    "shape up": "Shape Up",
    "line up": "Shape Up",
    "lineup": "Shape Up",

    "haircut": "Haircut",
    "beard": "Beard Trim",
    "kids": "Kids Cut",
    "fade": "Skin Fade",
    "cut": "Haircut",
}

# ----------------------------
# In-memory storage
# ----------------------------
# phone -> state for booking flow
user_state = {}

# phone -> active booking info (used for reminders & cancel)
active_bookings = {}


# ----------------------------
# Helpers
# ----------------------------
def norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())

def pretty_hours_one_line() -> str:
    return "Hours: Mon–Sat 9am–6pm | Sun Closed"

def pretty_hours_block() -> str:
    return "Opening hours:\nMon–Sat: 9am–6pm\nSun: Closed"

def brand_header() -> str:
    return "💈 BBC Barbers\nPowered by TrimTech AI"

def reset_user(phone: str):
    user_state.pop(phone, None)

def get_or_init(phone: str):
    if phone not in user_state:
        user_state[phone] = {"step": "MENU", "service": None, "dt": None, "name": None, "suggestions": None}
    return user_state[phone]

def is_within_open_hours(dt: datetime) -> bool:
    dt = dt.astimezone(TZ)
    hours = SHOP_HOURS.get(dt.weekday())
    if not hours:
        return False
    start_t, end_t = hours
    return (start_t <= dt.time() < end_t)

def round_up_to_step(dt: datetime, step_minutes: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    rem = dt.minute % step_minutes
    if rem == 0:
        return dt
    return dt + timedelta(minutes=(step_minutes - rem))

def next_open_slot(after_dt: datetime) -> datetime:
    dt = round_up_to_step(after_dt.astimezone(TZ), SLOT_STEP_MINUTES)
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    if dt < now:
        dt = round_up_to_step(now, SLOT_STEP_MINUTES)

    for _ in range(14 * 24 * 60 // SLOT_STEP_MINUTES):
        hours = SHOP_HOURS.get(dt.weekday())
        if hours:
            start_t, end_t = hours
            if dt.time() < start_t:
                dt = dt.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
                dt = round_up_to_step(dt, SLOT_STEP_MINUTES)
            if start_t <= dt.time() < end_t:
                return dt
        dt = dt + timedelta(minutes=SLOT_STEP_MINUTES)

    return round_up_to_step(datetime.now(TZ), SLOT_STEP_MINUTES)

def parse_datetime(text: str) -> datetime | None:
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }
    dt = dateparser.parse(text, settings=settings)
    if not dt:
        return None
    return dt.astimezone(TZ)

def service_from_text(text: str) -> str | None:
    t = norm(text)

    if t.isdigit():
        i = int(t) - 1
        if 0 <= i < len(SERVICES):
            return SERVICES[i]

    for s in SERVICES:
        if norm(s) == t:
            return s

    for k in sorted(SERVICE_ALIASES.keys(), key=len, reverse=True):
        if k in t:
            return SERVICE_ALIASES[k]

    return None

def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %d %b, %I:%M %p").lstrip("0").replace(" 0", " ")

def menu_text() -> str:
    lines = [brand_header(), "", "Choose a service:"]
    for i, s in enumerate(SERVICES, start=1):
        price = SERVICE_PRICES.get(s, "")
        lines.append(f"{i}) {s} — {price}" if price else f"{i}) {s}")

    lines += ["", "Reply with a number or service name.", pretty_hours_one_line()]
    return "\n".join(lines)

def confirm_text(st) -> str:
    dt: datetime = st["dt"]
    s = st["service"]
    name = st.get("name") or "Customer"
    price = SERVICE_PRICES.get(s, "")
    service_line = f"{s}" + (f" — {price}" if price else "")

    return (
        "Please confirm:\n\n"
        "💈 BBC Barbers\n"
        f"Service: {service_line}\n"
        f"📅 {fmt_dt(dt)}\n"
        f"Name: {name}\n\n"
        "Reply:\n"
        "YES – confirm\n"
        "CHANGE – new time\n"
        "NO – cancel"
    )

def can_send_outbound() -> bool:
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TwilioClient)

def send_whatsapp(to_phone: str, body: str) -> bool:
    if not can_send_outbound():
        return False
    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_phone, body=body)
        return True
    except Exception:
        return False


# ----------------------------
# Reminder Loop
# ----------------------------
def reminder_loop():
    while True:
        try:
            now = datetime.now(TZ)
            for phone, b in list(active_bookings.items()):
                try:
                    dt = datetime.fromisoformat(b["dt"]).astimezone(TZ)
                except Exception:
                    continue

                if dt < now - timedelta(minutes=5):
                    active_bookings.pop(phone, None)
                    continue

                mins_to = int((dt - now).total_seconds() // 60)
                service = b.get("service", "appointment")
                price = SERVICE_PRICES.get(service, "")
                service_line = f"{service}" + (f" — {price}" if price else "")

                if (not b.get("rem1_sent")) and (REMINDER_1 - 1 <= mins_to <= REMINDER_1 + 1):
                    msg = (
                        "⏰ Reminder — BBC Barbers\n\n"
                        f"{b.get('name','Customer')}, your appointment is in 1 hour\n"
                        f"{service_line}\n"
                        f"📅 {fmt_dt(dt)}\n\n"
                        "Reply CANCEL if you can’t make it.\n"
                        "Powered by TrimTech AI"
                    )
                    if send_whatsapp(phone, msg):
                        b["rem1_sent"] = True

                if (not b.get("rem2_sent")) and (REMINDER_2 - 1 <= mins_to <= REMINDER_2 + 1):
                    msg = (
                        "⏰ See you soon — BBC Barbers\n\n"
                        f"{b.get('name','Customer')}, your appointment is in 30 minutes\n"
                        f"{service_line}\n"
                        f"📅 {fmt_dt(dt)}\n\n"
                        "Reply CANCEL if needed.\n"
                        "Powered by TrimTech AI"
                    )
                    if send_whatsapp(phone, msg):
                        b["rem2_sent"] = True

        except Exception:
            pass

        time_mod.sleep(60)


# ----------------------------
# Flask App
# ----------------------------
app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

@app.post("/whatsapp")
def whatsapp_webhook():
    incoming = request.values.get("Body", "")
    phone = request.values.get("From", "")
    t = norm(incoming)

    resp = MessagingResponse()
    msg = resp.message()

    st = get_or_init(phone)

    # -------- Global commands
    if t in {"start", "restart", "reset", "menu"}:
        reset_user(phone)
        st = get_or_init(phone)
        msg.body(menu_text())
        return str(resp)

    if t == "view":
        b = active_bookings.get(phone)
        if b and b.get("link"):
            msg.body(f"Your booking link:\n👉 {b['link']}\n\nReply MENU to return.")
        else:
            msg.body("No booking link saved yet. Book first, then reply VIEW.")
        return str(resp)

    # CANCEL (delete calendar event)
    if t == "cancel":
        b = active_bookings.get(phone)
        if not b or not b.get("event_id"):
            reset_user(phone)
            msg.body("No active booking found to cancel. Reply MENU to start.")
            return str(resp)

        ok, m = delete_booking_event(CALENDAR_ID, b["event_id"])
        if ok:
            active_bookings.pop(phone, None)
            reset_user(phone)
            msg.body("✅ Cancelled — your appointment has been removed.\nReply MENU to book again.")
        else:
            msg.body(f"⚠️ I couldn’t cancel it automatically: {m}\nReply MENU or try again.")
        return str(resp)

    # -------- Flow steps
    step = st.get("step", "MENU")

    # Step: MENU
    if step == "MENU":
        svc = service_from_text(incoming)
        if not svc:
            msg.body(menu_text())
            return str(resp)

        st["service"] = svc
        st["step"] = "ASK_DATETIME"
        msg.body(
            f"💈 BBC Barbers — {svc}\n\n"
            f"{pretty_hours_block()}\n\n"
            "📅 What day & time would you like?\n"
            "Examples:\n"
            "• Tomorrow 2pm\n"
            "• Sat 7 Feb 1pm\n"
            "• 10/02 15:30"
        )
        return str(resp)

    # Step: ASK_DATETIME
    if step == "ASK_DATETIME":
        # Handle 1/2/3 suggestions
        if t in {"1", "2", "3"} and st.get("suggestions"):
            idx = int(t) - 1
            try:
                dt = datetime.fromisoformat(st["suggestions"][idx]).astimezone(TZ)
            except Exception:
                dt = None
        else:
            dt = parse_datetime(incoming)

        if not dt:
            msg.body(
                "Sorry — I couldn't understand that time.\n\n"
                "Try:\n"
                "• Tomorrow 2pm\n"
                "• Sat 7 Feb 1pm\n"
                "• 10/02 15:30"
            )
            return str(resp)

        now = datetime.now(TZ)
        if dt < now + timedelta(minutes=5):
            suggested = next_open_slot(now + timedelta(minutes=15))
            msg.body(
                "That time is too soon / in the past.\n"
                f"Try this instead: {fmt_dt(suggested)}\n\n"
                "Reply with a new day & time."
            )
            return str(resp)

        if not is_within_open_hours(dt):
            a = next_open_slot(dt)
            b = next_open_slot(a + timedelta(minutes=SLOT_STEP_MINUTES))
            c = next_open_slot(b + timedelta(minutes=SLOT_STEP_MINUTES))
            st["suggestions"] = [a.isoformat(), b.isoformat(), c.isoformat()]
            msg.body(
                "⛔ We’re closed at that time.\n\n"
                f"{pretty_hours_block()}\n\n"
                "Next available:\n"
                f"1) {fmt_dt(a)}\n"
                f"2) {fmt_dt(b)}\n"
                f"3) {fmt_dt(c)}\n\n"
                "Reply 1, 2, 3 — or send another day & time."
            )
            return str(resp)

        # ✅ ANTI DOUBLE-BOOKING (check calendar conflicts here)
        service_name = st.get("service")
        duration = SERVICE_DURATIONS.get(service_name, DEFAULT_SERVICE_MINUTES)

        available, reason = is_time_available(
            when=dt,
            calendar_id=CALENDAR_ID,
            duration_minutes=duration,
            timezone=TIMEZONE,
        )

        if not available:
            alts = next_available_slots(
                after_when=dt + timedelta(minutes=SLOT_STEP_MINUTES),
                calendar_id=CALENDAR_ID,
                duration_minutes=duration,
                timezone=TIMEZONE,
                opening_hour=9,
                closing_hour=18,
                slot_step_minutes=SLOT_STEP_MINUTES,
                days_ahead=7,
                max_suggestions=3,
            )

            if alts:
                st["suggestions"] = [x.isoformat() for x in alts]
                msg.body(
                    "❌ That time is already booked.\n\n"
                    "Next available:\n"
                    f"1) {fmt_dt(alts[0])}\n"
                    f"2) {fmt_dt(alts[1]) if len(alts) > 1 else ''}\n"
                    f"3) {fmt_dt(alts[2]) if len(alts) > 2 else ''}\n\n"
                    "Reply 1, 2, 3 — or send another day & time."
                )
            else:
                msg.body(
                    "❌ That time is already booked.\n\n"
                    "Reply with another day & time."
                )
            return str(resp)

        # If free → proceed
        st["dt"] = dt
        st["suggestions"] = None
        st["step"] = "ASK_NAME"
        msg.body(f"✅ Time held: {fmt_dt(dt)}\n\nWhat’s your name?")
        return str(resp)

    # Step: ASK_NAME
    if step == "ASK_NAME":
        name = incoming.strip()
        name = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ '\-]", "", name).strip()

        if len(name) < 2:
            msg.body("Please send your name (e.g. Ty).")
            return str(resp)

        st["name"] = name
        st["step"] = "CONFIRM"
        msg.body(confirm_text(st))
        return str(resp)

    # Step: CONFIRM
    if step == "CONFIRM":
        if t == "no":
            reset_user(phone)
            msg.body("Cancelled. Reply MENU to start again.")
            return str(resp)

        if t == "change":
            st["dt"] = None
            st["step"] = "ASK_DATETIME"
            msg.body(f"Sure — send the new day & time you want.\n\n{pretty_hours_block()}")
            return str(resp)

        if t == "yes":
            service_name = st["service"]
            when = st["dt"]
            name = st.get("name") or "Customer"
            duration = SERVICE_DURATIONS.get(service_name, DEFAULT_SERVICE_MINUTES)

            # ✅ Double-check availability again (prevents race condition)
            available, reason = is_time_available(
                when=when,
                calendar_id=CALENDAR_ID,
                duration_minutes=duration,
                timezone=TIMEZONE,
            )
            if not available:
                alts = next_available_slots(
                    after_when=when + timedelta(minutes=SLOT_STEP_MINUTES),
                    calendar_id=CALENDAR_ID,
                    duration_minutes=duration,
                    timezone=TIMEZONE,
                    opening_hour=9,
                    closing_hour=18,
                    slot_step_minutes=SLOT_STEP_MINUTES,
                    days_ahead=7,
                    max_suggestions=3,
                )
                st["step"] = "ASK_DATETIME"
                st["dt"] = None
                st["suggestions"] = [x.isoformat() for x in alts] if alts else None

                if alts:
                    msg.body(
                        "❌ Sorry — that slot was just taken.\n\n"
                        "Next available:\n"
                        f"1) {fmt_dt(alts[0])}\n"
                        f"2) {fmt_dt(alts[1]) if len(alts) > 1 else ''}\n"
                        f"3) {fmt_dt(alts[2]) if len(alts) > 2 else ''}\n\n"
                        "Reply 1, 2, 3 — or send another day & time."
                    )
                else:
                    msg.body("❌ Sorry — that slot was just taken.\n\nReply with another day & time.")
                return str(resp)

            ok, message, link, event_id = create_booking_event(
                service_name=service_name,
                when=when,
                name=name,
                phone=phone,
                calendar_id=CALENDAR_ID,
                duration_minutes=duration,
                timezone=TIMEZONE,
            )

            if ok:
                active_bookings[phone] = {
                    "service": service_name,
                    "dt": when.isoformat(),
                    "name": name,
                    "event_id": event_id,
                    "link": link,
                    "rem1_sent": False,
                    "rem2_sent": False,
                }
                reset_user(phone)

                price = SERVICE_PRICES.get(service_name, "")
                service_line = f"{service_name}" + (f" — {price}" if price else "")

                msg.body(
                    f"✅ Confirmed — thank you {name}\n\n"
                    "💈 BBC Barbers\n"
                    f"Service: {service_line}\n"
                    f"📅 {fmt_dt(when)}\n\n"
                    "• Reminders at 1 hour & 30 mins\n"
                    "• Reply CANCEL to cancel\n"
                    "• Reply VIEW for booking link\n\n"
                    "Powered by TrimTech AI"
                )
            else:
                msg.body(f"{message}\n\nReply MENU to try again.")
            return str(resp)

        msg.body(confirm_text(st))
        return str(resp)

    msg.body(menu_text())
    return str(resp)


if __name__ == "__main__":
    if ENABLE_REMINDERS and can_send_outbound():
        threading.Thread(target=reminder_loop, daemon=True).start()
        print("✅ Reminder loop ON (1h + 30m)")
    else:
        print("⚠️ Reminder loop OFF (missing Twilio REST creds or disabled)")

    app.run(host="0.0.0.0", port=PORT, debug=True)
