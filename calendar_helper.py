import os
from datetime import timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
TIMEZONE_HINT = os.getenv("TIMEZONE_HINT", "Europe/London")
MAX_BARBERS = int(os.getenv("MAX_BARBERS", "1"))

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_calendar_service():

    credentials = service_account.Credentials.from_service_account_file(
        "credentials.json",
        scopes=SCOPES
    )

    service = build("calendar", "v3", credentials=credentials)

    return service


def is_free(start_dt, minutes):

    service = get_calendar_service()

    end_dt = start_dt + timedelta(minutes=minutes)

    events = service.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True
    ).execute()

    items = events.get("items", [])

    return len(items) < MAX_BARBERS


def create_booking(phone, service_name, start_dt, minutes=30, name=""):

    service = get_calendar_service()

    end_dt = start_dt + timedelta(minutes=minutes)

    event = {
        "summary": f"{service_name} | {name}",
        "description": f"Customer phone: {phone}",
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": TIMEZONE_HINT,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": TIMEZONE_HINT,
        },
    }

    created_event = service.events().insert(
        calendarId=GOOGLE_CALENDAR_ID,
        body=event
    ).execute()

    return {
        "status": "confirmed",
        "event_id": created_event["id"],
        "html_link": created_event.get("htmlLink", ""),
        "start": start_dt,
        "end": end_dt
    }