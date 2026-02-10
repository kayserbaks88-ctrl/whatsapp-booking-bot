import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_service():
    sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
    creds = Credentials.from_service_account_file(sa_file, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds)


def create_booking_event(
    service_name: str,
    when: datetime,
    name: str = "Customer",
    phone: str = "",
    calendar_id: str = "primary",
    duration_minutes: int = 45,
    timezone: str = "Europe/London",
):
    """
    Returns: (ok: bool, message: str, link: str|None, event_id: str|None)
    """
    try:
        tz = ZoneInfo(timezone)

        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        else:
            when = when.astimezone(tz)

        start_dt = when
        end_dt = start_dt + timedelta(minutes=int(duration_minutes))

        summary = f"{service_name} - {name}"
        description = f"Customer: {name}\nPhone: {phone}\nService: {service_name}"

        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
        }

        service = _get_service()
        created = service.events().insert(calendarId=calendar_id, body=event).execute()

        link = created.get("htmlLink")
        event_id = created.get("id")
        return True, "Booked", link, event_id

    except Exception as e:
        return False, str(e), None, None


def delete_booking_event(calendar_id: str, event_id: str):
    """
    Returns: (ok: bool, message: str)
    """
    try:
        service = _get_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return True, "Deleted"
    except Exception as e:
        return False, str(e)
