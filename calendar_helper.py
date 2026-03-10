import os
from datetime import timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_calendar_service():

    credentials = service_account.Credentials.from_service_account_file(
        "credentials.json",
        scopes=SCOPES
    )

    service = build("calendar", "v3", credentials=credentials)

    return service


def is_free(start_time):

    service = get_calendar_service()

    end_time = start_time + timedelta(minutes=30)

    events = service.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=start_time.isoformat(),
        timeMax=end_time.isoformat(),
        singleEvents=True
    ).execute()

    return len(events.get("items", [])) == 0


def create_booking(name, service_name, price, start_time):

    service = get_calendar_service()

    end_time = start_time + timedelta(minutes=30)

    event = {
        "summary": f"{service_name} - {name}",
        "description": f"{service_name} (£{price})",
        "start": {
            "dateTime": start_time.isoformat(),
            "timeZone": "Europe/London",
        },
        "end": {
            "dateTime": end_time.isoformat(),
            "timeZone": "Europe/London",
        },
    }

    created = service.events().insert(
        calendarId=GOOGLE_CALENDAR_ID,
        body=event
    ).execute()

    return created.get("htmlLink")