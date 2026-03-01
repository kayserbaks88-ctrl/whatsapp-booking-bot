import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

TIMEZONE_HINT = os.getenv("TIMEZONE_HINT", "Europe/London").strip()
TZ = ZoneInfo(TIMEZONE_HINT)

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _calendar_service():
    if not GOOGLE_CALENDAR_ID:
        raise RuntimeError("Missing GOOGLE_CALENDAR_ID")
    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON")

    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat()


def is_time_available(start_dt: datetime, duration_min: int) -> bool:
    svc = _calendar_service()
    end_dt = start_dt + timedelta(minutes=duration_min)

    fb = svc.freebusy().query(
        body={
            "timeMin": _to_rfc3339(start_dt),
            "timeMax": _to_rfc3339(end_dt),
            "items": [{"id": GOOGLE_CALENDAR_ID}],
        }
    ).execute()

    busy = fb.get("calendars", {}).get(GOOGLE_CALENDAR_ID, {}).get("busy", [])
    return len(busy) == 0


def next_available_slots(
    start_dt: datetime,
    duration_min: int,
    step_min: int = 15,
    count: int = 5,
    search_days: int = 7,
):
    slots = []
    cur = start_dt
    step = timedelta(minutes=step_min)
    limit = start_dt + timedelta(days=search_days)

    while cur < limit and len(slots) < count:
        if is_time_available(cur, duration_min):
            slots.append(cur)
        cur += step

    return slots


def create_booking_event(
    service_name: str,
    start_dt: datetime,
    duration_minutes: int,
    customer_number: str,
    price: int | None = None,
):
    """
    Returns: (event_id, html_link)
    """
    svc = _calendar_service()
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    summary = service_name
    if price is not None and price != 0:
        summary = f"{service_name} (£{price})"

    event = {
        "summary": summary,
        "start": {"dateTime": _to_rfc3339(start_dt), "timeZone": TIMEZONE_HINT},
        "end": {"dateTime": _to_rfc3339(end_dt), "timeZone": TIMEZONE_HINT},
        "extendedProperties": {
            "private": {
                "phone": customer_number,
                "service": service_name,
                "minutes": str(duration_minutes),
                "price": "" if price is None else str(price),
            }
        },
    }

    created = (
        svc.events()
        .insert(calendarId=GOOGLE_CALENDAR_ID, body=event)
        .execute()
    )

    return created.get("id"), created.get("htmlLink")


def delete_booking_event(event_id: str) -> bool:
    try:
        svc = _calendar_service()
        svc.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        return True
    except Exception:
        return False


def list_bookings_for_phone(phone: str, max_results: int = 25):
    svc = _calendar_service()
    now = datetime.now(TZ)

    events = svc.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=_to_rfc3339(now),
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results,
    ).execute()

    out = []
    for e in events.get("items", []):
        priv = (e.get("extendedProperties") or {}).get("private") or {}
        if priv.get("phone") != phone:
            continue

        start = e.get("start", {}).get("dateTime")
        if not start:
            continue

        start_dt = datetime.fromisoformat(start).astimezone(TZ)

        # Prefer stored metadata, fallback to summary parsing
        service = priv.get("service") or e.get("summary") or "Booking"
        minutes = int(priv.get("minutes") or "30")
        price_raw = priv.get("price")
        price = None
        if price_raw is not None and price_raw != "":
            try:
                price = int(price_raw)
            except ValueError:
                price = None

        out.append(
            {
                "event_id": e.get("id"),
                "service_name": service,
                "start_dt": start_dt,
                "minutes": minutes,
                "price": price,
                "htmlLink": e.get("htmlLink"),
            }
        )

    return out


def cancel_booking_by_index(phone: str, index_1based: int):
    bookings = list_bookings_for_phone(phone)
    if not bookings:
        return False, "You have no bookings."

    if index_1based < 1 or index_1based > len(bookings):
        return False, f"Choose a valid booking number 1–{len(bookings)}."

    eid = bookings[index_1based - 1]["event_id"]
    ok = delete_booking_event(eid)
    return (True, "Cancelled.") if ok else (False, "Calendar error cancelling that booking.")


def reschedule_booking_by_index(phone: str, index_1based: int, new_start_dt: datetime):
    """
    Reschedules by updating same event (best practice).
    Keeps minutes from event metadata.
    """
    svc = _calendar_service()
    bookings = list_bookings_for_phone(phone)
    if not bookings:
        return False, "You have no bookings."

    if index_1based < 1 or index_1based > len(bookings):
        return False, f"Choose a valid booking number 1–{len(bookings)}."

    b = bookings[index_1based - 1]
    eid = b["event_id"]
    minutes = int(b.get("minutes") or 30)
    new_end = new_start_dt + timedelta(minutes=minutes)

    try:
        event = svc.events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=eid).execute()
        event["start"] = {"dateTime": _to_rfc3339(new_start_dt), "timeZone": TIMEZONE_HINT}
        event["end"] = {"dateTime": _to_rfc3339(new_end), "timeZone": TIMEZONE_HINT}

        updated = svc.events().update(calendarId=GOOGLE_CALENDAR_ID, eventId=eid, body=event).execute()
        return True, updated.get("htmlLink")
    except Exception:
        return False, "Calendar error rescheduling that booking."