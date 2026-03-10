from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

from llm_helper import llm_extract
from calendar_helper import is_free, create_booking

app = Flask(__name__)

PENDING = {}

WELCOME = """
💈 *BBC Barbers*

Hi there! 👋

How can I help today?

You can book by sending something like:

✂️ Haircut tomorrow 2pm  
🔥 Skin fade Friday 3pm  
🧔 Beard trim Monday 5

*Prices*

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

    # Greeting detection
    if incoming.lower() in ["hi", "hello", "hey"]:
        msg.body(
            "Hi! 👋 Welcome to *BBC Barbers*.\n\n"
            "How can I help today?"
        )
        return str(resp)

    # Continue booking flow
    if number in PENDING:

        booking = PENDING[number]

        if "time" not in booking and time:

            if not is_free(time):

                msg.body(
                    "Ah sorry — that slot has just gone. ❌\n\n"
                    "Could you try another time?"
                )
                return str(resp)

            booking["time"] = time

            msg.body(
                f"👍 That time is available!\n\n"
                f"*{booking['service']}*\n"
                f"{time.strftime('%A %H:%M')}\n\n"
                "Could I take your name to confirm the booking?"
            )

            return str(resp)

        if "time" in booking:

            name = incoming

            link = create_booking(
                name,
                booking["service"],
                booking["price"],
                booking["time"]
            )

            msg.body(
                f"✅ *You're all booked!*\n\n"
                f"{booking['service']} for *{name}*\n"
                f"{booking['time'].strftime('%A %H:%M')}\n\n"
                f"📅 Add to calendar:\n{link}\n\n"
                f"See you then! 💈"
            )

            del PENDING[number]

            return str(resp)

    # New booking request
    if service:

        service_name, price = service

        if not time:

            PENDING[number] = {
                "service": service_name,
                "price": price
            }

            msg.body(
                f"💈 Great choice! A *{service_name}* is £{price}.\n\n"
                f"What time would you like?\n\n"
                f"For example:\n"
                f"{service_name} tomorrow 2pm"
            )

            return str(resp)

        if not is_free(time):

            msg.body(
                "Sorry — that time is already booked. ❌\n\n"
                "Could you try another time?"
            )

            return str(resp)

        PENDING[number] = {
            "service": service_name,
            "price": price,
            "time": time
        }

        msg.body(
            f"👍 Good news — that slot is free!\n\n"
            f"{service_name}\n"
            f"{time.strftime('%A %H:%M')}\n\n"
            "What's your name so I can confirm the booking?"
        )

        return str(resp)

    msg.body(WELCOME)

    return str(resp)


if __name__ == "__main__":
    app.run()