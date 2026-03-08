import os
import re
import json
import psycopg2
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

from llm_helper import llm_extract
from calendar_helper import list_upcoming, is_free, create_booking, cancel_booking

app = Flask(__name__)

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "BBC Barbers")
TIMEZONE = os.getenv("TIMEZONE", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

DATABASE_URL = os.getenv("DATABASE_URL")


# ===============================
# DATABASE STATE STORAGE
# ===============================

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_state (
                    phone TEXT PRIMARY KEY,
                    data JSONB
                );
            """)
        conn.commit()


init_db()


def get_state(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM user_state WHERE phone=%s", (phone,))
            row = cur.fetchone()
            if row:
                return row[0]
    return {"state": "MENU"}


def set_state(phone: str, **kwargs):
    current = get_state(phone)
    current.update(kwargs)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_state (phone, data)
                VALUES (%s, %s)
                ON CONFLICT (phone)
                DO UPDATE SET data = EXCLUDED.data
            """, (phone, json.dumps(current)))
        conn.commit()


def reset_state(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_state WHERE phone=%s", (phone,))
        conn.commit()


# ===============================
# SERVICES
# ===============================

SERVICES = [
    ("Haircut", 18, 30),
    ("Skin Fade", 22, 45),
    ("Shape Up", 12, 20),
    ("Beard Trim", 10, 20),
    ("Hot Towel Shave", 25, 45),
    ("Blow Dry", 20, 30),
]

SERVICE_NAMES = [s[0] for s in SERVICES]
SERVICE_BY_NAME = {s[0].lower(): s for s in SERVICES}
SERVICE_BY_NUM = {str(i + 1): SERVICES[i] for i in range(len(SERVICES))}


# ===============================
# HELPERS
# ===============================

def menu_text():
    lines = [
        f"💈 *{BUSINESS_NAME}*",
        "Welcome! Reply with the number or type the service name."
    ]
    for i, (name, price, mins) in enumerate(SERVICES, start=1):
        lines.append(f"{i}) {name} — £{price} ({mins}m)")
    lines.append("")
    lines.append("Commands: MENU | MY BOOKINGS | CANCEL | RESCHEDULE | BACK")
    return "\n".join(lines)


def normalize_time_text(text: str):
    t = text.strip()
    t = re.sub(r"\b(\d{1,2})\s*p\b", r"\1pm", t, flags=re.I)
    t = re.sub(r"\b(\d{1,2})\s*a\b", r"\1am", t, flags=re.I)
    return t


def parse_datetime(user_text: str):
    import dateparser
    raw = normalize_time_text(user_text.lower())
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.now(TZ),
    }
    return dateparser.parse(raw, settings=settings)


# ===============================
# ROUTE
# ===============================

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    resp = MessagingResponse()
    msg = resp.message()

    from_number = request.values.get("From", "")
    text = (request.values.get("Body", "") or "").strip()
    text_upper = text.upper()

    # Quick reset commands
    if text_upper in ("MENU", "BACK", "START"):
        reset_state(from_number)
        msg.body(menu_text())
        return str(resp)

    # View bookings
    if text_upper in ("MY BOOKINGS", "BOOKINGS"):
        msg.body("\n".join([
            f"{i+1}) {ev['summary']} @ {ev['start']}"
            for i, ev in enumerate(list_upcoming(from_number))
        ]) or "No upcoming bookings.")
        return str(resp)

    # ================= STATE =================
    st = get_state(from_number)
    state = st.get("state", "MENU")

    # ================= MENU =================
    if state == "MENU":

        svc_tuple = None

        if text in SERVICE_BY_NUM:
            svc_tuple = SERVICE_BY_NUM[text]
        elif text.lower() in SERVICE_BY_NAME:
            svc_tuple = SERVICE_BY_NAME[text.lower()]

        if svc_tuple:
            set_state(from_number, state="AWAIT_TIME", service=svc_tuple)
            msg.body(
                f"✂️ {svc_tuple[0]}\n"
                "What day & time?\n"
                "Examples: Tomorrow 2pm | Fri 2pm"
            )
            return str(resp)

        msg.body(menu_text())
        return str(resp)

    # ================= AWAIT TIME =================
    if state == "AWAIT_TIME":

        svc_tuple = st.get("service")
        if not svc_tuple:
            reset_state(from_number)
            msg.body(menu_text())
            return str(resp)

        dt = parse_datetime(text)
        if not dt:
            msg.body("I couldn’t understand that time. Try: Tomorrow 2pm")
            return str(resp)

        start_dt = dt.astimezone(TZ)
        end_dt = start_dt + timedelta(minutes=svc_tuple[2])

        if not is_free(start_dt, end_dt):
            msg.body("❌ That slot is taken. Try another time.")
            return str(resp)

        set_state(
            from_number,
            state="AWAIT_NAME",
            service=svc_tuple,
            pending_start=start_dt.isoformat()
        )

        msg.body("Almost done 👍\nWhat name should I book this under?")
        return str(resp)

    # ================= AWAIT NAME =================
    if state == "AWAIT_NAME":

        svc_tuple = st.get("service")
        pending_start = st.get("pending_start")

        if not svc_tuple or not pending_start:
            reset_state(from_number)
            msg.body(menu_text())
            return str(resp)

        customer_name = text.strip()
        if not customer_name:
            msg.body("Please enter a name.")
            return str(resp)

        start_dt = datetime.fromisoformat(pending_start)

        result = create_booking(
            from_number,
            svc_tuple[0],
            start_dt,
            minutes=svc_tuple[2],
            name=customer_name
        )

        reset_state(from_number)

        pretty_date = start_dt.strftime("%A %d %B")
        pretty_time = start_dt.strftime("%I:%M %p")

        msg.body(
            f"💈 *{BUSINESS_NAME}*\n\n"
            f"✅ Booking Confirmed\n\n"
            f"Service: {svc_tuple[0]}\n"
            f"Date: {pretty_date}\n"
            f"Time: {pretty_time}\n"
            f"Name: {customer_name}\n"
            f"Price: £{svc_tuple[1]}"
        )

        return str(resp)

    # fallback
    reset_state(from_number)
    msg.body(menu_text())
    return str(resp)


@app.route("/health")
def health():
    return "ok", 200