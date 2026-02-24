import os
import re
import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

import dateparser
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
from openai import OpenAI

from calendar_helper import (
    add_booking,
    list_bookings,
    cancel_booking,
    reschedule_booking,
    find_next_booking,
)

load_dotenv()

# ---------------- CONFIG ----------------
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

PORT = int(os.getenv("PORT", "5000"))

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

ENABLE_LLM = os.getenv("ENABLE_LLM", "0") == "1"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
DEBUG_LLM = os.getenv("DEBUG_LLM", "0") == "1"

llm_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ---------------- MENU / SERVICES ----------------
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
# {
#   "step": "idle" | "await_time" | "cancel_pick" | "resched_pick" | "resched_time",
#   "service": svc_tuple or None,
#   "pending_bookings": None or list,
#   "resched_booking_id": None or str
# }


def st(from_number: str):
    if from_number not in STATE:
        STATE[from_number] = {
            "step": "idle",
            "service": None,
            "pending_bookings": None,
            "resched_booking_id": None,
        }
    return STATE[from_number]


def reset(from_number: str):
    STATE[from_number] = {
        "step": "idle",
        "service": None,
        "pending_bookings": None,
        "resched_booking_id": None,
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
    lines.append("Tip: you can type a sentence like:")
    lines.append("• Book a skin fade *tomorrow at 2pm*")
    lines.append("• Can I get a haircut *Friday at 2pm*?\n")
    lines.append("Commands: *MENU* | *MY BOOKINGS* | *CANCEL* | *RESCHEDULE*")
    return "\n".join(lines).strip()


def parse_service(text: str):
    t = text.lower().strip()

    # Allow "1", "1)", "1.", "1 -"
    m = re.match(r"^(\d+)\D*$", t)
    if m:
        idx_str = m.group(1)
        if idx_str in SERVICE_BY_INDEX:
            return SERVICE_BY_INDEX[idx_str]

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
    t = re.sub(r"\b(\d{1,2})\s*p\b", r"\1pm", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(\d{1,2})\s*a\b", r"\1am", t, flags=re.IGNORECASE)
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


# ---------------- LLM HELPER ----------------
def call_llm_for_booking(from_number: str, user_message: str):
    if not (ENABLE_LLM and llm_client):
        if DEBUG_LLM:
            print("LLM disabled or client missing")
        return None

    known_services = ", ".join(sorted({s[0] for s in SERVICES}))

    system_prompt = f"""
You are an assistant for a barber shop called {SHOP_NAME} (powered by {BUSINESS_NAME}).
The user is chatting via WhatsApp. You ONLY help with booking, rescheduling,
cancelling or checking availability for services.

You MUST respond with STRICT JSON ONLY (no extra words, no markdown), in this schema:

{{
  "assistant_reply": "string - short WhatsApp reply in English",
  "intent": "smalltalk" | "create_booking" | "cancel_booking" | "reschedule_booking" | "check_availability" | "unknown",
  "service": "one of: {known_services} or null",
  "datetime_text": "string with what you understood as date/time or null",
  "needs_service": true | false,
  "needs_datetime": true | false
}}
    """

    user_prompt = f"""
User phone: {from_number}
User message: "{user_message}"
"""

    try:
        completion = llm_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.3,
            messages=[
                {"role": "system", "content": system_prompt.strip()},
                {"role": "user", "content": user_prompt.strip()},
            ],
        )
        raw = completion.choices[0].message.content.strip()
        if DEBUG_LLM:
            print("LLM raw response:", raw)
        parsed = json.loads(raw)
        if DEBUG_LLM:
            print("LLM parsed JSON:", parsed)
        return parsed
    except Exception as e:
        print("LLM error:", e)
        return None


# ---------------- APP ----------------
app = Flask(__name__)


@app.route("/", methods=["GET"])
def index():
    return "Barber bot is running", 200


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

    # BACK
    if low == "back":
        reset(from_number)
        msg.body(menu_text())
        return str(resp)

    # MY BOOKINGS
    if low in {"my bookings", "my booking", "bookings", "view bookings"}:
        reset(from_number)
        bookings = list_bookings(from_number)
        if not bookings:
            msg.body("You don’t have any upcoming bookings. Type *MENU* to book.")
            return str(resp)

        lines = ["📅 *Your bookings:*"]
        for i, b in enumerate(bookings, start=1):
            lines.append(f"{i}) {b['service']} — {fmt_dt(b['start'])}")
        lines.append("\nType *CANCEL* or *RESCHEDULE* to manage one.")
        msg.body("\n".join(lines))
        return str(resp)

    # CANCEL
    if low == "cancel":
        bookings = list_bookings(from_number)
        if not bookings:
            msg.body("No upcoming bookings to cancel. Type *MENU* to book.")
            return str(resp)

        s["step"] = "cancel_pick"
        s["pending_bookings"] = bookings

        lines = ["❌ *Cancel which booking?* Reply with the *number*:"]
        for i, b in enumerate(bookings, start=1):
            lines.append(f"{i}) {b['service']} — {fmt_dt(b['start'])}")
        msg.body("\n".join(lines))
        return str(resp)

    if s["step"] == "cancel_pick":
        if body.isdigit():
            idx = int(body)
            bookings = s.get("pending_bookings") or []
            if 1 <= idx <= len(bookings):
                chosen = bookings[idx - 1]
                removed = cancel_booking(from_number, chosen["id"])
                if not removed:
                    msg.body("⚠️ Couldn’t cancel that booking. Try *MY BOOKINGS* again.")
                    reset(from_number)
                    return str(resp)

                reset(from_number)
                msg.body(
                    f"✅ Cancelled: *{removed['service']}* — {fmt_dt(removed['start'])}\n\n"
                    "Type *MENU* to book again."
                )
                return str(resp)

        msg.body("Reply with a valid number from the list, or type *MENU*.")
        return str(resp)

    # RESCHEDULE
    if low == "reschedule":
        bookings = list_bookings(from_number)
        if not bookings:
            msg.body("No upcoming bookings to reschedule. Type *MENU* to book.")
            return str(resp)

        s["step"] = "resched_pick"
        s["pending_bookings"] = bookings

        lines = ["🕒 *Reschedule which booking?* Reply with the *number*:"]
        for i, b in enumerate(bookings, start=1):
            lines.append(f"{i}) {b['service']} — {fmt_dt(b['start'])}")
        msg.body("\n".join(lines))
        return str(resp)

    if s["step"] == "resched_pick":
        if body.isdigit():
            idx = int(body)
            bookings = s.get("pending_bookings") or []
            if 1 <= idx <= len(bookings):
                chosen = bookings[idx - 1]
                s["resched_booking_id"] = chosen["id"]
                s["step"] = "resched_time"
                msg.body(
                    f"✍🏽 *{chosen['service']}*\nCurrent: {fmt_dt(chosen['start'])}\n\n"
                    "Send the *new day & time* (e.g. *Tomorrow 3pm*)."
                )
                return str(resp)

        msg.body("Reply with a valid number from the list, or type *MENU*.")
        return str(resp)

    if s["step"] == "resched_time":
        bid = s.get("resched_booking_id")
        if not bid:
            reset(from_number)
            msg.body(menu_text())
            return str(resp)

        dt = parse_dt(body)
        if not dt:
            msg.body("I didn’t understand that time. Try: *Tomorrow 3pm* or *Fri 2pm*.")
            return str(resp)

        if not within_opening(dt):
            msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
            return str(resp)

        updated = reschedule_booking(from_number, bid, dt)
        if not updated:
            msg.body("Couldn’t find that booking anymore. Type *MY BOOKINGS*.")
            reset(from_number)
            return str(resp)

        reset(from_number)
        msg.body(
            f"✅ Rescheduled: *{updated['service']}*\n"
            f"🗓 {fmt_dt(updated['start'])}\n\n"
            f"{friendly_after_booking()}"
        )
        return str(resp)

    # MAIN BOOKING FLOW
    if s["step"] == "idle":
        svc = parse_service(body)

        if svc:
            dt = parse_dt(body)
            if dt:
                if not within_opening(dt):
                    msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
                    return str(resp)

                minutes = int(svc[2])
                created = add_booking(from_number, svc[0], dt, minutes)

                reset(from_number)
                msg.body(
                    f"✅ Booked: *{created['service']}*\n"
                    f"🗓 {fmt_dt(created['start'])}\n\n"
                    f"{friendly_after_booking()}"
                )
                return str(resp)

            s["step"] = "await_time"
            s["service"] = svc
            msg.body(ask_time_for_service(svc))
            return str(resp)

        # LLM fallback
        llm = call_llm_for_booking(from_number, body)
        if llm:
            intent = llm.get("intent")
            assistant_reply = llm.get("assistant_reply") or ""
            service_name = llm.get("service")
            datetime_text = llm.get("datetime_text")
            needs_service = bool(llm.get("needs_service"))
            needs_datetime = bool(llm.get("needs_datetime"))

            if intent == "smalltalk":
                msg.body(assistant_reply or "👋")
                return str(resp)

            svc = None
            if service_name:
                for s_svc in SERVICES:
                    if s_svc[0].lower() == service_name.lower():
                        svc = s_svc
                        break

            if intent in {"create_booking", "check_availability"}:
                if needs_datetime or not datetime_text:
                    if svc:
                        s["step"] = "await_time"
                        s["service"] = svc
                        msg.body(assistant_reply or ask_time_for_service(svc))
                    else:
                        msg.body(
                            assistant_reply
                            or "Which service would you like? For example: *Haircut*, *Skin Fade*, *Beard Trim*."
                        )
                    return str(resp)

                dt = parse_dt(datetime_text)
                if not dt:
                    msg.body("I didn’t quite get the time. Try: *Tomorrow 2pm* or *Fri 2pm*.")
                    return str(resp)

                if not within_opening(dt):
                    msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
                    return str(resp)

                if not svc:
                    msg.body("Got the time, but which service would you like? E.g. *Haircut*, *Skin Fade*, *Beard Trim*.")
                    return str(resp)

                minutes = int(svc[2])
                created = add_booking(from_number, svc[0], dt, minutes)

                reset(from_number)
                msg.body(
                    f"✅ Booked: *{created['service']}*\n"
                    f"🗓 {fmt_dt(created['start'])}\n\n"
                    f"{friendly_after_booking()}"
                )
                return str(resp)

            if intent == "cancel_booking":
                nb = find_next_booking(from_number)
                if not nb:
                    msg.body("I couldn’t find a booking under your number. Type *MY BOOKINGS* to check.")
                else:
                    cancel_booking(from_number, nb["id"])
                    msg.body(
                        assistant_reply
                        or f"Okay, I’ve cancelled your next booking: *{nb['service']}* on {fmt_dt(nb['start'])}."
                    )
                return str(resp)

            if intent == "reschedule_booking":
                nb = find_next_booking(from_number)
                if not nb:
                    msg.body("I couldn’t find a booking under your number. Type *MY BOOKINGS* to check.")
                else:
                    s["step"] = "resched_time"
                    s["resched_booking_id"] = nb["id"]
                    msg.body(
                        assistant_reply
                        or f"Your next booking is *{nb['service']}* on {fmt_dt(nb['start'])}.\n"
                        "Send the *new day & time* (e.g. *Tomorrow 3pm*)."
                    )
                return str(resp)

        msg.body("Type *MENU* to see services, or say something like: *Book haircut tomorrow 2pm*.")
        return str(resp)

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
        created = add_booking(from_number, svc[0], dt, minutes)

        reset(from_number)
        msg.body(
            f"✅ Booked: *{created['service']}*\n"
            f"🗓 {fmt_dt(created['start'])}\n\n"
            f"{friendly_after_booking()}"
        )
        return str(resp)

    msg.body("Type *MENU* to see options, or *HELP*.")
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
