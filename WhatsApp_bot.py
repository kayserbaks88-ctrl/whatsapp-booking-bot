import os
import re
import json
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import requests
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
app = Flask(__name__)

# ---------------- CONFIG ----------------
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "TrimTech AI")
SHOP_NAME = os.getenv("SHOP_NAME", "BBC Barbers")

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
PORT = int(os.getenv("PORT", "5000"))

HOLD_EXPIRE_MINUTES = int(os.getenv("HOLD_EXPIRE_MINUTES", "10"))
SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))
BOOKING_BUFFER_MINUTES = int(os.getenv("BOOKING_BUFFER_MINUTES", "0"))

OPEN_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat"}
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

# ----- LLM config -----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()   # <-- IMPORTANT
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20"))

# ---------------- SERVICES ----------------
SERVICE_CATALOG = [
    ("Haircut", 18, 45),
    ("Skin Fade", 22, 60),
    ("Beard Trim", 10, 20),
    ("Kids Cut", 15, 30),
    ("Shape Up", 12, 20),
    ("Eyebrow Trim", 6, 10),
    ("Nose Wax", 8, 10),
    ("Ear Wax", 8, 10),
    ("Hot Towel Shave", 15, 30),
    ("Blow Dry", 10, 20),
]

CATEGORIES = {
    "Men’s Cuts": ["Haircut", "Skin Fade", "Shape Up"],
    "Beard / Shaves": ["Beard Trim", "Hot Towel Shave"],
    "Kids": ["Kids Cut"],
    "Grooming": ["Eyebrow Trim", "Nose Wax", "Ear Wax", "Blow Dry"],
}

SERVICE_MAP = {name.lower(): (name, price, mins) for (name, price, mins) in SERVICE_CATALOG}

SERVICE_ALIASES = {
    "fade": "Skin Fade",
    "skin fade": "Skin Fade",
    "hair cut": "Haircut",
    "cut": "Haircut",
    "line up": "Shape Up",
    "shapeup": "Shape Up",
    "beard": "Beard Trim",
    "brows": "Eyebrow Trim",
    "eyebrows": "Eyebrow Trim",
    "nose waxing": "Nose Wax",
    "ear waxing": "Ear Wax",
    "hot towel": "Hot Towel Shave",
}

user_state = {}

# ---------------- HELPERS ----------------
def now() -> datetime:
    return datetime.now(TZ)

def norm(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def fmt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %d %b, %I:%M %p")

def day_key(dt: datetime) -> str:
    return dt.strftime("%a").lower()[:3]

def within_hours(dt: datetime) -> bool:
    if day_key(dt) not in OPEN_DAYS:
        return False
    return OPEN_TIME <= dt.time() < CLOSE_TIME

def end_within_hours(start_dt: datetime, minutes: int) -> bool:
    end_dt = start_dt + timedelta(minutes=minutes)
    return within_hours(start_dt) and (end_dt.time() <= CLOSE_TIME) and (start_dt.date() == end_dt.date())

def parse_dt(text: str):
    if not text:
        return None

    t = text.strip()
    t = re.sub(r"[,]+", " ", t)
    t = re.sub(r"\s+", " ", t)

    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now(),
        "DATE_ORDER": "DMY",
    }

    dt = dateparser.parse(t, settings=settings)
    if not dt:
        return None

    try:
        return dt.astimezone(TZ)
    except Exception:
        return dt.replace(tzinfo=TZ)

def services_total_minutes(services):
    return sum(s["minutes"] for s in services)

def services_total_price(services):
    return sum(s["price"] for s in services)

def summarize_services(services):
    return " + ".join(s["name"] for s in services)

def menu_text():
    lines = []
    lines.append(f"💈 *{SHOP_NAME}*")
    lines.append("Reply with *number* or *name*:")
    lines.append("")

    numbered = []
    n = 1
    for cat, items in CATEGORIES.items():
        lines.append(f"*{cat}*")
        for item in items:
            name, price, mins = SERVICE_MAP[item.lower()]
            numbered.append(name)
            lines.append(f"{n}) {name} — £{price}")
            n += 1
        lines.append("")

    lines.append("Hours: Mon–Sat 9am–6pm | Sun Closed")
    lines.append("")
    lines.append('Tip: you can also type: *"Can I book a haircut tomorrow at 2pm?"*')
    return "\n".join(lines), numbered

def pick_service_by_number(num: int, numbered_names):
    if 1 <= num <= len(numbered_names):
        key = numbered_names[num - 1].lower()
        name, price, mins = SERVICE_MAP[key]
        return {"name": name, "price": price, "minutes": mins}
    return None

def resolve_services(raw_services):
    out = []
    seen = set()
    for s in raw_services or []:
        if not s:
            continue
        t = norm(str(s))
        t = SERVICE_ALIASES.get(t, t)
        key = norm(t)

        if key in SERVICE_MAP:
            name, price, mins = SERVICE_MAP[key]
        else:
            found = None
            for k in SERVICE_MAP.keys():
                if k in key or key in k:
                    found = k
                    break
            if not found:
                continue
            name, price, mins = SERVICE_MAP[found]

        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "price": price, "minutes": mins})
    return out

def looks_like_booking_text(t: str) -> bool:
    if not t:
        return False
    if re.search(r"\b(tomorrow|today|mon|tue|wed|thu|fri|sat|sunday|next)\b", t):
        return True
    if re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", t):
        return True
    if re.search(r"\b\d{1,2}[\/\-]\d{1,2}\b", t):
        return True
    keywords = ["book", "appointment", "haircut", "fade", "beard", "brow", "eyebrow", "shape", "trim", "wax", "shave"]
    return any(k in t for k in keywords)

def call_openai_json(user_text: str):
    """
    Returns dict: {"services":[...], "datetime_text":"..."} or None.
    Uses Responses API with json_schema.
    """
    if not OPENAI_API_KEY:
        return None

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

    system = (
        f"You extract booking info for a UK barbershop.\n"
        f"Timezone: {TIMEZONE}. Date format: DMY.\n"
        f"Services can be words like: haircut, fade, beard, brows, shape up.\n"
        f"Return datetime_text as the phrase the user said (e.g. 'tomorrow at 2pm').\n"
        f"If no time phrase exists, datetime_text must be null."
    )

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "response_format": {"type": "json_schema", "json_schema": schema},
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=OPENAI_TIMEOUT,
        )

        if r.status_code >= 300:
            return None

        data = r.json()

        # Robust parse across Responses output shapes
        out_items = data.get("output", []) or []
        for item in out_items:
            for c in (item.get("content") or []):
                if c.get("type") in ("output_text", "text"):
                    txt = (c.get("text") or "").strip()
                    if not txt:
                        continue
                    try:
                        return json.loads(txt)
                    except Exception:
                        return None

        # Sometimes model output may appear under "output_text"
        txt2 = (data.get("output_text") or "").strip()
        if txt2:
            try:
                return json.loads(txt2)
            except Exception:
                return None

        return None
    except Exception:
        return None

# ---------------- ROUTES ----------------
@app.get("/")
def health():
    return "OK", 200

@app.post("/whatsapp")
def whatsapp():
    from_ = request.form.get("From", "")
    body = request.form.get("Body", "") or ""
    t = norm(body)

    resp = MessagingResponse()
    msg = resp.message()

    st = user_state.get(from_, {"step": "START"})
    step = st.get("step", "START")

    # ---- SMART FREE-TEXT (LLM) ----
    # (Works even if user is currently in SERVICE/TIME/START)
    if t not in {"menu", "cancel", "view", "reschedule", "yes", "no", "back"} and looks_like_booking_text(t):
        data = call_openai_json(body)
        if data:
            raw_services = data.get("services") or []
            datetime_text = data.get("datetime_text")

            services = resolve_services(raw_services)

            # If message includes datetime, try to jump ahead (service + time)
            if services and datetime_text:
                dt = parse_dt(datetime_text)
                if dt:
                    total_minutes = services_total_minutes(services)
                    if within_hours(dt) and end_within_hours(dt, total_minutes):
                        ok, reason = is_time_available(
                            dt, CALENDAR_ID, total_minutes, TIMEZONE,
                            buffer_minutes=BOOKING_BUFFER_MINUTES
                        )
                        if ok:
                            user_state[from_] = {
                                "step": "NAME",
                                "services": services,
                                "dt": dt,
                                "hold_until": now() + timedelta(minutes=HOLD_EXPIRE_MINUTES)
                            }
                            msg.body(
                                f"✅ Time held: {fmt(dt)}\n\n"
                                f"Service(s): {summarize_services(services)}\n"
                                f"Total: £{services_total_price(services)} • {total_minutes} mins\n\n"
                                f"What’s your name?"
                            )
                            return str(resp)

            # If we got services but no usable time, move to TIME step
            if services and step in {"START", "SERVICE", "TIME"}:
                user_state[from_] = {"step": "TIME", "services": services}
                msg.body(
                    f"🪒 {summarize_services(services)}\n\n"
                    f"What day & time?\n"
                    f"Examples:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\n"
                    f"Reply BACK to change service."
                )
                return str(resp)

    # --------- Commands ---------
    if t in {"hi", "hello", "hey", "start", "menu"}:
        m, numbered_names = menu_text()
        user_state[from_] = {"step": "SERVICE", "numbered_names": numbered_names}
        msg.body(m)
        return str(resp)

    if t == "back":
        if step == "TIME":
            m, numbered_names = menu_text()
            user_state[from_] = {"step": "SERVICE", "numbered_names": numbered_names}
            msg.body("↩️ Back to menu:\n\n" + m)
            return str(resp)
        if step == "NAME":
            user_state[from_] = {"step": "TIME", "services": st.get("services", [])}
            msg.body("↩️ Back. What day & time?\nExamples: Tomorrow 2pm / Mon 3:15pm / 10/02 15:30")
            return str(resp)
        if step == "CONFIRM":
            user_state[from_] = {"step": "NAME", "services": st.get("services", []), "dt": st.get("dt"), "hold_until": st.get("hold_until")}
            msg.body("↩️ Back. What’s your name?")
            return str(resp)

        msg.body("↩️ Nothing to go back to. Reply MENU.")
        return str(resp)

    if t == "cancel":
        event_id = st.get("event_id")
        if event_id:
            try:
                delete_booking_event(CALENDAR_ID, event_id)
            except Exception:
                pass
            user_state[from_] = {"step": "SERVICE"}
            msg.body("✅ Cancelled. Reply MENU to book again.")
            return str(resp)

        user_state[from_] = {"step": "SERVICE"}
        msg.body("No booking found to cancel. Reply MENU.")
        return str(resp)

    if t == "view":
        link = st.get("event_link")
        if link:
            msg.body(f"Your booking link:\n👉 {link}")
        else:
            msg.body("No booking link found. Reply MENU.")
        return str(resp)

    if t == "reschedule":
        if st.get("event_id"):
            user_state[from_] = {"step": "RESCHEDULE_TIME", "event_id": st.get("event_id"), "services": st.get("services", []), "name": st.get("name")}
            msg.body("🕒 What new day & time?\nExamples:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30")
            return str(resp)

        msg.body("No booking found to reschedule. Reply MENU.")
        return str(resp)

    # --------- Flow ---------
    if step == "SERVICE":
        numbered_names = st.get("numbered_names") or menu_text()[1]
        raw = re.split(r"\s*(\+|,| and )\s*", body, flags=re.IGNORECASE)
        raw = [x for x in raw if x and x.strip() and x.strip().lower() not in {"+", ",", "and"}]

        picked = []
        if len(raw) == 1 and raw[0].strip().isdigit():
            svc = pick_service_by_number(int(raw[0].strip()), numbered_names)
            if svc:
                picked = [svc]
        else:
            for part in raw:
                part_norm = norm(part)
                if part_norm.isdigit():
                    svc = pick_service_by_number(int(part_norm), numbered_names)
                    if svc:
                        picked.append(svc)
                    continue

                part_norm = SERVICE_ALIASES.get(part_norm, part_norm)

                if part_norm in SERVICE_MAP:
                    name, price, mins = SERVICE_MAP[part_norm]
                    picked.append({"name": name, "price": price, "minutes": mins})
                    continue

                for k in SERVICE_MAP.keys():
                    if k in part_norm or part_norm in k:
                        name, price, mins = SERVICE_MAP[k]
                        picked.append({"name": name, "price": price, "minutes": mins})
                        break

        seen = set()
        services = []
        for s in picked:
            if s["name"].lower() in seen:
                continue
            seen.add(s["name"].lower())
            services.append(s)

        if not services:
            msg.body("I didn’t recognize that service. Reply with a menu number or name. (Example: 1 or Haircut)\n\nReply MENU to see options.")
            return str(resp)

        user_state[from_] = {"step": "TIME", "services": services}
        msg.body(
            f"🪒 {summarize_services(services)}\n\n"
            f"What day & time?\n"
            f"Examples:\n• Tomorrow 2pm\n• Mon 3:15pm\n• 10/02 15:30\n\n"
            f"Reply BACK to change service."
        )
        return str(resp)

    if step == "TIME":
        services = st.get("services", [])
        if not services:
            user_state[from_] = {"step": "SERVICE"}
            msg.body("Session reset. Reply MENU.")
            return str(resp)

        # 1) normal parse
        dt = parse_dt(body)

        # 2) if parse fails, try LLM just for datetime extraction
        if not dt and looks_like_booking_text(t):
            data = call_openai_json(body)
            if data:
                dt_text = data.get("datetime_text")
                if dt_text:
                    dt = parse_dt(dt_text)

        if not dt:
            msg.body("I didn’t understand the time.\nTry: Tomorrow 2pm / Mon 3:15pm / 10/02 15:30\n\nReply BACK to change service.")
            return str(resp)

        total_minutes = services_total_minutes(services)

        if not within_hours(dt) or not end_within_hours(dt, total_minutes):
            msg.body("⏰ We’re open Mon–Sat 9am–6pm.\nPlease pick a time within opening hours.")
            return str(resp)

        ok, reason = is_time_available(
            dt, CALENDAR_ID, total_minutes, TIMEZONE,
            buffer_minutes=BOOKING_BUFFER_MINUTES
        )
        if not ok:
            next_slots = next_available_slots(
                dt, CALENDAR_ID, total_minutes, TIMEZONE,
                step_minutes=SLOT_STEP_MINUTES,
                count=5,
                buffer_minutes=BOOKING_BUFFER_MINUTES
            )
            lines = [f"❌ Not available ({reason})", "", "Next:"]
            for s in next_slots:
                lines.append(f"• {fmt(s)}")
            msg.body("\n".join(lines))
            return str(resp)

        user_state[from_] = {
            "step": "NAME",
            "services": services,
            "dt": dt,
            "hold_until": now() + timedelta(minutes=HOLD_EXPIRE_MINUTES)
        }

        msg.body(
            f"✅ Time held: {fmt(dt)}\n\n"
            f"Service(s): {summarize_services(services)}\n"
            f"Total: £{services_total_price(services)} • {total_minutes} mins\n\n"
            f"What’s your name?\n(Reply BACK to change time.)"
        )
        return str(resp)

    if step == "NAME":
        services = st.get("services", [])
        dt = st.get("dt")
        hold_until = st.get("hold_until")

        if not services or not dt:
            user_state[from_] = {"step": "SERVICE"}
            msg.body("Session reset. Reply MENU.")
            return str(resp)

        if hold_until and now() > hold_until:
            user_state[from_] = {"step": "TIME", "services": services}
            msg.body("⏳ Hold expired. Please send the time again.\nExample: Tomorrow 2pm")
            return str(resp)

        customer_name = body.strip()
        if len(customer_name) < 2:
            msg.body("Please send your name (example: Baks).")
            return str(resp)

        total_minutes = services_total_minutes(services)
        total_price = services_total_price(services)

        user_state[from_] = {
            "step": "CONFIRM",
            "services": services,
            "dt": dt,
            "name": customer_name,
            "hold_until": hold_until
        }

        msg.body(
            "Confirm:\n\n"
            f"🪒 {summarize_services(services)}\n"
            f"🗓️ {fmt(dt)}\n"
            f"👤 {customer_name}\n"
            f"💷 £{total_price} • {total_minutes} mins\n\n"
            "Reply YES to confirm or NO to change time.\n(Reply BACK to edit name.)"
        )
        return str(resp)

    if step == "CONFIRM":
        services = st.get("services", [])
        dt = st.get("dt")
        customer_name = st.get("name")
        hold_until = st.get("hold_until")

        if t == "no":
            user_state[from_] = {"step": "TIME", "services": services}
            msg.body("No problem — what new day & time?\nExample: Tomorrow 2pm")
            return str(resp)

        if t != "yes":
            msg.body("Reply YES to confirm or NO to change time.")
            return str(resp)

        if hold_until and now() > hold_until:
            user_state[from_] = {"step": "TIME", "services": services}
            msg.body("⏳ Hold expired. Please send the time again.\nExample: Tomorrow 2pm")
            return str(resp)

        total_minutes = services_total_minutes(services)
        title = f"{summarize_services(services)} - {customer_name}"
        description = (
            f"Booked via {BUSINESS_NAME}\n"
            f"Shop: {SHOP_NAME}\n"
            f"Customer: {customer_name}\n"
            f"Services: {summarize_services(services)}\n"
            f"Total: £{services_total_price(services)} • {total_minutes} mins"
        )

        try:
            event_id, event_link = create_booking_event(
                calendar_id=CALENDAR_ID,
                start_dt=dt,
                duration_minutes=total_minutes,
                title=title,
                description=description,
                timezone=TIMEZONE,
            )
        except Exception:
            user_state[from_] = {"step": "SERVICE"}
            msg.body("⚠️ Something went wrong creating the booking. Reply MENU to restart.")
            return str(resp)

        user_state[from_] = {
            "step": "BOOKED",
            "services": services,
            "dt": dt,
            "name": customer_name,
            "event_id": event_id,
            "event_link": event_link
        }

        msg.body(
            "✅ BOOKED!\n\n"
            f"🪒 {summarize_services(services)}\n"
            f"🗓️ {fmt(dt)}\n\n"
            "Reply RESCHEDULE to change time\n"
            "Reply CANCEL to cancel\n"
            "Reply VIEW for link\n\n"
            f"Powered by {BUSINESS_NAME}"
        )
        return str(resp)

    if step == "RESCHEDULE_TIME":
        event_id = st.get("event_id")
        services = st.get("services", [])
        customer_name = st.get("name", "Customer")

        dt = parse_dt(body)
        if not dt and looks_like_booking_text(t):
            data = call_openai_json(body)
            if data:
                dt_text = data.get("datetime_text")
                if dt_text:
                    dt = parse_dt(dt_text)

        if not dt:
            msg.body("I didn’t understand the time.\nTry: Tomorrow 2pm / Mon 3:15pm / 10/02 15:30")
            return str(resp)

        total_minutes = services_total_minutes(services) if services else 45

        if not within_hours(dt) or not end_within_hours(dt, total_minutes):
            msg.body("⏰ We’re open Mon–Sat 9am–6pm.\nPlease pick a time within opening hours.")
            return str(resp)

        ok, reason = is_time_available(
            dt, CALENDAR_ID, total_minutes, TIMEZONE,
            buffer_minutes=BOOKING_BUFFER_MINUTES
        )
        if not ok:
            next_slots = next_available_slots(
                dt, CALENDAR_ID, total_minutes, TIMEZONE,
                step_minutes=SLOT_STEP_MINUTES,
                count=5,
                buffer_minutes=BOOKING_BUFFER_MINUTES
            )
            lines = [f"❌ Not available ({reason})", "", "Next:"]
            for s in next_slots:
                lines.append(f"• {fmt(s)}")
            msg.body("\n".join(lines))
            return str(resp)

        try:
            delete_booking_event(CALENDAR_ID, event_id)
        except Exception:
            pass

        title = f"{summarize_services(services) if services else 'Booking'} - {customer_name}"
        description = f"Rescheduled via {BUSINESS_NAME}"

        try:
            new_event_id, new_event_link = create_booking_event(
                calendar_id=CALENDAR_ID,
                start_dt=dt,
                duration_minutes=total_minutes,
                title=title,
                description=description,
                timezone=TIMEZONE,
            )
        except Exception:
            msg.body("⚠️ Something went wrong rescheduling. Reply MENU.")
            user_state[from_] = {"step": "SERVICE"}
            return str(resp)

        user_state[from_] = {
            "step": "BOOKED",
            "services": services,
            "dt": dt,
            "name": customer_name,
            "event_id": new_event_id,
            "event_link": new_event_link
        }

        msg.body(
            "✅ RESCHEDULED!\n\n"
            f"🗓️ {fmt(dt)}\n\n"
            "Reply RESCHEDULE to change time\n"
            "Reply CANCEL to cancel\n"
            "Reply VIEW for link"
        )
        return str(resp)

    # Fallback
    user_state[from_] = {"step": "SERVICE"}
    m, _ = menu_text()
    msg.body(m)
    return str(resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
