# booking.py
import re
import sqlite3
from datetime import datetime, timedelta

DB_PATH = "bookings.db"

SERVICES = {
    "haircut": {"price": 12, "duration_min": 30},
    "skin fade": {"price": 15, "duration_min": 45},
    "beard": {"price": 8, "duration_min": 20},
}

SHOP = {
    "name": "TrimTech AI",
    "address": "12 High Street",
    "currency": "£",
    "open_hours": {  # 24h clock
        "mon": (10, 19),
        "tue": (10, 19),
        "wed": (10, 19),
        "thu": (10, 19),
        "fri": (10, 20),
        "sat": (9, 18),
        "sun": (11, 17),
    }
}

DAY_MAP = {
    "monday": "mon", "mon": "mon",
    "tuesday": "tue", "tue": "tue",
    "wednesday": "wed", "wed": "wed",
    "thursday": "thu", "thu": "thu",
    "friday": "fri", "fri": "fri",
    "saturday": "sat", "sat": "sat",
    "sunday": "sun", "sun": "sun",
}

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            phone TEXT PRIMARY KEY,
            service TEXT,
            day TEXT,
            time TEXT,
            created_at TEXT
        )
    """)
    return conn

def save_booking(phone: str, service: str, day: str, time_str: str):
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO bookings(phone, service, day, time, created_at) VALUES(?,?,?,?,?)",
        (phone, service, day, time_str, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def get_booking(phone: str):
    conn = _db()
    cur = conn.execute("SELECT service, day, time FROM bookings WHERE phone=?", (phone,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"service": row[0], "day": row[1], "time": row[2]}

def cancel_booking(phone: str):
    conn = _db()
    conn.execute("DELETE FROM bookings WHERE phone=?", (phone,))
    conn.commit()
    conn.close()

def price_for(service: str) -> str:
    s = SERVICES.get(service.lower())
    if not s:
        return ""
    return f"{SHOP['currency']}{s['price']}"

def normalize_service(text: str):
    t = text.strip().lower()
    # allow people to type partials
    for s in SERVICES.keys():
        if t == s or t in s:
            return s
    return None

def parse_day(text: str):
    t = text.strip().lower()
    if t in DAY_MAP:
        return t.capitalize() if len(t) > 3 else [k.capitalize() for k,v in DAY_MAP.items() if v == DAY_MAP[t] and len(k)>3][0]
    # allow dd/mm
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        now = datetime.now()
        yr = now.year
        try:
            dt = datetime(yr, mo, d)
            return dt.strftime("%A")
        except ValueError:
            return None
    return None

def parse_time(text: str):
    t = text.strip().lower().replace(" ", "")
    # 5pm, 5:30pm, 17:00
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)$", t)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2) or "00")
        ap = m.group(3)
        if h == 12:
            h = 0
        if ap == "pm":
            h += 12
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
        return None

    m2 = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if m2:
        h, mi = int(m2.group(1)), int(m2.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    return None

def opening_hours_for(day_name: str):
    key = DAY_MAP.get(day_name.lower())
    if not key:
        # convert "Tuesday" -> "tue"
        key = DAY_MAP.get(day_name.lower()[:3])
    if not key:
        return None
    return SHOP["open_hours"].get(key)

def is_time_in_opening(day_name: str, hhmm: str):
    hrs = opening_hours_for(day_name)
    if not hrs:
        return False
    h = int(hhmm.split(":")[0])
    return hrs[0] <= h < hrs[1]

def suggest_slots(day_name: str, step_min: int = 30):
    hrs = opening_hours_for(day_name)
    if not hrs:
        return []
    start_h, end_h = hrs
    slots = []
    # every 30 mins
    t = datetime(2000,1,1,start_h,0)
    end = datetime(2000,1,1,end_h,0)
    while t < end:
        slots.append(t.strftime("%H:%M"))
        t += timedelta(minutes=step_min)
    # return a few
    return slots[:8]
