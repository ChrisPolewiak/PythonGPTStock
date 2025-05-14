import logging
import os
import json
import asyncio
import azure.functions as func
import html

from datetime import datetime
from azure.communication.email import EmailClient

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

from semantic_kernel.kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.functions.kernel_arguments import KernelArguments



app = func.FunctionApp()

# Load portfolio data depending on environment
def load_portfolio():
    if os.getenv("AZURE_FUNCTIONS_ENVIRONMENT") == "Development":
        with open(os.path.join(os.path.dirname(__file__), "portfolio.json"), "r") as f:
            return json.load(f)
    else:
        account_name = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
        account_url = f"https://{account_name}.blob.core.windows.net"
        blob = BlobClient(account_url=account_url,
                        container_name="source",
                        blob_name="portfolio.json",
                        credential=DefaultAzureCredential())
        stream = blob.download_blob()
        return json.loads(stream.readall())

runmode = os.getenv("AZURE_FUNCTIONS_ENVIRONMENT") or "Production"

async def querymodel():
    portfolio_data = load_portfolio()
    input_data = json.dumps(portfolio_data, indent=2)

    prompt = f"""
Dziś jest {{date}}.
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

Mój portfel inwestycyjny:
"""
    prompt = html.escape(prompt + input_data)

    # logging.info(f"Prompt starts with:\n{prompt[:2500]}")

    kernel = Kernel()
    chat_service = AzureChatCompletion(
        deployment_name="gpt-4",
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_API_KEY")
    )
    kernel.add_service(chat_service)

    args = KernelArguments(
        input=input_data,
        date=datetime.now().strftime('%Y-%m-%d')
        )


    result = await kernel.invoke_prompt(
        prompt=prompt,
        arguments=args,
        temperature=0.0,
        max_tokens=1000
        )

    output_text = "".join([chunk.content for chunk in result.value])
    metadata = result.metadata or {}
    usage = metadata.get("usage", {})

    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cost_input = prompt_tokens * 0.01 / 1000
    cost_output = completion_tokens * 0.03 / 1000
    total_cost = round(cost_input + cost_output, 4)

    logging.info(f"Prompt tokens: {prompt_tokens}, Completion tokens: {completion_tokens}, Total cost: {total_cost}")

    return output_text, prompt_tokens, completion_tokens, total_cost


def send_report(html_body: str, prompt_tokens: int, completion_tokens: int, total_cost: float):
    acs_connection_string = os.environ["ACS_CONNECTION_STRING"]
    sender_email = os.environ["SENDER_EMAIL"]
    receiver_email = os.environ["RECEIVER_EMAIL"]

    cost_note = f"<hr><p style='font-size:small;color:gray'>🔍 Wykorzystano {prompt_tokens} tokenów promptu, {completion_tokens} tokenów odpowiedzi.<br>💸 Szacunkowy koszt: <b>${total_cost}</b> (GPT-4 Turbo).<br>${runmode}</p>"
    final_html = html_body + cost_note

    email_client = EmailClient.from_connection_string(acs_connection_string)
    message = {
        "content": {
            "subject": f"📈 Dzienny przegląd portfela — {datetime.now().strftime('%Y-%m-%d')}",
            "plainText": "Twój raport dzienny jest dostępny w wersji HTML.",
            "html": final_html
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
    logging.info("Daily review sent via ACS.")

async def run_daily_review():
    html_body, prompt_tokens, completion_tokens, total_cost = await querymodel()
    send_report(html_body, prompt_tokens, completion_tokens, total_cost)

@app.function_name(name="daily_review")
@app.timer_trigger(
    schedule="0 0 12 10 * *",
    arg_name="myTimer",
    run_on_startup=False,
    use_monitor=True)
def daily_review(myTimer: func.TimerRequest) -> None:
    if myTimer and myTimer.past_due:
        logging.info('The timer is past due!')
    asyncio.run(run_daily_review())

@app.route(route="runreview", auth_level=func.AuthLevel.FUNCTION)
def run_review_http(req: func.HttpRequest) -> func.HttpResponse:
    try:
        asyncio.run(run_daily_review())
        return func.HttpResponse("✅ Daily report has been manually triggered.", status_code=200)
    except Exception as e:
        return func.HttpResponse(f"❌ Error while triggering daily report: {e}", status_code=500)

