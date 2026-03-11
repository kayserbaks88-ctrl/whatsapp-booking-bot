import os
import json
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PROMPT = """
You are a booking assistant for a barbershop.

Extract booking information from the message.

Return ONLY JSON.

Fields:
intent: book | cancel | other
service: haircut | beard | other
when_text: natural language time mentioned

Example:

Message: I want a haircut tomorrow at 3
Response:
{
 "intent": "book",
 "service": "haircut",
 "when_text": "tomorrow 3pm"
}
"""

def llm_extract(message):

    response = client.responses.create(
        model="gpt-4.1-mini",
        temperature=0,
        input=f"{PROMPT}\n\nMessage: {message}"
    )

    text = response.output[0].content[0].text

    try:
        return json.loads(text)
    except:
        return {}