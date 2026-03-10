import os
import json
from datetime import timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

GOOGLE_CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"]


def get_calendar_service():

    service_account_info = json.loads(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    )

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
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

    emoji = {
        "Haircut": "✂️",
        "Skin Fade": "🔥",
        "Shape Up": "🪒",
        "Beard Trim": "🧔",
        "Hot Towel Shave": "🪓",
        "Blow Dry": "💨"
    }.get(service_name, "💈")

    event = {
        "summary": f"{emoji} {service_name} | {name}",
        "description": (
            f"Customer: {name}\n"
            f"Service: {service_name}\n"
            f"Price: £{price}\n\n"
            f"Booked via TrimTech AI"
        ),
        "start": {
            "dateTime": start_time.isoformat(),
            "timeZone": "Europe/London"
        },
        "end": {
            "dateTime": end_time.isoformat(),
            "timeZone": "Europe/London"
        }
    }

    created = service.events().insert(
        calendarId=GOOGLE_CALENDAR_ID,
        body=event
    ).execute()

    return created.get("htmlLink")


def next_available_slots(start_time):

    slots = []
    current = start_time

    while len(slots) < 3:

        current = current + timedelta(minutes=30)

        if is_free(current):
            slots.append(current)

    return slots


def find_available_slots(start_time):

    slots = []
    current = start_time

    while len(slots) < 5:

        current = current + timedelta(minutes=30)

        if is_free(current):
            slots.append(current)

    return slots