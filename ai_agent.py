import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

SHOP_NAME = os.getenv("BARBERSHOP_NAME", "BBC Barbers")
PORT = int(os.getenv("PORT", "5000"))
TZ_NAME = os.getenv("TIMEZONE", "Europe/London")
TZ = ZoneInfo(TZ_NAME)

# -----------------------------
# In-memory storage (Stage 1)
# -----------------------------
appointments = {}  # { "YYYY-MM-DD HH:MM": {"from": "...", "service": "..."} }
user_state = {}    # { "+44...": {"pending": {...}, "chosen_service": "..."} }

SERVICES = {
    "skin fade": "SKIN FADE",
    "haircut": "HAIRCUT",
    "beard": "BEARD",
}


# -----------------------------
# Helpers
# -----------------------------
def now_local() -> datetime:
    return datetime.now(TZ)


def clean_message(text: str) -> str:
    """
    Normalise messy human text into something parseable
    WITHOUT breaking words like 'sunday'.
    """
    if not text:
        return ""
    t = text.lower().strip()

    # remove punctuation (keep : for times)
    t = re.sub(r"[,\.\!\?\(\)\[\]\{\}]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    # remove filler phrases safely using word boundaries
    filler_patterns = [
        r"\bbro\b", r"\bpls\b", r"\bplease\b",
        r"\bcan i\b", r"\bcould i\b", r"\bcan you\b",
        r"\bi need\b", r"\bi want\b", r"\bi would like\b",
        r"\bany chance\b", r"\bhey\b", r"\bhi\b", r"\bhello\b",
        r"\bget me\b", r"\bget a\b", r"\bbook me\b", r"\bbook\b", r"\bfor me\b"
    ]
    for pat in filler_patterns:
        t = re.sub(pat, " ", t)

    t = re.sub(r"\s+", " ", t).strip()

    # service synonyms (whole words)
    service_patterns = [
        (r"\bbeard\s*trim\b", "beard"),
        (r"\btrim\b", "haircut"),
        (r"\bhair\s*cut\b", "haircut"),
        (r"\bcut\b", "haircut"),
        (r"\bline\s*up\b", "haircut"),
        (r"\bshape\s*up\b", "haircut"),
        (r"\bskinfade\b", "skin fade"),
        (r"\bfade\b", "skin fade"),
    ]
    for pat, repl in service_patterns:
        t = re.sub(pat, repl, t)

    # vague time words -> default times
    time_patterns = [
        (r"\bmorning\b", "10am"),
        (r"\bmidday\b", "12pm"),
        (r"\bnoon\b", "12pm"),
        (r"\bafternoon\b", "2pm"),
        (r"\bevening\b", "6pm"),
        (r"\btonight\b", "7pm"),
        (r"\bnight\b", "7pm"),
    ]
    for pat, repl in time_patterns:
        t = re.sub(pat, repl, t)

    t = re.sub(r"\s+", " ", t).strip()
    return t


def parse_service(text: str) -> str | None:
    for key in SERVICES.keys():
        if re.search(rf"\b{re.escape(key)}\b", text):
            return key
    # allow direct menu words
    if text.strip() in ["skinfade", "skin fade"]:
        return "skin fade"
    if text.strip() == "haircut":
        return "haircut"
    if text.strip() == "beard":
        return "beard"
    return None


def parse_time(text: str) -> tuple[int, int] | None:
    t = text.replace(" ", "")

    # 17:30 format
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 5pm / 5:30pm format
    m = re.search(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?(am|pm)\b", t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or "0")
        ampm = m.group(3)
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        return hour, minute

    return None


def next_date_for_word(word: str) -> datetime | None:
    base = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    w = word.lower().strip()

    if w == "today":
        return base
    if w == "tomorrow":
        return base + timedelta(days=1)

    weekdays = {
        "monday": 0, "mon": 0,
        "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2,
        "thursday": 3, "thu": 3, "thurs": 3,
        "friday": 4, "fri": 4,
        "saturday": 5, "sat": 5,
        "sunday": 6, "sun": 6,
    }
    if w not in weekdays:
        return None

    target = weekdays[w]
    days_ahead = (target - base.weekday()) % 7
    return base + timedelta(days=days_ahead)


def parse_date(text: str) -> datetime | None:
    for tok in text.split():
        d = next_date_for_word(tok)
        if d:
            return d
    return None


def format_dt(dt: datetime) -> str:
    # e.g. "Sunday 07 Jan at 6pm"
    s = dt.strftime("%A %d %b at %-I:%M%p").replace(":00", "")
    return s


def slot_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def is_slot_taken(dt: datetime) -> bool:
    return slot_key(dt) in appointments


def make_menu() -> str:
    return (
        f"👋 Welcome to {SHOP_NAME}!\n\n"
        "How would you like to book?\n\n"
        "Reply with one of these:\n"
        "✂️ SKIN FADE\n"
        "💈 HAIRCUT\n"
        "🧔 BEARD\n\n"
        "Or simply type your booking like this:\n"
        "Skin fade Sunday 5pm\n\n"
        "📍 Walk-ins & bookings available\n"
        "🕘 Open 7 days a week"
    )


def build_confirm(service_key: str, dt: datetime) -> str:
    nice_service = SERVICES[service_key].title()
    return (
        f"✅ I’ve got: *{nice_service}* — *{format_dt(dt)}*\n\n"
        "Reply *YES* to confirm, or type a new time/day to change it."
    )


def try_extract_booking(text: str) -> dict | None:
    """
    Returns:
      None if message isn't about booking
      {"incomplete": True, ...} if partial
      {"service": ..., "dt": ...} if complete
    """
    service_key = parse_service(text)
    date_base = parse_date(text)
    tm = parse_time(text)

    if not service_key and not date_base and not tm:
        return None

    if not service_key or not date_base or not tm:
        return {"incomplete": True, "service": service_key, "date": date_base, "time": tm}

    hour, minute = tm
    dt = date_base.replace(hour=hour, minute=minute)

    # If in the past and user used weekday word, bump by 7 days
    if dt < now_local() and re.search(
        r"\b(mon|tue|tues|wed|thu|thurs|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text
    ):
        dt = dt + timedelta(days=7)

    return {"service": service_key, "dt": dt}


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def health():
    return {"ok": True, "service": SHOP_NAME, "time": now_local().isoformat()}


@app.post("/whatsapp")
def whatsapp_webhook():
    resp = MessagingResponse()
    msg = resp.message()

    from_number = request.values.get("From", "")
    raw_body = request.values.get("Body", "")
    body = clean_message(raw_body)

    print("\n--- INCOMING ---")
    print("FROM :", from_number)
    print("RAW  :", raw_body)
    print("CLEAN:", body)

    if from_number not in user_state:
        user_state[from_number] = {}

    # 1) confirmation
    if body.strip() in ["yes", "y", "confirm", "yeah", "yep"]:
        pending = user_state[from_number].get("pending")
        if not pending:
            msg.body("No booking waiting to confirm.\n\n" + make_menu())
            return str(resp)

        dt = pending["dt"]
        service_key = pending["service"]

        if is_slot_taken(dt):
            msg.body("⚠️ Sorry, that slot has just been taken. Please choose another time.\n\nExample: Haircut Sunday 7pm")
            user_state[from_number].pop("pending", None)
            return str(resp)

        appointments[slot_key(dt)] = {"from": from_number, "service": service_key}
        user_state[from_number].pop("pending", None)

        msg.body(f"✅ *Booked:* {SERVICES[service_key].title()} — *{format_dt(dt)}*")
        return str(resp)

    # 2) menu option only
    if body.strip() in ["skin fade", "skinfade", "haircut", "beard"]:
        service_key = "skin fade" if body.strip() in ["skin fade", "skinfade"] else body.strip()
        user_state[from_number]["chosen_service"] = service_key
        msg.body(f"Nice — *{SERVICES[service_key].title()}* ✅\nNow send a day + time.\n\nExample: Sunday 5pm")
        return str(resp)

    # 3) attempt parse booking
    booking = try_extract_booking(body)
    if booking:
        if booking.get("incomplete"):
            chosen = user_state[from_number].get("chosen_service")
            service_key = booking.get("service") or chosen
            date_base = booking.get("date")
            tm = booking.get("time")

            if service_key and date_base and tm:
                hour, minute = tm
                dt = date_base.replace(hour=hour, minute=minute)
                booking = {"service": service_key, "dt": dt}
            else:
                missing = []
                if not service_key:
                    missing.append("service (skin fade / haircut / beard)")
                if not date_base:
                    missing.append("day (e.g., Sunday / tomorrow)")
                if not tm:
                    missing.append("time (e.g., 5pm)")

                msg.body(
                    "I can help — I just need: " + ", ".join(missing) +
                    "\n\nExample: Haircut Sunday 6pm"
                )
                return str(resp)

        service_key = booking["service"]
        dt = booking["dt"]

        if is_slot_taken(dt):
            msg.body("⚠️ That time is already booked. Try another slot.\n\nExample: Sunday 7pm")
            return str(resp)

        # Save pending confirmation
        user_state[from_number]["pending"] = {"service": service_key, "dt": dt}
        msg.body(build_confirm(service_key, dt))
        return str(resp)

    # 4) fallback menu
    msg.body(make_menu())
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
