import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2 import service_account

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")


def _get_calendar_service():
    """
    Build Google Calendar service using either:
    1) GOOGLE_CREDENTIALS_JSON (raw JSON in env), or
    2) GOOGLE_CREDENTIALS_FILE (path to JSON file), or
    3) 'credentials.json' in project root.
    """
    json_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if json_env:
        try:
            info = json.loads(json_env)
        except json.JSONDecodeError as e:
            raise ValueError(
                "GOOGLE_CREDENTIALS_JSON is set but is not valid JSON"
            ) from e

        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
        return build("calendar", "v3", credentials=credentials)

    # Fallback: use file path
    creds_path = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    credentials = service_account.Credentials.from_service_account_file(
        creds_path, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=credentials)


def _phone_tag(phone: str) -> str:
    # Tag we put in description so we can filter events by WhatsApp number
    return f"[WA:{phone}]"


def add_booking(phone: str, service_name: str, start_dt: datetime, minutes: int):
    """
    Create a booking event in Google Calendar and return:
    { "id": eventId, "service": str, "start": datetime, "minutes": int }
    """
    service = _get_calendar_service()
    start_dt = start_dt.astimezone(TZ)
    end_dt = start_dt + timedelta(minutes=minutes)

    event = {
        "summary": service_name,
        "description": f"{_phone_tag(phone)} WhatsApp booking",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": str(TZ)},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": str(TZ)},
    }

    created = (
        service.events()
        .insert(calendarId=CALENDAR_ID, body=event)
        .execute()
    )

    return {
        "id": created["id"],
        "service": service_name,
        "start": start_dt,
        "minutes": minutes,
    }


def list_bookings(phone: str, limit: int = 10):
    """
    List upcoming bookings for this phone.
    Returns list of {id, service, start}.
    """
    service = _get_calendar_service()
    now = datetime.now(TZ).isoformat()
    events_result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=now,
            maxResults=50,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    events = events_result.get("items", [])

    tag = _phone_tag(phone)
    bookings = []

    for e in events:
        desc = e.get("description", "") or ""
        if tag not in desc:
            continue

        start_str = e["start"].get("dateTime") or e["start"].get("date")
        if "T" in start_str:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        else:
            # All-day fallback: assume 9am
            start_dt = datetime.fromisoformat(start_str + "T09:00:00")

        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=TZ)
        start_dt = start_dt.astimezone(TZ)

        bookings.append(
            {
                "id": e["id"],
                "service": e.get("summary", "Booking"),
                "start": start_dt,
            }
        )

    bookings.sort(key=lambda b: b["start"])
    return bookings[:limit]


def cancel_booking(phone: str, booking_id: str):
    """
    Delete a booking by Google eventId.
    Returns deleted booking info or None if not found.
    """
    service = _get_calendar_service()
    try:
        event = service.events().get(calendarId=CALENDAR_ID, eventId=booking_id).execute()
    except Exception:
        return None

    service.events().delete(calendarId=CALENDAR_ID, eventId=booking_id).execute()

    start_str = event["start"].get("dateTime") or event["start"].get("date")
    if "T" in start_str:
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    else:
        start_dt = datetime.fromisoformat(start_str + "T09:00:00")

    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=TZ)
    start_dt = start_dt.astimezone(TZ)

    return {
        "id": booking_id,
        "service": event.get("summary", "Booking"),
        "start": start_dt,
    }


def reschedule_booking(phone: str, booking_id: str, new_start: datetime):
    """
    Move an existing booking to a new time, keep same duration.
    """
    service = _get_calendar_service()
    new_start = new_start.astimezone(TZ)

    event = service.events().get(calendarId=CALENDAR_ID, eventId=booking_id).execute()

    # Compute old duration
    old_start_str = event["start"].get("dateTime") or event["start"].get("date")
    old_end_str = event["end"].get("dateTime") or event["end"].get("date")
    if "T" in old_start_str:
        old_start_dt = datetime.fromisoformat(old_start_str.replace("Z", "+00:00"))
    else:
        old_start_dt = datetime.fromisoformat(old_start_str + "T09:00:00")
    if "T" in old_end_str:
        old_end_dt = datetime.fromisoformat(old_end_str.replace("Z", "+00:00"))
    else:
        old_end_dt = datetime.fromisoformat(old_end_str + "T09:00:00")

    minutes = int((old_end_dt - old_start_dt).total_seconds() // 60)

    new_end = new_start + timedelta(minutes=minutes)

    event["start"] = {"dateTime": new_start.isoformat(), "timeZone": str(TZ)}
    event["end"] = {"dateTime": new_end.isoformat(), "timeZone": str(TZ)}

    updated = (
        service.events()
        .update(calendarId=CALENDAR_ID, eventId=booking_id, body=event)
        .execute()
    )

    return {
        "id": booking_id,
        "service": updated.get("summary", "Booking"),
        "start": new_start,
        "minutes": minutes,
    }


def find_next_booking(phone: str):
    """
    Return the next upcoming booking for this phone, or None.
    """
    bs = list_bookings(phone, limit=50)
    now = datetime.now(TZ)
    future = [b for b in bs if b["start"] >= now]
    if not future:
        return None
    return future[0]
