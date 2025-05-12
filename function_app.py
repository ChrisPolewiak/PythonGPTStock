import logging
import os
import json
from datetime import datetime
from azure.communication.email import EmailClient, EmailContent, EmailAddress, EmailMessage
import openai
import azure.functions as func

app = func.FunctionApp()

@app.timer_trigger(schedule="0 0 12 * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
def daily_review(myTimer: func.TimerRequest) -> None:
    if myTimer.past_due:
        logging.info('The timer is past due!')

    logging.info('Executing daily portfolio review.')

    # Set up environment variables
    openai.api_key = os.environ["OPENAI_API_KEY"]
    acs_connection_string = os.environ["ACS_CONNECTION_STRING"]
    sender_email = os.environ["SENDER_EMAIL"]
    receiver_email = os.environ["RECEIVER_EMAIL"]

    # Load portfolio from JSON
    with open(os.path.join(os.path.dirname(__file__), "portfolio.json"), "r") as f:
        portfolio_data = json.load(f)

    # Build prompt
    prompt = f"""
Today is {datetime.now().strftime('%Y-%m-%d')}.
Based on the following stock portfolio, generate a short market review and suggest any changes that should be made today.
Include only tickers that require attention (e.g., large movement, news, earnings, etc.).
Be concise but informative. Add price targets if possible.

Portfolio:
{json.dumps(portfolio_data, indent=2)}
"""

    # Call OpenAI
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a financial analyst helping a retail investor optimize their stock portfolio."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.7
    )

    result = response["choices"][0]["message"]["content"]

    # Send email using Azure Communication Services
    email_client = EmailClient.from_connection_string(acs_connection_string)
    content = EmailContent(
        subject=f"📈 Daily Stock Review — {datetime.now().strftime('%Y-%m-%d')}",
        plain_text=result
    )
    message = EmailMessage(
        sender=sender_email,
        content=content,
        recipients=[EmailAddress(email=receiver_email)]
    )

    poller = email_client.begin_send(message)
    poller.result()

    logging.info("Daily review sent via ACS.")