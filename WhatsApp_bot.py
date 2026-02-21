import os
import re
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

import dateparser
import requests
from dotenv import load_dotenv
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

from calendar_helper import (
    is_time_available,
    next_available_slots,
    create_booking_event,
)

load_dotenv()

# =========================
# CONFIG
# =========================
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")
TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

PORT = int(os.getenv("PORT", "5000"))
SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))

OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)
OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}  # sun closed

# OpenAI (optional)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1").strip()  # set to gpt-4.1
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20"))

# =========================
# SERVICES (Menu)
# =========================
# You can expand this later to match your full Meta “services list”.
# For now, this is your working menu (as in your screenshots).
SERVICES = [
    # (display_name, price, minutes, aliases)
    ("Haircut", 18, 45, ["haircut", "hair cut", "cut", "mens cut", "men cut", "hair cutt"]),
    ("Skin Fade", 22, 60, ["skin fade", "fade", "skinfade"]),
    ("Shape Up", 12, 20, ["shape up", "shapeup", "line up", "lineup"]),
    ("Beard Trim", 10, 20, ["beard trim", "beard", "trim beard"]),
    ("Kids Cut", 15, 45, ["kids cut", "kid cut", "children cut", "child cut", "boys cut"]),
]

SERVICE_BY_NUMBER = {str(i + 1): SERVICES[i] for i in range(len(SERVICES))}

THANKS_PAT = re.compile(r"\b(thanks|thank you|cheers|nice one|legend|ta)\b", re.I)
HI_PAT = re.compile(r"^(hi|hello|hey|yo|hiya)\b", re.I)
MENU_PAT = re.compile(r"^(menu|start|home)$", re.I)
BACK_PAT = re.compile(r"^(back|cancel)$", re.I)


# =========================
# STATE
# =========================
@dataclass
class UserState:
    step: str = "menu"  # menu | awaiting_time | offering_slots
    service_name: Optional[str] = None
    service_minutes: int = 45
    offered_slots: List[str] = field(default_factory=list)  # iso strings for offered slots
    last_prompt: Optional[str] = None


STATE: Dict[str, UserState] = {}


def get_state(user: str) -> UserState:
    if user not in STATE:
        STATE[user] = UserState()
    return STATE[user]


# =========================
# HELPERS
# =========================
def is_open_day(dt: datetime) -> bool:
    dow = dt.strftime("%a").lower()[:3]  # mon, tue...
    return dow in OPEN_DAYS


def within_open_hours(dt: datetime, minutes: int) -> bool:
    # Ensure tz-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    if not is_open_day(dt):
        return False

    start_ok = dt.time() >= OPEN_TIME
    end_time = (dt + timedelta(minutes=minutes)).time()
    end_ok = end_time <= CLOSE_TIME
    return start_ok and end_ok


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %d %b %H:%M")


def menu_text() -> str:
    lines = []
    lines.append(f"💈 *{SHOP_NAME}*")
    lines.append("Welcome! Reply with a *number* or *name*:\n")
    lines.append("*Men’s Cuts*")
    for i, (name, price, mins, _) in enumerate(SERVICES, start=1):
        lines.append(f"{i}) {name} — £{price}")
    lines.append("")
    lines.append("Hours: *Mon–Sat 9am–6pm* | Sun Closed")
    lines.append("")
    lines.append("Tip: you can type a full sentence like:")
    lines.append("• Book a skin fade *tomorrow at 2pm*")
    lines.append("• Can I get a haircut *Friday at 2pm?*")
    lines.append("")
    lines.append("Type *MENU* anytime to see this again.")
    return "\n".join(lines)


def parse_service(text: str) -> Optional[Tuple[str, int]]:
    t = text.strip().lower()

    # number selection
    if t in SERVICE_BY_NUMBER:
        name, price, mins, _ = SERVICE_BY_NUMBER[t]
        return name, mins

    # name / alias match
    for (name, price, mins, aliases) in SERVICES:
        if name.lower() in t:
            return name, mins
        for a in aliases:
            if a in t:
                return name, mins

    return None


def parse_datetime(text: str) -> Optional[datetime]:
    """
    Uses dateparser with UK timezone.
    Accepts: "tomorrow 2pm", "Fri 14:45", "10/02 15:30", "today at 5"
    """
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }
    dt = dateparser.parse(text, settings=settings)
    if not dt:
        return None
    return dt.astimezone(TZ)


def call_llm_extract(text: str) -> Optional[dict]:
    """
    Optional LLM extraction:
    Returns dict like {"intent":"book","service":"Haircut","datetime_text":"tomorrow 2pm"}
    If OpenAI not configured, returns None.
    """
    if not OPENAI_API_KEY:
        return None

    system = (
        "You extract booking intent for a barbershop WhatsApp bot.\n"
        "Return STRICT JSON only. No markdown.\n"
        "Schema:\n"
        "{"
        '"intent":"book|menu|greeting|thanks|other",'
        '"service":"Haircut|Skin Fade|Shape Up|Beard Trim|Kids Cut|null",'
        '"datetime_text":"<natural language datetime phrase or null>"'
        "}\n"
        "If user just says a number 1-5, that is likely a service selection.\n"
        "If user says thanks/thank you, intent=thanks.\n"
        "If unclear, intent=other.\n"
    )

    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=OPENAI_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        out = json.loads(content)
        if isinstance(out, dict):
            return out
        return None
    except Exception:
        # If LLM fails, just continue with rule-based flow.
        return None


def build_offers_text(slots: List[datetime]) -> str:
    lines = []
    lines.append("❌ That time is taken. Next available:")
    for i, s in enumerate(slots, start=1):
        lines.append(f"{i}) {fmt_dt(s)}")
    lines.append("")
    lines.append("Reply with *1/2/3* or the time (e.g. *09:15* or *Tomorrow 9am*).")
    lines.append("Reply *BACK* to change service.")
    return "\n".join(lines)


def pick_offer_from_reply(st: UserState, text: str) -> Optional[datetime]:
    t = text.strip().lower()

    # If user replies 1/2/3 choose offered slot
    if t in {"1", "2", "3"}:
        idx = int(t) - 1
        if 0 <= idx < len(st.offered_slots):
            return datetime.fromisoformat(st.offered_slots[idx]).astimezone(TZ)

    # If user types a time/date, parse it and accept if it matches one of offered (same minute)
    dt = parse_datetime(text)
    if dt:
        for iso in st.offered_slots:
            offered_dt = datetime.fromisoformat(iso).astimezone(TZ)
            if offered_dt.replace(second=0, microsecond=0) == dt.replace(second=0, microsecond=0):
                return offered_dt
    return None


# =========================
# BOOKING FLOW
# =========================
def handle_message(from_number: str, body: str) -> str:
    st = get_state(from_number)
    text = (body or "").strip()

    if not text:
        return menu_text()

    # quick commands
    if MENU_PAT.match(text):
        st.step = "menu"
        st.service_name = None
        st.offered_slots = []
        return menu_text()

    if BACK_PAT.match(text):
        st.step = "menu"
        st.service_name = None
        st.offered_slots = []
        return "✅ No problem. Back to menu.\n\n" + menu_text()

    # polite handling (do NOT reset flow)
    if THANKS_PAT.search(text):
        if st.step in {"awaiting_time", "offering_slots"}:
            return "🙏 You’re welcome! Just send the day & time when you’re ready (e.g. *Tomorrow 2pm*)."
        return "🙏 You’re welcome! Type *MENU* to book another appointment."

    if HI_PAT.match(text) and st.step == "menu":
        return menu_text()

    # Optional LLM extract (helps with full sentences)
    llm = call_llm_extract(text)

    # If LLM says menu/greeting/thanks handle
    if llm and llm.get("intent") == "menu":
        st.step = "menu"
        st.service_name = None
        st.offered_slots = []
        return menu_text()

    if llm and llm.get("intent") == "greeting" and st.step == "menu":
        return menu_text()

    if llm and llm.get("intent") == "thanks":
        return "🙏 You’re welcome! Type *MENU* to book another appointment."

    # Step 1: If we’re in menu, accept service selection (number or name) OR full sentence booking
    if st.step == "menu":
        # service by rules
        svc = parse_service(text)
        svc_name = None
        svc_mins = None

        # service by LLM (if available)
        if llm and llm.get("service"):
            svc_name = llm["service"]
            # map to minutes
            for (n, _, mins, _) in SERVICES:
                if n.lower() == str(svc_name).lower():
                    svc_mins = mins
                    break

        if svc:
            svc_name, svc_mins = svc

        # If we found a service, store it
        if svc_name and svc_mins:
            st.service_name = svc_name
            st.service_minutes = svc_mins

            # If message also contains datetime, try book immediately
            dt_text = None
            if llm and llm.get("datetime_text"):
                dt_text = llm["datetime_text"]
            else:
                dt_text = text  # try parse from full text anyway

            dt = parse_datetime(dt_text)
            if dt:
                return attempt_booking(from_number, st, dt)

            st.step = "awaiting_time"
            return (
                f"✍️ *{st.service_name}*\n\n"
                "What day & time?\nExamples:\n"
                "• Tomorrow 2pm\n"
                "• Fri 2pm\n"
                "• 10/02 15:30\n\n"
                "Reply *BACK* to change service."
            )

        # No service found → maybe they wrote a full sentence but service missing
        return (
            "I can help you book. ✅\n\n"
            "Reply with a service *number/name* or type a full sentence like:\n"
            "• Book a haircut tomorrow at 2pm\n\n"
            "Type *MENU* to see services."
        )

    # Step 2: awaiting time
    if st.step == "awaiting_time":
        # If user accidentally types a service number again, treat it as service selection
        svc = parse_service(text)
        if svc:
            st.service_name, st.service_minutes = svc
            return (
                f"✍️ *{st.service_name}*\n\n"
                "What day & time?\nExamples:\n"
                "• Tomorrow 2pm\n"
                "• Fri 2pm\n"
                "• 10/02 15:30\n\n"
                "Reply *BACK* to change service."
            )

        dt = parse_datetime(text)
        if not dt:
            return (
                "I didn’t understand the time.\nTry:\n"
                "• Tomorrow 2pm\n"
                "• Mon 3:15pm\n"
                "• 10/02 15:30\n\n"
                "Reply *BACK* to change service."
            )

        return attempt_booking(from_number, st, dt)

    # Step 3: offering slots
    if st.step == "offering_slots":
        chosen = pick_offer_from_reply(st, text)
        if chosen:
            return attempt_booking(from_number, st, chosen)

        # If user typed a service number/name while in offering slots, interpret as change service (nice UX)
        svc = parse_service(text)
        if svc:
            st.service_name, st.service_minutes = svc
            st.step = "awaiting_time"
            st.offered_slots = []
            return (
                f"✍️ *{st.service_name}*\n\n"
                "What day & time?\nExamples:\n"
                "• Tomorrow 2pm\n"
                "• Fri 2pm\n"
                "• 10/02 15:30\n\n"
                "Reply *BACK* to change service."
            )

        return "Reply with *1/2/3* or type one of the offered times (e.g. *Tomorrow 9am*)."

    # Fallback
    st.step = "menu"
    return menu_text()


def attempt_booking(from_number: str, st: UserState, dt: datetime) -> str:
    if not st.service_name:
        st.step = "menu"
        return "Please choose a service first.\n\n" + menu_text()

    # Opening hours check
    if not within_open_hours(dt, st.service_minutes):
        return "That time isn’t within opening hours (Mon–Sat 9–6). Try another time."

    # Availability check
    if is_time_available(dt, st.service_minutes):
        end_dt = dt + timedelta(minutes=st.service_minutes)
        summary = f"{st.service_name} - WhatsApp Booking"
        description = f"Booked via {BUSINESS_NAME}"
        create_booking_event(
            start_dt=dt,
            end_dt=end_dt,
            summary=summary,
            description=description,
            from_number=from_number,
            service_name=st.service_name,
        )

        # Reset to idle/menu-like state after booking
        st.step = "menu"
        st.offered_slots = []
        svc = st.service_name
        st.service_name = None

        return (
            f"✅ Booked: *{svc}*\n🗓️ {fmt_dt(dt)}\n\n"
            "Anything else? Type *MENU* to book another."
        )

    # Not available → offer next slots
    slots = next_available_slots(
        desired_dt=dt,
        minutes=st.service_minutes,
        slot_step_minutes=SLOT_STEP_MINUTES,
        max_slots=3,
        search_hours_ahead=48,
    )
    if not slots:
        st.step = "awaiting_time"
        return "Sorry, nothing available soon. Try another day/time."

    st.step = "offering_slots"
    st.offered_slots = [s.isoformat() for s in slots]
    return build_offers_text(slots)


# =========================
# FLASK APP
# =========================
app = Flask(__name__)


@app.get("/")
def health():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp_webhook():
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "")

    reply = handle_message(from_number, body)

    resp = MessagingResponse()
    msg = resp.message()
    msg.body(reply)
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)