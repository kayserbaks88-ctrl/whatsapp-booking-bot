import os
import re
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

DEBUG_LLM = os.getenv("DEBUG_LLM", "0") == "1"

# Services (name, price, minutes)
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

# simple in-memory state
STATE = {}  # phone -> dict(state="MENU"/"AWAIT_TIME", service=tuple)

def get_state(phone: str):
    return STATE.get(phone, {"state": "MENU"})

def set_state(phone: str, **kwargs):
    st = get_state(phone)
    st.update(kwargs)
    STATE[phone] = st

def reset_state(phone: str):
    STATE[phone] = {"state": "MENU"}

def menu_text() -> str:
    lines = [
        f"💈 *{BUSINESS_NAME}*",
        "Welcome! Reply with the number or just type the service name."
    ]

    for i, (name, price, mins) in enumerate(SERVICES, start=1):
        lines.append(f"{i}) {name} — £{price} ({mins}m)")
    lines.append("")
    lines.append("Commands: MENU | MY BOOKINGS | CANCEL | RESCHEDULE | BACK")
    lines.append("")
    lines.append("Tip: You can type a full sentence like:")
    lines.append("• Book a skin fade tomorrow at 2pm")
    lines.append("• Can I get a haircut Friday at 2pm?")
    lines.append("")
    lines.append("Type MENU anytime to see this again.")
    return "\n".join(lines)

def normalize_time_text(text: str) -> str:
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

def bookings_text(phone: str) -> str:
    items = list_upcoming(phone, limit=10)
    if not items:
        return "You have no upcoming bookings."
    out = ["📅 *Your bookings:*"]
    for idx, ev in enumerate(items, start=1):
        out.append(f"{idx}) {ev['summary']} @ {ev['start']}")
    out.append("\nReply: CANCEL 1  (or)  RESCHEDULE 1 Friday 3pm")
    return "\n".join(out)

def try_llm_first(text: str, phone: str, profile_name: str):
    """
    LLM-first for sentences (so it works from ANY state).
    Returns (handled: bool, response_text: str)
    """
    # only try LLM if it looks like a sentence
    if len(text.split()) < 3:
        return False, ""

    if DEBUG_LLM:
        print("LLM DEBUG: about to call llm_extract", flush=True)

    llm = llm_extract(text, SERVICE_NAMES, phone=phone)

    if DEBUG_LLM:
        print("LLM DEBUG: llm_extract returned:", llm, flush=True)

    if not llm:
        return False, ""

    intent = (llm.get("intent") or "").lower().strip()

    if intent == "menu":
        reset_state(phone)
        return True, menu_text()

    if intent == "view":
        reset_state(phone)
        return True, bookings_text(phone)

    if intent == "book":
        svc = (llm.get("service") or "").strip().lower()
        when_text = (llm.get("when_text") or "").strip()

        if not svc or svc not in SERVICE_BY_NAME:
            reset_state(phone)
            return True, "Which service would you like?\n\n" + menu_text()

        svc_tuple = SERVICE_BY_NAME[svc]

        if not when_text:
            set_state(phone, state="AWAIT_TIME", service=svc_tuple)
            return True, f"✂️ {svc_tuple[0]}\nWhat day & time?\nExamples: Tomorrow 2pm | Fri 2pm | 10/02 15:30\n\nReply BACK to change service."

        dt = parse_datetime(when_text)
        if not dt:
            set_state(phone, state="AWAIT_TIME", service=svc_tuple)
            return True, f"✂️ {svc_tuple[0]}\nI couldn’t understand the time. Try: Tomorrow 2pm"

        start_dt = dt.astimezone(TZ)
        end_dt = start_dt + timedelta(minutes=svc_tuple[2])

        if not is_free(start_dt, end_dt):
            return True, "❌ That slot is taken. Try another time."

        result = create_booking(
            phone,
            svc_tuple[0],
            start_dt,
            minutes=svc_tuple[2],
            name=profile_name
        )

        link_line = ""
        if isinstance(result, dict) and result.get("link"):
            link_line = f"\n📅 View: {result['link']}"

        reset_state(phone)

        return True, (
            f"✅ Booked *{svc_tuple[0]}* for "
            f"{start_dt.strftime('%a %d %b %I:%M%p')}"
            f"{link_line}"
        )

    if intent in ("cancel", "reschedule"):
        return True, bookings_text(phone)

    return False, ""

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    resp = MessagingResponse()
    msg = resp.message()

    from_number = request.values.get("From", "")
    text = (request.values.get("Body", "") or "").strip()
    text_upper = text.upper().strip()
    text_lower = text.lower()

    # polite replies
    if any(word in text_lower for word in ["thanks", "thank you", "cheers", "nice one", "appreciate"]):
        msg.body("You're very welcome! 😊 If you need anything else, just type MENU.")
        return str(resp)

    if any(word in text_lower for word in ["ok", "okay", "cool", "nice"]):
        msg.body("Perfect 👌 Let me know if you'd like to book anything.")
        return str(resp)

    # quick commands
    if text_upper in ("MENU", "BACK", "START"):
        reset_state(from_number)
        msg.body(menu_text())
        return str(resp)

    if text_upper in ("MY BOOKINGS", "MYBOOKINGS", "BOOKINGS"):
        msg.body(bookings_text(from_number))
        return str(resp)

    # cancel shortcut
    m = re.match(r"^\s*CANCEL\s+(\d+)\s*$", text_upper)
    if m:
        idx = int(m.group(1))
        items = list_upcoming(from_number, limit=10)
        if 1 <= idx <= len(items):
            cancel_booking(items[idx - 1]["id"])
            msg.body("✅ Cancelled.")
        else:
            msg.body("❌ Invalid booking number.")
        return str(resp)

    # LLM first
    profile_name = request.values.get("ProfileName", "")
    handled, out = try_llm_first(text, from_number, profile_name)
    if handled:
        msg.body(out)
        return str(resp)

    # state handling
    st = get_state(from_number)
    state = st.get("state", "MENU")

    # ================= MENU STATE =================
    if state == "MENU":

        svc_tuple = None

        if text in SERVICE_BY_NUM:
            svc_tuple = SERVICE_BY_NUM[text]
        else:
            key = text.lower().strip()
            if key in SERVICE_BY_NAME:
                svc_tuple = SERVICE_BY_NAME[key]

        if svc_tuple:
            set_state(from_number, state="AWAIT_TIME", service=svc_tuple)
            msg.body(
                f"✂️ {svc_tuple[0]}\nWhat day & time?\n"
                f"Examples: Tomorrow 2pm | Fri 2pm | 10/02 15:30\n\n"
                f"Reply BACK to change service."
            )
            return str(resp)

        msg.body(menu_text())
        return str(resp)

    # ================= AWAIT NAME STATE =================
    if state == "AWAIT_NAME":

        svc_tuple = st.get("service")
        pending_start = st.get("pending_start")

        if not svc_tuple or not pending_start:
            reset_state(from_number)
            msg.body(menu_text())
            return str(resp)

        customer_name = text.strip()
        if not customer_name:
            msg.body("Please enter a name for the booking.")
            return str(resp)

        start_dt = datetime.fromisoformat(pending_start)
        end_dt = start_dt + timedelta(minutes=svc_tuple[2])

        result = create_booking(
            from_number,
            svc_tuple[0],
            start_dt,
            minutes=svc_tuple[2],
            name=customer_name,
        )

        link_line = ""
        if isinstance(result, dict) and result.get("link"):
            link_line = f"\n📅 View: {result['link']}"

        reset_state(from_number)

        service_name = svc_tuple[0]
        price = svc_tuple[1]
        duration = svc_tuple[2]

        pretty_date = start_dt.strftime("%A %d %B")
        pretty_time = start_dt.strftime("%I:%M %p")

        confirmation_text = (
            f"💈 *{BUSINESS_NAME}*\n\n"
            f"✅ *Booking Confirmed*\n\n"
            f"✂️ Service: {service_name}\n"
            f"📅 Date: {pretty_date}\n"
            f"⏰ Time: {pretty_time}\n"
            f"👤 Name: {customer_name}\n"
            f"💷 Price: £{price}\n"
            f"⏳ Duration: {duration} mins"
            f"{link_line}\n\n"
            f"You’ll receive a reminder before your appointment.\n\n"
            f"Commands: CANCEL | RESCHEDULE | MENU"
        )

        msg.body(confirmation_text)
        return str(resp)

    # fallback
    msg.body(menu_text())
    return str(resp)
def health():
    return "ok", 200