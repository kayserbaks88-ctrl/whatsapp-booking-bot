import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import dateparser
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

from calendar_helper import create_booking_event

load_dotenv()

# ----------------------------
# Config
# ----------------------------
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "BBC Barbers")
TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))

SERVICES = ["Haircut", "Skin Fade", "Beard Trim", "Kids Cut", "Shape Up"]
SERVICE_ALIASES = {
    "kids cut": "Kids Cut",
    "kids": "Kids Cut",

    "skin fade": "Skin Fade",
    "fade": "Skin Fade",

    "beard trim": "Beard Trim",
    "beard": "Beard Trim",

    "shape up": "Shape Up",
    "line up": "Shape Up",

    "haircut": "Haircut",
    "cut": "Haircut",   # keep this LAST
}

# ----------------------------
# In-memory session state
# ----------------------------
# Each phone number maps to a dict like:
# { "state": "...", "service": "...", "when": datetime, "name": "..." }
user_sessions = {}

app = Flask(__name__)


# ----------------------------
# Helpers
# ----------------------------
def norm(text: str) -> str:
    """Normalize text safely (emoji-safe)."""
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def detect_service(text):
    t = text.lower()

    for key in sorted(SERVICE_ALIASES.keys(), key=len, reverse=True):
        if key in t:
            return SERVICE_ALIASES[key]
    return None

def extract_service(text: str):
    return detect_service(text)

def is_yes(text: str) -> bool:
    t = norm(text)
    return t in {"yes", "y", "yeah", "yep", "ok", "okay", "confirm", "sure", "go ahead"}


def is_no(text: str) -> bool:
    t = norm(text)
    return t in {"no", "n", "nope", "nah", "cancel"}


def is_change(text: str) -> bool:
    t = norm(text)
    return t.startswith("change") or t in {"edit", "modify"}


def is_reset(text: str) -> bool:
    t = norm(text)
    return t in {"reset", "restart", "start over", "start", "menu"}


def try_extract_name(text: str):
    """
    Accepts:
    - "my name is John"
    - "i am John"
    - "John"
    - "John Smith"
    """
    raw = (text or "").strip()
    t = norm(raw)

    # common patterns
    patterns = [
        r"^my name is ([a-zA-Z][a-zA-Z\s'\-]{1,40})$",
        r"^i am ([a-zA-Z][a-zA-Z\s'\-]{1,40})$",
        r"^im ([a-zA-Z][a-zA-Z\s'\-]{1,40})$",
        r"^it's ([a-zA-Z][a-zA-Z\s'\-]{1,40})$",
        r"^its ([a-zA-Z][a-zA-Z\s'\-]{1,40})$",
    ]
    for p in patterns:
        m = re.match(p, t)
        if m:
            name = m.group(1).strip()
            return name.title()

    # if it's just a couple words and contains letters, treat as name
    if re.match(r"^[a-zA-Z][a-zA-Z\s'\-]{1,40}$", raw) and len(raw.split()) <= 3:
        return raw.strip().title()

    return None


def parse_datetime_flexible(text: str):
    """
    Parse date/time from free text like:
    - tomorrow 3pm
    - friday at 1
    - 6 jan 16:00
    """
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now,
    }

    dt = dateparser.parse(text, settings=settings)
    if not dt:
        return None

    # normalize timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def fmt_when(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(TIMEZONE)).strftime("%a %d %b at %I:%M %p")


def welcome_menu() -> str:
    return (
        f"👋 Welcome to {BUSINESS_NAME}!\n"
        f"What service would you like?\n\n"
        f"✂️ Haircut\n"
        f"💈 Skin Fade\n"
        f"🧔 Beard Trim\n"
        f"👶 Kids Cut\n"
        f"📏 Shape Up\n\n"
        f"Type: Haircut / Skin Fade / Beard Trim / Kids Cut / Shape Up"
    )


def get_session(phone: str) -> dict:
    if phone not in user_sessions:
        user_sessions[phone] = {"state": "awaiting_service"}
    return user_sessions[phone]


def reset_session(phone: str):
    user_sessions[phone] = {"state": "awaiting_service"}


# ----------------------------
# Main handler
# ----------------------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming_msg = request.values.get("Body", "") or ""
    phone = request.values.get("From", "") or ""

    session = get_session(phone)
    state = session.get("state", "awaiting_service")

    msg_norm = norm(incoming_msg)

    # Global commands
    if is_reset(msg_norm):
        reset_session(phone)
        resp = MessagingResponse()
        resp.message("✅ Reset done.\n\n" + welcome_menu())
        return str(resp)

    # Allow "change" at any time (puts them back to choosing what to change)
    if is_change(msg_norm):
        session["state"] = "awaiting_change_field"
        resp = MessagingResponse()
        resp.message(
            "No problem — what would you like to change?\n"
            "Type: service / time / name"
        )
        return str(resp)

    reply = ""

    # ----------------------------
    # State: awaiting_service
    # ----------------------------
    if state == "awaiting_service":
        service = extract_service(incoming_msg)
        if service:
            session["service"] = service
            session["state"] = "awaiting_time"
            reply = (
                f"✅ {service} selected.\n"
                f"What day & time would you like?\n\n"
                f"Examples:\n"
                f"• tomorrow 3pm\n"
                f"• friday at 1\n"
                f"• 6 jan 16:00"
            )
        else:
            # Don't say "I didn't catch that" for greetings etc — just show menu.
            reply = welcome_menu()

    # ----------------------------
    # State: awaiting_time
    # ----------------------------
    elif state == "awaiting_time":
        dt = parse_datetime_flexible(incoming_msg)
        if dt:
            session["when"] = dt
            session["state"] = "awaiting_name"
            reply = (
                f"✅ Got it: {fmt_when(dt)}.\n"
                f"What's your name?"
            )
        else:
            reply = (
                "I didn’t catch the time 🤔\n"
                "Try:\n"
                "• tomorrow 3pm\n"
                "• friday at 1\n"
                "• 6 jan 16:00\n\n"
                "Or type 'reset' to start over."
            )

    # ----------------------------
    # State: awaiting_name
    # ----------------------------
    elif state == "awaiting_name":
        name = try_extract_name(incoming_msg)
        if name:
            session["name"] = name
            session["state"] = "awaiting_confirm"
            service = session.get("service", "Service")
            when = session.get("when")
            reply = (
                "Please confirm your booking:\n\n"
                f"• Service: {service}\n"
                f"• Time: {fmt_when(when)}\n"
                f"• Name: {name}\n\n"
                "Reply YES to confirm or NO to cancel.\n"
                "You can also type CHANGE to edit."
            )
        else:
            reply = "What name should I put the booking under? (e.g. John or John Smith)"

    # ----------------------------
    # State: awaiting_confirm
    # ----------------------------
    elif state == "awaiting_confirm":
        if is_yes(msg_norm):
            service = session.get("service", "Service")
            when = session.get("when")
            name = session.get("name", "Customer")

            ok, message, link = create_booking_event(
                service_name=service,
                when=when,
                name=name,
                phone=phone,
                calendar_id=CALENDAR_ID,
                timezone_hint=TIMEZONE,
            )

            if ok:
                reply = f"✅ Confirmed! You’re booked for {fmt_when(when)}.\nSee you soon, {name}!"
                if link:
                    reply += f"\n\n📅 Calendar link:\n{link}"
            else:
                reply = (
                    "⚠️ I couldn’t create the booking in the calendar.\n"
                    f"Reason: {message}\n\n"
                    "Type RESET to try again."
                )

            # End the session cleanly so it doesn't loop:
            reset_session(phone)

        elif is_no(msg_norm):
            reply = "❌ No worries — booking cancelled.\n\n" + welcome_menu()
            reset_session(phone)
        else:
            reply = "Please reply YES to confirm or NO to cancel. (Or type CHANGE to edit.)"

    # ----------------------------
    # State: awaiting_change_field
    # ----------------------------
    elif state == "awaiting_change_field":
        t = msg_norm
        if "service" in t:
            session["state"] = "awaiting_service"
            reply = "Sure — what service would you like?\n\n" + welcome_menu()
        elif "time" in t or "date" in t:
            session["state"] = "awaiting_time"
            reply = (
                "Sure — what day & time would you like?\n"
                "Examples:\n• tomorrow 3pm\n• friday at 1\n• 6 jan 16:00"
            )
        elif "name" in t:
            session["state"] = "awaiting_name"
            reply = "Sure — what’s your name?"
        else:
            reply = "Type: service / time / name"

    else:
        # Failsafe: never crash / never loop weirdly
        reset_session(phone)
        reply = "✅ Let’s start again.\n\n" + welcome_menu()

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
