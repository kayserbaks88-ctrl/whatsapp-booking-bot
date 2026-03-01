import os
import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

import dateparser
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

from calendar_helper import (
    is_time_available,
    next_available_slots,
    create_booking_event,
    list_bookings_for_phone,
    cancel_booking_by_index,
    reschedule_booking_by_index,
)
from llm_helper import llm_extract

load_dotenv()
app = Flask(__name__)

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

PORT = int(os.getenv("PORT", "5000"))
SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

SERVICES = [
    ("Haircut", 18, 45),
    ("Skin Fade", 22, 60),
    ("Shape Up", 12, 30),
    ("Beard Trim", 10, 30),
    ("Hot Towel Shave", 25, 60),
    ("Blow Dry", 20, 45),
    ("Boy's Cut", 15, 45),
    ("Children's Cut", 15, 45),
    ("Ear Waxing", 8, 15),
    ("Eyebrow Trim", 8, 15),
    ("Nose Waxing", 8, 15),
    ("Male Grooming", 20, 45),
    ("Wedding Package", 0, 60),
]

SERVICE_NAMES = [s[0] for s in SERVICES]
SERVICE_BY_INDEX = {str(i + 1): SERVICES[i] for i in range(len(SERVICES))}
SERVICE_BY_NAME = {s[0].lower(): s for s in SERVICES}

ALIASES = {
    "kids cut": "Children's Cut",
    "childrens cut": "Children's Cut",
    "children cut": "Children's Cut",
    "boy cut": "Boy's Cut",
    "boys cut": "Boy's Cut",
    "ear wax": "Ear Waxing",
    "nose wax": "Nose Waxing",
    "eyebrow": "Eyebrow Trim",
    "trim": "Haircut",
}

COMMANDS_LINE = "Commands: MENU | MY BOOKINGS | CANCEL | RESCHEDULE"
STATE = {}  # from_number -> {"step": "...", ...}


def reset_state(from_number: str):
    STATE[from_number] = {"step": "idle"}


def set_service(from_number: str, svc_tuple):
    STATE[from_number] = {"step": "await_time", "service": svc_tuple}


def format_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %d %b %H:%M")


def is_open(dt: datetime) -> bool:
    if dt.astimezone(TZ).strftime("%a").lower()[:3] not in OPEN_DAYS:
        return False
    t = dt.astimezone(TZ).time()
    return (t >= OPEN_TIME) and (t < CLOSE_TIME)


def menu_text() -> str:
    lines = [f"💈 {SHOP_NAME}", "Welcome! Reply with a number or name:\n"]
    blocks = [
        ("Men's Cuts", [1, 2, 3]),
        ("Beard Trimming", [4]),
        ("Hot Towel Shaves", [5]),
        ("Blow Dry", [6]),
        ("Boy's Cuts", [7]),
        ("Children's Cuts", [8]),
        ("Waxing / Extras", [9, 10, 11, 12]),
        ("Packages", [13]),
    ]
    for title, idxs in blocks:
        lines.append(title)
        for i in idxs:
            name, price, _mins = SERVICES[i - 1]
            price_txt = "Ask" if price == 0 else f"£{price}"
            lines.append(f"{i}) {name} — {price_txt}")
        lines.append("")
    lines.append("Hours: Mon–Sat 9am–6pm | Sun Closed\n")
    lines.append(COMMANDS_LINE)
    lines.append("\nTip: you can type a full sentence like:")
    lines.append("• Book a skin fade tomorrow at 2pm")
    lines.append("• Can I get a haircut Friday at 2pm?")
    lines.append("\nType MENU anytime to see this again.")
    return "\n".join(lines)


def normalize_time_text(text: str) -> str:
    t = text.strip()
    t = re.sub(r"\b(\d{1,2})\s*p\b", r"\1pm", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(\d{1,2})\s*a\b", r"\1am", t, flags=re.IGNORECASE)
    return t


def parse_datetime(user_text: str) -> datetime | None:
    raw = normalize_time_text(user_text.lower())
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.now(TZ),
    }
    dt = dateparser.parse(raw, settings=settings)
    if not dt:
        return None
    return dt.astimezone(TZ)


def bookings_text(from_number: str) -> str:
    arr = list_bookings_for_phone(from_number)
    if not arr:
        return "📋 You have no bookings yet.\n\nType MENU to book one."

    lines = ["📋 Your bookings:"]
    for i, b in enumerate(arr, 1):
        lines.append(f"{i}) {b['summary']} — {format_dt(b['start_dt'])}")
    lines.append("\nTo cancel: reply CANCEL (or 'CANCEL 1')")
    lines.append("To reschedule: reply RESCHEDULE (or 'RESCHEDULE 1')")
    return "\n".join(lines)


def _attempt_booking(msg, from_number: str, dt: datetime):
    st = STATE.get(from_number, {"step": "idle"})
    svc_tuple = st.get("service")
    if not svc_tuple:
        msg.body("Type MENU to start a booking.")
        reset_state(from_number)
        return

    service_name, price, minutes = svc_tuple

    if not is_open(dt):
        msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
        return

    if not is_time_available(dt, minutes):
        slots = next_available_slots(dt, minutes, step_min=SLOT_STEP_MINUTES, count=5)
        if slots:
            msg.body("That slot is taken. Next available:\n" + "\n".join([format_dt(s) for s in slots]))
        else:
            msg.body("That slot is taken. Try another time.")
        return

    event_id, link = create_booking_event(dt, minutes, service_name, from_number, price=price)
    price_txt = "Ask" if price == 0 else f"£{price}"

    msg.body(
        f"✅ Booked: {service_name} ({price_txt})\n🗓️ {format_dt(dt)}\n\n"
        + (f"🔗 Calendar link: {link}\n\n" if link else "")
        + "Anything else? Type MENU to book another, or MY BOOKINGS to view."
    )
    reset_state(from_number)


@app.route("/health", methods=["GET"])
def health():
    return "ok", 200


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming = (request.values.get("Body") or "").strip()
    from_number = (request.values.get("From") or "").strip()

    resp = MessagingResponse()
    msg = resp.message()

    if not from_number:
        msg.body("Missing sender.")
        return str(resp)

    if from_number not in STATE:
        reset_state(from_number)

    text = incoming.strip()
    text_upper = text.upper()
    st = STATE.get(from_number, {"step": "idle"})

    # ---- hard commands (always win) ----
    if text_upper in {"MENU", "HELP"}:
        msg.body(menu_text())
        reset_state(from_number)
        return str(resp)

    if text_upper in {"MY BOOKINGS", "MYBOOKINGS", "BOOKINGS", "VIEW"}:
        msg.body(bookings_text(from_number))
        reset_state(from_number)
        return str(resp)

    m_cancel = re.match(r"^CANCEL(?:\s+(\d+))?$", text_upper)
    if m_cancel:
        idx = int(m_cancel.group(1)) if m_cancel.group(1) else 1
        ok, note = cancel_booking_by_index(from_number, idx)
        if ok:
            msg.body("✅ Canceled.\n\n" + bookings_text(from_number))
        else:
            msg.body(f"❌ {note}\n\n" + bookings_text(from_number))
        reset_state(from_number)
        return str(resp)

    m_res = re.match(r"^RESCHEDULE(?:\s+(\d+))?$", text_upper)
    if m_res:
        idx = int(m_res.group(1)) if m_res.group(1) else 1
        STATE[from_number] = {"step": "await_reschedule_time", "booking_index": idx}
        msg.body("🗓️ What new day & time?\nExamples:\n• Tomorrow 2pm\n• Fri 3:15pm\n• 10/02 15:30")
        return str(resp)

    if text_upper == "BACK":
        msg.body(menu_text())
        reset_state(from_number)
        return str(resp)

    # ---- LLM assist (ONLY when idle) ----
    if st.get("step") == "idle":
        llm = llm_extract(text, SERVICE_NAMES)
        if llm and llm.get("intent") and llm["intent"] != "unknown":
            intent = llm["intent"]

            if intent == "menu":
                msg.body(menu_text()); reset_state(from_number); return str(resp)

            if intent == "view":
                msg.body(bookings_text(from_number)); reset_state(from_number); return str(resp)

            if intent == "cancel":
                idx = int(llm.get("booking_index") or 1)
                ok, note = cancel_booking_by_index(from_number, idx)
                msg.body(("✅ Canceled.\n\n" if ok else f"❌ {note}\n\n") + bookings_text(from_number))
                reset_state(from_number)
                return str(resp)

            if intent == "reschedule":
                idx = int(llm.get("booking_index") or 1)
                STATE[from_number] = {"step": "await_reschedule_time", "booking_index": idx}
                msg.body("🗓️ What new day & time?\nExamples:\n• Tomorrow 2pm\n• Fri 3:15pm\n• 10/02 15:30")
                return str(resp)

            if intent == "book":
                svc = (llm.get("service") or "").strip()
                when_text = (llm.get("when_text") or "").strip()

                if svc.lower() == "trim":
                    svc = "Haircut"

                svc_tuple = SERVICE_BY_NAME.get(svc.lower()) if svc else None
                if not svc_tuple and svc:
                    # try alias mapping
                    ali = ALIASES.get(svc.lower())
                    if ali:
                        svc_tuple = SERVICE_BY_NAME.get(ali.lower())

                if svc_tuple:
                    set_service(from_number, svc_tuple)
                    if when_text:
                        dt = parse_datetime(when_text)
                        if not dt:
                            msg.body("I didn’t understand the time. Try: Tomorrow 2pm / Fri 2pm / 10/02 15:30\nReply BACK to change service.")
                            return str(resp)
                        _attempt_booking(msg, from_number, dt)
                        return str(resp)
                    else:
                        msg.body(f"✍️ {svc_tuple[0]}\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Fri 2pm\n• 10/02 15:30\n\nReply BACK to change service.")
                        return str(resp)

    # ---- normal flow: service selection ----
    if st.get("step") == "idle" and text in SERVICE_BY_INDEX:
        svc_tuple = SERVICE_BY_INDEX[text]
        set_service(from_number, svc_tuple)
        msg.body(f"✍️ {svc_tuple[0]}\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Fri 2pm\n• 10/02 15:30\n\nReply BACK to change service.")
        return str(resp)

    if st.get("step") == "idle":
        lowered = text.lower().strip()
        if lowered in SERVICE_BY_NAME:
            svc_tuple = SERVICE_BY_NAME[lowered]
            set_service(from_number, svc_tuple)
            msg.body(f"✍️ {svc_tuple[0]}\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Fri 2pm\n• 10/02 15:30\n\nReply BACK to change service.")
            return str(resp)
        if lowered in ALIASES:
            svc_tuple = SERVICE_BY_NAME[ALIASES[lowered].lower()]
            set_service(from_number, svc_tuple)
            msg.body(f"✍️ {svc_tuple[0]}\nWhat day & time?\nExamples:\n• Tomorrow 2pm\n• Fri 2pm\n• 10/02 15:30\n\nReply BACK to change service.")
            return str(resp)

    # ---- awaiting booking time ----
    if st.get("step") == "await_time":
        dt = parse_datetime(text)
        if not dt:
            msg.body("I didn’t understand the time.\nTry:\n• Tomorrow 2pm\n• Fri 2pm\n• 10/02 15:30\n\nReply BACK to change service.")
            return str(resp)
        _attempt_booking(msg, from_number, dt)
        return str(resp)

    # ---- awaiting reschedule time ----
    if st.get("step") == "await_reschedule_time":
        idx = int(st.get("booking_index") or 1)
        dt = parse_datetime(text)
        if not dt:
            msg.body("I didn’t understand the time.\nTry: Tomorrow 2pm / Fri 2pm / 10/02 15:30")
            return str(resp)

        if not is_open(dt):
            msg.body("That time isn’t within opening hours (Mon–Sat 9–6). Try another time.")
            return str(resp)

        # duration: keep simple default 45 OR infer from summary by matching service name
        bookings = list_bookings_for_phone(from_number)
        if idx < 1 or idx > len(bookings):
            msg.body("Booking number not found.\n\n" + bookings_text(from_number))
            reset_state(from_number)
            return str(resp)

        summary = bookings[idx - 1]["summary"].lower()
        minutes = 45
        for name, _price, mins in SERVICES:
            if name.lower() in summary:
                minutes = mins
                break

        if not is_time_available(dt, minutes):
            slots = next_available_slots(dt, minutes, step_min=SLOT_STEP_MINUTES, count=5)
            if slots:
                msg.body("That slot is taken. Next available:\n" + "\n".join([format_dt(s) for s in slots]))
            else:
                msg.body("That slot is taken. Try another time.")
            return str(resp)

        ok, link = reschedule_booking_by_index(from_number, idx, dt, minutes)
        if ok:
            msg.body(
                f"✅ Rescheduled\n🗓️ {format_dt(dt)}\n\n"
                + (f"🔗 Calendar link: {link}\n\n" if link else "")
                + "Type MENU to book another, or MY BOOKINGS to view."
            )
        else:
            msg.body("Sorry — couldn’t reschedule that. Try again.")
        reset_state(from_number)
        return str(resp)

    msg.body("Type MENU to see services, or say: Book haircut Friday 2pm.")
    reset_state(from_number)
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)