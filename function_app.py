import logging
import os
import json
from datetime import datetime
from azure.communication.email import EmailClient
from openai import AzureOpenAI
import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

if os.getenv("AZURE_FUNCTIONS_ENVIRONMENT") == "Development":
    # Lokalny test
    with open(os.path.join(os.path.dirname(__file__), "portfolio.json"), "r") as f:
        portfolio_data = json.load(f)
else:
    blob = BlobClient(account_url="https://<your-storage-account>.blob.core.windows.net/",
                    container_name="source",
                    blob_name="portfolio.json",
                    credential=DefaultAzureCredential())

    stream = blob.download_blob()
    portfolio_data = json.loads(stream.readall())

app = func.FunctionApp()

@app.timer_trigger(
        schedule="0 0 12 * * *",
        arg_name="myTimer",
        run_on_startup=True,
        use_monitor=False)

def daily_review(myTimer: func.TimerRequest) -> None:
    if myTimer.past_due:
        logging.info('The timer is past due!')

    logging.info('Executing daily portfolio review.')

    # Set up environment variables
    acs_connection_string = os.environ["ACS_CONNECTION_STRING"]
    sender_email = os.environ["SENDER_EMAIL"]
    receiver_email = os.environ["RECEIVER_EMAIL"]

    # # Load portfolio from JSON
    # with open(os.path.join(os.path.dirname(__file__), "portfolio.json"), "r") as f:
    #     portfolio_data = json.load(f)

    # Build refined prompt in Polish with structured HTML response
    prompt = f"""
Dziś jest {datetime.now().strftime('%Y-%m-%d')}.
Na podstawie poniższego portfela inwestycyjnego wygeneruj **krótki dzienny przegląd** w języku polskim, w formacie **HTML**.

Zasady:
- Rozpocznij raport od sekcji 📌 "Rekomendacje na dziś" – przedstaw jasno co warto zrobić (np. sprzedać, przeczekać, rozważyć dokupienie).
- W kolejnych sekcjach analizuj wszystkie spółki z istotnymi informacjami (📉 duże zmiany, 🗓️ zapowiedzi wyników, 💸 dywidendy, 🛑 alerty, 📢 newsy z rynku).
- Nie pomijaj żadnych wiadomości ani spółek z ważnymi informacjami. Raport ma być kompletny, nie losowy.
- Posortuj spółki wg ważności informacji – od najważniejszych do najmniej istotnych.
- Sekcja "Pozostałe" ma pojawić się z podsumowaniem biezacych informacji.
- Nie dodawaj oznaczeń portfeli (np. XTB, Revolut).
- Wyróżnij istotne rzeczy graficznie (HTML, kolory, ikony) – ale **nie używaj wykresów**.
- Jeżeli to możliwe, dodaj miniaturowe logotypy spółek (np. przez favicony lub linki).
- Nie dodawaj zbędnych informacji, które nie są istotne dla inwestora – np. komentarzy o braku logotypów.
- Nie używaj tagów <html> ani <body> — generuj tylko treść HTML do osadzenia w wiadomości email.

Portfolio:
{json.dumps(portfolio_data, indent=2)}
"""


    # Call OpenAI
    client = AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-03-01-preview",
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"]
    )

    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Jesteś analitykiem finansowym pomagającym polskiemu inwestorowi indywidualnemu analizować swój portfel. Tworzysz raport dzienny w HTML z najważniejszymi informacjami."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0
    )

    result = response.choices[0].message.content
    usage = response.usage
    prompt_tokens = usage.prompt_tokens
    completion_tokens = usage.completion_tokens
    total_tokens = usage.total_tokens

    # Estimate cost
    cost_input = prompt_tokens * 0.01 / 1000
    cost_output = completion_tokens * 0.03 / 1000
    total_cost = round(cost_input + cost_output, 4)

    cost_note = f"<hr><p style='font-size:small;color:gray'>🔍 Wykorzystano {prompt_tokens} tokenów promptu, {completion_tokens} tokenów odpowiedzi.<br>💸 Szacunkowy koszt: <b>${total_cost}</b> (GPT-4 Turbo)</p>"

    html_body = result + cost_note

    # Send email using Azure Communication Services Email (dictionary-based)
    email_client = EmailClient.from_connection_string(acs_connection_string)

    message = {
        "content": {
            "subject": f"📈 Dzienny przegląd portfela — {datetime.now().strftime('%Y-%m-%d')}",
            "plainText": "Twój raport dzienny jest dostępny w wersji HTML.",
            "html": html_body
        },
        "recipients": {
            "to": [
                {
                    "address": receiver_email,
                    "displayName": "Krzysztof Polewiak"
                }
            ]
        },
        "senderAddress": sender_email
    }


    poller = email_client.begin_send(message)
    poller.result()

    logging.info("Daily review sent via ACS using dict-based message format.")
