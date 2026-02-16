import os
import re
import json
import requests
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
)

load_dotenv()

# ---------------- CONFIG ----------------
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))

SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))
BUFFER_MINUTES = int(os.getenv("BUFFER_MINUTES", "0"))  # cleanup buffer

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20"))
DEBUG_LLM = os.getenv("DEBUG_LLM", "0") == "1"

# ------------- SERVICES -------------
# (Name, Price, Duration mins, Aliases)
SERVICES = [
    ("Haircut", 18, 45, ["haircut", "cut", "mens cut", "men cut"]),
    ("Skin Fade", 22, 60, ["skin fade", "fade"]),
    ("Shape Up", 12, 20, ["shape up", "line up", "lineup"]),
    ("Beard Trim", 10, 20, ["beard", "beard trim", "trim beard"]),
    ("Hot Towel Shave", 15, 30, ["hot towel", "shave", "hot towel shave"]),
    ("Kids Cut", 15, 45, ["kids", "kids cut", "child", "children"]),
    ("Eyebrow Trim", 6, 10, ["eyebrow", "brow", "eyebrow trim"]),
    ("Nose Wax", 8, 10, ["nose wax", "nose"]),
    ("Ear Wax", 8, 10, ["ear wax", "ear"]),
    ("Blow Dry", 10, 15, ["blow dry", "blowdry"]),
]

SERVICE_BY_NUM = {str(i + 1): SERVICES[i] for i in range(len(SERVICES))}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def services_total_minutes(service_names: list[str]) -> int:
    total = 0
    for s in service_names:
        for name, _, mins, _aliases in SERVICES:
            if s == name:
                total += mins
                break
    return total or 45


def resolve_services(raw_services: list[str]) -> list[str]:
    """Map LLM strings / aliases -> canonical service names."""
    out = []
    for rs in raw_services or []:
        t = norm(rs)
        if not t:
            continue
        # exact canonical match
        for name, _, _mins, aliases in SERVICES:
            if t == norm(name):
                out.append(name)
                break
            if any(t == norm(a) for a in aliases):
                out.append(name)
                break
        else:
            # fuzzy contains
            for name, _, _mins, aliases in SERVICES:
                if norm(name) in t or any(norm(a) in t for a in aliases):
                    out.append(name)
                    break
    # de-dupe preserve order
    seen = set()
    final = []
    for s in out:
        if s not in seen:
            seen.add(s)
            final.append(s)
    return final


def parse_dt(text: str) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.now(TZ),
    }
    dt = dateparser.parse(text, settings=settings)
    return dt.astimezone(TZ) if dt else None


def within_hours(start_dt: datetime, duration_minutes: int) -> bool:
    """Enforce Mon–Sat 9–6 (end must also be within hours)."""
    start_dt = start_dt.astimezone(TZ)
    day = start_dt.strftime("%a").lower()[:3]
    if day not in OPEN_DAYS:
        return False
    if not (OPEN_TIME <= start_dt.time() < CLOSE_TIME):
        return False
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    if not (OPEN_TIME < end_dt.time() <= CLOSE_TIME):
        return False
    return True


def menu_text() -> str:
    lines = [
        f"💈 *{SHOP_NAME}*",
        "Reply with *number* or *name*:\n",
        "*Men’s Cuts*",
        "1) Haircut — £18",
        "2) Skin Fade — £22",
        "3) Shape Up — £12\n",
        "*Beard / Shaves*",
        "4) Beard Trim — £10",
        "5) Hot Towel Shave — £15\n",
        "*Kids*",
        "6) Kids Cut — £15\n",
        "*Grooming*",
        "7) Eyebrow Trim — £6",
        "8) Nose Wax — £8",
        "9) Ear Wax — £8",
        "10) Blow Dry — £10\n",
        "Hours: Mon–Sat 9am–6pm | Sun Closed",
        "\nTip: you can type: *book haircut tomorrow at 2pm*",
    ]
    return "\n".join(lines)


def looks_like_booking_text(t: str) -> bool:
    t = norm(t)
    if not t:
        return False
    # must contain either a service-ish keyword or booking keyword + time
    service_words = [norm(s[0]) for s in SERVICES] + ["fade", "haircut", "beard", "kids", "wax", "shave", "shape"]
    booking_words = ["book", "booking", "appointment", "reserve"]
    time_words = ["today", "tomorrow", "mon", "tue", "wed", "thu", "fri", "sat", "am", "pm"]
    has_service = any(w in t for w in service_words)
    has_booking = any(w in t for w in booking_words)
    has_time = any(w in t for w in time_words) or re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", t) or re.search(r"\b\d{1,2}[/-]\d{1,2}\b", t)
    return (has_service and has_time) or (has_booking and has_time) or (has_booking and has_service)


def call_openai_json(user_text: str) -> dict | None:
    """
    Returns dict: {"services":[...], "datetime_text":"..."}
    Uses Responses API with json_schema.
    """
    if not OPENAI_API_KEY:
        if DEBUG_LLM:
            print("[LLM] OPENAI_API_KEY missing")
        return None

    system = (
        "You extract booking info for a UK barbershop.\n"
        "Return ONLY JSON that matches the schema.\n"
        "If the customer did not provide a time/date, set datetime_text to null.\n"
        "services should be a list of service names the customer asked for (can be empty)."
    )

    schema = {
        "name": "booking_extract",
        "schema": {
            "type": "object",
            "properties": {
                "services": {"type": "array", "items": {"type": "string"}},
                "datetime_text": {"type": ["string", "null"]},
            },
            "required": ["services", "datetime_text"],
            "additionalProperties": False,
        },
        "strict": True,
    }

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
        ],
        "text": {"format": {"type": "json_schema", "name": schema["name"], "schema": schema["schema"], "strict": True}},
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=OPENAI_TIMEOUT,
        )
        if r.status_code >= 400:
            if DEBUG_LLM:
                print("[LLM] HTTP", r.status_code, r.text[:500])
            return None

        data = r.json()

        # Pull text from output items
        text_out = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text_out += c.get("text", "")

        if not text_out.strip():
            if DEBUG_LLM:
                print("[LLM] No output_text found")
            return None

        parsed = json.loads(text_out)
        if DEBUG_LLM:
            print("[LLM] parsed:", parsed)
        return parsed
    except Exception as e:
        if DEBUG_LLM:
            print("[LLM] exception:", repr(e))
        return None


# ---------------- STATE ----------------
user_state: dict[str, dict] = {}
# state = { "step": "START|ASK_SERVICE|ASK_TIME|CONFIRM",
#           "services":[...], "dt": datetime, "customer_name": str }

def reset_state(from_number: str):
    user_state[from_number] = {"step": "START", "services": [], "dt": None}


def get_name_from_profile(req) -> str:
    # Twilio WhatsApp sometimes includes ProfileName
    return (req.values.get("ProfileName") or "").strip() or "Customer"


app = Flask(__name__)


@app.get("/")
def health():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp():
    resp = MessagingResponse()
    msg = resp.message()

    from_ = request.values.get("From", "")
    body = (request.values.get("Body") or "").strip()

    if not from_:
        msg.body("Missing sender.")
        return str(resp)

    st = user_state.get(from_)
    if not st:
        reset_state(from_)
        st = user_state[from_]

    text = norm(body)

    # Global commands
    if text in {"restart", "reset", "start", "menu"}:
        reset_state(from_)
        msg.body(menu_text())
        return str(resp)

    if text == "back":
        reset_state(from_)
        msg.body(menu_text())
        return str(resp)

    # ---------------- LLM FIRST (free text) ----------------
    # Only try LLM if message looks like booking text and we're not already mid-confirmation.
    if looks_like_booking_text(body) and st.get("step") in {"START", "ASK_SERVICE"}:
        llm = call_openai_json(body)
        if llm:
            services = resolve_services(llm.get("services") or [])
            dt_text = llm.get("datetime_text")

            # If LLM found services, keep them
            if services:
                st["services"] = services
                st["step"] = "ASK_TIME"

            # If LLM found datetime, parse it
            if dt_text:
                dt = parse_dt(dt_text)
                if dt:
                    st["dt"] = dt
                else:
                    # last safety fallback: extract time words manually from original body
                    possible = re.search(r"(tomorrow|today|\bmon\b|\btue\b|\bwed\b|\bthu\b|\bfri\b|\bsat\b).*(\d{1,2}(:\d{2})?\s?(am|pm))",
                                         body.lower())
                    if possible:
                        dt = parse_dt(possible.group(0))
                        if dt:
                            st["dt"] = dt

            # If we have both: try book immediately
            if st.get("services") and st.get("dt"):
                total_mins = services_total_minutes(st["services"])
                dt = st["dt"]

                if not within_hours(dt, total_mins):
                    msg.body("That time is outside opening hours (Mon–Sat 9am–6pm). Try another time.")
                    return str(resp)

                ok, why = is_time_available(
                    dt,
                    CALENDAR_ID,
                    total_mins,
                    TIMEZONE,
                    buffer_minutes=BUFFER_MINUTES,
                )
                if not ok:
                    # show next slots
                    slots = next_available_slots(
                        dt,
                        CALENDAR_ID,
                        total_mins,
                        TIMEZONE,
                        step_minutes=SLOT_STEP_MINUTES,
                        max_results=5,
                        search_days=7,
                        buffer_minutes=BUFFER_MINUTES,
                    )
                    if slots:
                        pretty = "\n".join([s.strftime("%a %d %b %H:%M") for s in slots])
                        msg.body(f"That time isn’t available ({why}).\nNext available:\n{pretty}\n\nReply with one of these times.")
                        st["step"] = "ASK_TIME"
                        return str(resp)
                    msg.body("That time isn’t available. Reply with another day/time.")
                    st["step"] = "ASK_TIME"
                    return str(resp)

                # Book it
                customer_name = get_name_from_profile(request)
                res = create_booking_event(
                    CALENDAR_ID,
                    " + ".join(st["services"]),
                    customer_name,
                    dt,
                    total_mins,
                    phone=from_,
                    timezone=TIMEZONE,
                )
                msg.body(
                    f"✅ Booked!\n"
                    f"Service: {', '.join(st['services'])}\n"
                    f"When: {dt.strftime('%a %d %b %H:%M')}\n\n"
                    f"Reply *MENU* to book another."
                )
                reset_state(from_)
                return str(resp)

            # If service found but no time -> ask time
            if st.get("services") and not st.get("dt"):
                msg.body(
                    f"✍️ *{', '.join(st['services'])}*\n\n"
                    "What day & time?\nExamples:\n"
                    "• Tomorrow 2pm\n"
                    "• Mon 3:15pm\n"
                    "• 10/02 15:30\n\n"
                    "Reply BACK to change service."
                )
                st["step"] = "ASK_TIME"
                return str(resp)

            # If time found but no service -> show menu
            if st.get("dt") and not st.get("services"):
                msg.body("Got the time ✅ Now choose a service:\n\n" + menu_text())
                st["step"] = "ASK_SERVICE"
                return str(resp)

    # ---------------- MENU FLOW ----------------
    step = st.get("step", "START")

    if step == "START":
        st["step"] = "ASK_SERVICE"
        msg.body(menu_text())
        return str(resp)

    if step == "ASK_SERVICE":
        # number selection
        if text in SERVICE_BY_NUM:
            name, price, mins, _aliases = SERVICE_BY_NUM[text]
            st["services"] = [name]
            st["step"] = "ASK_TIME"
            msg.body(
                f"✍️ *{name}*\n\n"
                "What day & time?\nExamples:\n"
                "• Tomorrow 2pm\n"
                "• Mon 3:15pm\n"
                "• 10/02 15:30\n\n"
                "Reply BACK to change service."
            )
            return str(resp)

        # name selection
        chosen = resolve_services([body])
        if chosen:
            st["services"] = [chosen[0]]
            st["step"] = "ASK_TIME"
            msg.body(
                f"✍️ *{chosen[0]}*\n\n"
                "What day & time?\nExamples:\n"
                "• Tomorrow 2pm\n"
                "• Mon 3:15pm\n"
                "• 10/02 15:30\n\n"
                "Reply BACK to change service."
            )
            return str(resp)

        msg.body("I didn’t recognise that service.\n\n" + menu_text())
        return str(resp)

    if step == "ASK_TIME":
        dt = parse_dt(body)

        # Extra fallback: catch “tomorrow at 2pm” etc
        if not dt:
            possible = re.search(r"(tomorrow|today|\bmon\b|\btue\b|\bwed\b|\bthu\b|\bfri\b|\bsat\b).*(\d{1,2}(:\d{2})?\s?(am|pm))",
                                 body.lower())
            if possible:
                dt = parse_dt(possible.group(0))

        if not dt:
            msg.body(
                "I didn’t understand the time.\n"
                "Try:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\n"
                "Reply BACK to change service."
            )
            return str(resp)

        total_mins = services_total_minutes(st.get("services") or [])
        if not within_hours(dt, total_mins):
            msg.body("That time is outside opening hours (Mon–Sat 9am–6pm). Try another time.")
            return str(resp)

        ok, why = is_time_available(
            dt,
            CALENDAR_ID,
            total_mins,
            TIMEZONE,
            buffer_minutes=BUFFER_MINUTES,
        )
        if not ok:
            slots = next_available_slots(
                dt,
                CALENDAR_ID,
                total_mins,
                TIMEZONE,
                step_minutes=SLOT_STEP_MINUTES,
                max_results=5,
                search_days=7,
                buffer_minutes=BUFFER_MINUTES,
            )
            if slots:
                pretty = "\n".join([s.strftime("%a %d %b %H:%M") for s in slots])
                msg.body(f"That time isn’t available ({why}).\nNext available:\n{pretty}\n\nReply with one of these times.")
                return str(resp)
            msg.body("That time isn’t available. Reply with another day/time.")
            return str(resp)

        # Book
        customer_name = get_name_from_profile(request)
        res = create_booking_event(
            CALENDAR_ID,
            " + ".join(st["services"]),
            customer_name,
            dt,
            total_mins,
            phone=from_,
            timezone=TIMEZONE,
        )
        msg.body(
            f"✅ Booked!\n"
            f"Service: {', '.join(st['services'])}\n"
            f"When: {dt.strftime('%a %d %b %H:%M')}\n\n"
            f"Reply *MENU* to book another."
        )
        reset_state(from_)
        return str(resp)

    # fallback
    reset_state(from_)
    msg.body(menu_text())
    return str(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
