import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from zoneinfo import ZoneInfo

from llm_helper import llm_extract
from calendar_helper import is_free, create_booking

app = Flask(__name__)

TZ = ZoneInfo("Europe/London")

# conversation memory
PENDING = {}

WELCOME = """
💈 BBC Barbers

Send a message like:

Haircut tomorrow 2pm
Skin fade Friday 3pm
Beard trim Monday 5

Prices:

✂️ Haircut — £18
🔥 Skin Fade — £22
🪒 Shape Up — £12
🧔 Beard Trim — £10
🪓 Hot Towel Shave — £25
💨 Blow Dry — £20
"""


@app.route("/whatsapp", methods=["POST"])
def whatsapp():

    incoming = request.values.get("Body", "").strip()
    number = request.values.get("From")

    resp = MessagingResponse()
    msg = resp.message()

    service, time = llm_extract(incoming, "Europe/London")

    # STEP 1 — service detected but no time
    if service and not time:

        PENDING[number] = {"service": service}

        service_name, price = service

        msg.body(
            f"💈 BBC Barbers\n\n"
            f"Great choice — {service_name} (£{price})\n\n"
            f"What time would you like?\n"
            f"Example:\n"
            f"{service_name} tomorrow 2pm"
        )

        return str(resp)

    # STEP 2 — service and time together
    if service and time:

        service_name, price = service

        if not is_free(time):

            msg.body(
                "❌ That time is already booked.\n\n"
                "Please try another time."
            )

            return str(resp)

        PENDING[number] = {
            "service": service,
            "time": time
        }

        msg.body(
            f"👍 {service_name} available at {time.strftime('%A %H:%M')}.\n\n"
            f"Please reply with your name to confirm."
        )

        return str(resp)

    # STEP 3 — user sends time after service
    if number in PENDING and time:

        service = PENDING[number]["service"]
        service_name, price = service

        if not is_free(time):

            msg.body(
                "❌ That time is already booked.\n\n"
                "Please try another time."
            )

            return str(resp)

        PENDING[number]["time"] = time

        msg.body(
            f"👍 {service_name} available at {time.strftime('%A %H:%M')}.\n\n"
            f"Please reply with your name to confirm."
        )

        return str(resp)

    # STEP 4 — user sends name
    if number in PENDING and "time" in PENDING[number]:

        customer_name = incoming
        service_name, price = PENDING[number]["service"]
        booking_time = PENDING[number]["time"]

        link = create_booking(customer_name, service_name, price, booking_time)

        msg.body(
            f"✅ Booking confirmed!\n\n"
            f"{service_name} for {customer_name}\n"
            f"{booking_time.strftime('%A %H:%M')}\n\n"
            f"📅 Add to calendar:\n{link}"
        )

        del PENDING[number]

        return str(resp)

    # fallback
    msg.body(WELCOME)

    return str(resp)


if __name__ == "__main__":
    app.run()