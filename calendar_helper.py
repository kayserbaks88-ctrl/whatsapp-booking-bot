import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

TIMEZONE_HINT = os.getenv("TIMEZONE_HINT", "Europe/London").strip()
TZ = ZoneInfo(TIMEZONE_HINT)

GOOGLE_CALENDAR_ID_ENV = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _calendar_service():
    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON")

    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _cal_id(calendar_id: str | None) -> str:
    cid = (calendar_id or "").strip() or GOOGLE_CALENDAR_ID_ENV
    if not cid:
        raise RuntimeError("Missing GOOGLE_CALENDAR_ID")
    return cid


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat()


def is_time_available(calendar_id: str, start_dt: datetime, duration_min: int = 30) -> bool:
    svc = _calendar_service()
    cid = _cal_id(calendar_id)
    end_dt = start_dt + timedelta(minutes=duration_min)

    fb = svc.freebusy().query(body={
        "timeMin": _to_rfc3339(start_dt),
        "timeMax": _to_rfc3339(end_dt),
        "items": [{"id": cid}],
    }).execute()

    busy = fb.get("calendars", {}).get(cid, {}).get("busy", [])
    return len(busy) == 0


def next_available_slots(
    calendar_id: str,
    start_dt: datetime,
    duration_min: int = 30,
    step_minutes: int = 15,
    count: int = 3,
):
    slots = []
    cur = start_dt
    step = timedelta(minutes=step_minutes)
    limit = start_dt + timedelta(days=7)

    while cur < limit and len(slots) < count:
        if is_time_available(calendar_id, cur, duration_min):
            slots.append(cur)
        cur += step

    return slots


def create_booking_event(
    calendar_id: str,
    service_name: str,
    start_dt: datetime,
    duration_minutes: int,
    customer_number: str,
    price: int | None = None,
):
    """
    Returns: (event_id, htmlLink)
    """
    svc = _calendar_service()
    cid = _cal_id(calendar_id)

    end_dt = start_dt + timedelta(minutes=int(duration_minutes))

    summary = service_name
    if price is not None and price != 0:
        summary = f"{service_name} (£{price})"

    event = {
        "summary": summary,
        "start": {"dateTime": _to_rfc3339(start_dt), "timeZone": TIMEZONE_HINT},
        "end": {"dateTime": _to_rfc3339(end_dt), "timeZone": TIMEZONE_HINT},
        "extendedProperties": {
            "private": {
                "phone": (customer_number or "").strip(),
            }
        },
    }

    created = svc.events().insert(calendarId=cid, body=event).execute()
    return created.get("id"), created.get("htmlLink")


def delete_booking_event(calendar_id: str, event_id: str) -> bool:
    try:
        svc = _calendar_service()
        cid = _cal_id(calendar_id)
        svc.events().delete(calendarId=cid, eventId=event_id).execute()
        return True
    except Exception:
        return False


def list_bookings_for_phone(calendar_id: str, phone: str, max_results: int = 20):
    """
    Returns list of:
      { event_id, summary, start(datetime tz-aware), link(htmlLink or None) }
    """
    svc = _calendar_service()
    cid = _cal_id(calendar_id)

    now = datetime.now(TZ)
    events = svc.events().list(
        calendarId=cid,
        timeMin=_to_rfc3339(now),
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results,
    ).execute()

    out = []
    for e in events.get("items", []):
        priv = (e.get("extendedProperties") or {}).get("private") or {}
        if (priv.get("phone") or "").strip() != (phone or "").strip():
            continue

        start = e.get("start", {}).get("dateTime")
        if not start:
            continue

        start_dt = datetime.fromisoformat(start).astimezone(TZ)

        out.append({
            "event_id": e.get("id"),
            "summary": e.get("summary", "Booking"),
            "start": start_dt,
            "link": e.get("htmlLink"),
        })

    return out