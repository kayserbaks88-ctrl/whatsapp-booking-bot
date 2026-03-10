from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

from llm_helper import llm_extract
from calendar_helper import is_free, create_booking

app = Flask(__name__)

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

    # waiting for name
    if number in PENDING:

        booking = PENDING[number]

        name = incoming

        link = create_booking(
            name,
            booking["service_name"],
            booking["price"],
            booking["time"]
        )

        msg.body(
            f"✅ Booking confirmed!\n\n"
            f"{booking['service_name']} for {name}\n"
            f"{booking['time'].strftime('%A %H:%M')}\n\n"
            f"📅 Add to calendar:\n{link}"
        )

        del PENDING[number]

        return str(resp)

    if not service:

        msg.body(WELCOME)
        return str(resp)

    service_name, price = service

    if not time:

        msg.body(
            f"Great choice — {service_name} (£{price})\n\n"
            f"What time would you like?\n"
            f"Example: {service_name} tomorrow 2pm"
        )

        return str(resp)

    if not is_free(time):

        msg.body("❌ That time is already booked. Try another time.")
        return str(resp)

    PENDING[number] = {
        "service_name": service_name,
        "price": price,
        "time": time
    }

    msg.body(
        f"👍 {service_name} available {time.strftime('%A %H:%M')}.\n\n"
        f"Please reply with your name to confirm booking."
    )

    return str(resp)


if __name__ == "__main__":
    app.run()