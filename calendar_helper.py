import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2 import service_account

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_calendar_service():
    """
    Build Google Calendar service using either:
    1) GOOGLE_CREDENTIALS_JSON (raw JSON in env), or
    2) GOOGLE_SERVICE_ACCOUNT_FILE (path to JSON file), or
    3) 'credentials.json' in project root.
    """
    json_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if json_env:
        # Load credentials directly from env JSON
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
    creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"Google service account file not found at {creds_path}. "
            f"Set GOOGLE_SERVICE_ACCOUNT_FILE, or set GOOGLE_CREDENTIALS_JSON, "
            f"or place credentials.json in project root."
        )

    credentials = service_account.Credentials.from_service_account_file(
        creds_path, scopes=SCOPES
    )
    service = build("calendar", "v3", credentials=credentials)
    return service



def list_user_bookings(calendar_id: str, user_phone: str, limit: int = 10):
    """
    List upcoming bookings for a given user (by WhatsApp phone number).

    We store the phone number in the event.description when creating a booking,
    then filter by that here.
    """
    service = _get_calendar_service()
    now = datetime.now(TZ).isoformat()

    events_result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=now,
            maxResults=50,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    events = events_result.get("items", [])

    bookings = []
    for e in events:
        desc = e.get("description", "") or ""
        # crude match: just check phone number appears in description
        if user_phone not in desc:
            continue

        start_str = e["start"].get("dateTime") or e["start"].get("date")
        if "T" in start_str:
            start_dt = datetime.fromisoformat(start_str)
        else:
            # all-day event fallback, assume 9am local
            start_dt = datetime.fromisoformat(start_str + "T09:00:00")

        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=TZ)
        start_dt = start_dt.astimezone(TZ)

        bookings.append(
            {
                "id": e["id"],
                "service": e.get("summary", "Booking"),
                "when": start_dt,
                "htmlLink": e.get("htmlLink", ""),
            }
        )

    bookings.sort(key=lambda b: b["when"])
    return bookings[:limit]


def is_time_available(calendar_id: str, start_dt: datetime, end_dt: datetime) -> bool:
    """
    Check if there are no overlapping events in the given time range.

    This is a simple overlap check: it looks for any events between start_dt and end_dt.
    """
    service = _get_calendar_service()

    time_min = start_dt.astimezone(TZ).isoformat()
    time_max = end_dt.astimezone(TZ).isoformat()

    events_result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    events = events_result.get("items", [])
    return len(events) == 0


def next_available_slots(
    calendar_id: str,
    minutes: int,
    from_dt: datetime,
    step_minutes: int,
    limit: int = 3,
):
    """
    Find the next available time slots of length 'minutes' starting from 'from_dt',
    stepping by 'step_minutes' each time, up to 14 days ahead.

    Returns a list of datetime objects in your timezone.
    """
    service = _get_calendar_service()
    found = []
    current = from_dt.astimezone(TZ)

    # Search window: 14 days
    end_search = current + timedelta(days=14)

    while current < end_search and len(found) < limit:
        end_dt = current + timedelta(minutes=minutes)

        # Quick overlap check by listing events in this window
        time_min = current.isoformat()
        time_max = end_dt.isoformat()

        events_result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = events_result.get("items", [])

        if not events:
            found.append(current)

        current += timedelta(minutes=step_minutes)

    return found


def create_booking_event(
    calendar_id: str,
    start_dt: datetime,
    minutes: int,
    service_name: str,
    user_phone: str,
):
    """
    Create a booking event in Google Calendar.

    The phone number is stored in description so we can find bookings per user.
    """
    service = _get_calendar_service()
    start_dt = start_dt.astimezone(TZ)
    end_dt = (start_dt + timedelta(minutes=minutes)).astimezone(TZ)

    event = {
        "summary": service_name,
        "description": f"WhatsApp booking for {user_phone}",
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": str(TZ),
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": str(TZ),
        },
    }

    created = (
        service.events()
        .insert(calendarId=calendar_id, body=event)
        .execute()
    )

    return {
        "id": created["id"],
        "summary": created.get("summary", service_name),
        "start": start_dt,
        "end": end_dt,
        "htmlLink": created.get("htmlLink", ""),
    }


def delete_booking_event(calendar_id: str, event_id: str):
    """
    Delete a booking (used by CANCEL flow).
    """
    service = _get_calendar_service()
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()


def update_booking_time(
    calendar_id: str, event_id: str, new_start: datetime, minutes: int
):
    """
    Move an existing booking to a new time (RESCHEDULE flow).
    """
    service = _get_calendar_service()
    new_start = new_start.astimezone(TZ)
    new_end = new_start + timedelta(minutes=minutes)

    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

    event["start"] = {"dateTime": new_start.isoformat(), "timeZone": str(TZ)}
    event["end"] = {"dateTime": new_end.isoformat(), "timeZone": str(TZ)}

    updated = (
        service.events()
        .update(calendarId=calendar_id, eventId=event_id, body=event)
        .execute()
    )

    return {
        "id": updated["id"],
        "summary": updated.get("summary", "Booking"),
        "start": new_start,
        "end": new_end,
        "htmlLink": updated.get("htmlLink", ""),
    }
