import os
import json
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def llm_extract(message):

    prompt = f"""
You are an AI receptionist for a barber shop.

Understand the customer's message and return JSON.

Return:
intent
service
time

Intent options:
greeting
booking
availability
thanks
other

Service options:
haircut
skin fade
beard trim
shape up

Message:
{message}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )

    content = response.choices[0].message.content

    try:
        data = json.loads(content)
    except:
        data = {
            "intent": "other",
            "service": None,
            "time": None
        }

    return data