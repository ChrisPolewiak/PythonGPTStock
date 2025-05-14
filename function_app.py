import logging
import os
import csv
import asyncio
import azure.functions as func
import html
import json

from datetime import datetime
from azure.communication.email import EmailClient

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

from semantic_kernel.kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.functions.kernel_arguments import KernelArguments



app = func.FunctionApp()
is_dev = os.getenv("AZURE_FUNCTIONS_ENVIRONMENT") == "Development"

# Load portfolio data depending on environment
def load_portfolio():
    if is_dev:
        with open(os.path.join(os.path.dirname(__file__), "portfolio.tsv"), "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            return list(reader)
    else:
        account_name = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
        account_url = f"https://{account_name}.blob.core.windows.net"
        blob = BlobClient(account_url=account_url,
                          container_name="source",
                          blob_name="portfolio.tsv",
                          credential=DefaultAzureCredential())
        stream = blob.download_blob()
        content = stream.readall().decode("utf-8").splitlines()
        reader = csv.DictReader(content, delimiter="\t")
        return list(reader)

def portfolio_to_tsv(data: list[dict]) -> str:
    if not data:
        return ""
    headers = data[0].keys()
    lines = ["\t".join(headers)]
    for row in data:
        lines.append("\t".join(str(row.get(h, "")) for h in headers))
    return "\n".join(lines)

def render_html_report(data: dict) -> str:
    html = []

    html.append(f"<h2>📌 Rekomendacje na dziś</h2>")

    for action, label, color in [("buy", "✅ Dokupienie", "green"), ("buy-new", "✅ Kupno", "green"), ("sell", "❌ Sprzedaż", "red"), ("hold", "🕒 Przetrzymanie", "gray")]:
        items = data.get("recommendations", {}).get(action, [])
        if items:
            html.append(f"<h3 style='color:{color}'>{label}</h3>")
            html.append("<ul>")
            for rec in items:
                html.append(f"<li><b>{rec.get('company')}</b> <i>({rec.get('symbol')})</i> &mdash; {rec.get('reason')}</li>")
            html.append("</ul>")

    html.append("<hr><h2>📊 Analiza spółek</h2>")
    for stock in data.get("analysis", []):
        html.append(f"<h3><b>{stock.get('company')}</b> <i>({stock.get('symbol')})</i></h3>")
        html.append("<ul>")
        for point in stock.get("highlights", []):
            html.append(f"<li>{point}</li>")
        html.append("</ul>")

    if notes := data.get("notes"):
        html.append(f"<hr><p style='color:gray;font-size:small'>{notes}</p>")

    return "\n".join(html)

def parse_result_to_json(json_text: str) -> dict:
    try:
        return json.loads(json_text)
    except Exception as e:
        logging.error(f"Failed to parse model output to JSON: {e}")
        return {}


runmode = os.getenv("AZURE_FUNCTIONS_ENVIRONMENT") or "Production"

async def querymodel():
    portfolio_data = load_portfolio()
    input_data = input_data = portfolio_to_tsv(portfolio_data)

    prompt = """
Dziś jest {datetime.now().strftime('%Y-%m-%d')}.
Na podstawie poniższego portfela inwestycyjnego wygeneruj analizę w postaci poprawnego obiektu JSON.
Nie dodawaj żadnych opisów ani komentarzy — tylko czysty JSON.

Podaj rekomendacje min 10 spółek, które są w portfelu + 5 nowych.
Rekomendacje kupna podziel na te które już są w portfelu (buy) i nowe (buy-new).
Interesują mnie spółki z rynku amerykańskiego dostępne w XTB generujące wysoką dywidendę albo duży wzrost w ciągu max 3 miesięcy.

W sekcji analiza podaj informacje dla wszystkich posiadanych spółek.
Komentarze dla spółek powinny być krótkie i zwięzłe, nie dłuższe niż 1 zdanie po polsku.

Struktura JSON:

{
  "recommendations": {
    "buy": [ { "symbol": "...", "company": "...", "reason": "..." } ],
    "sell": [ { "symbol": "...", "company": "...", "reason": "..." } ],
    "hold": [ { "symbol": "...", "company": "...", "reason": "..." } ]
    "buy-new": [ { "symbol": "...", "company": "...", "reason": "..." } ],
  },
  "analysis": [
    {
      "symbol": "...",
      "company": "...",
      "highlights": [ "...", "..." ]
    }
  ],
  "notes": "Dodatkowe uwagi podsumowujące analizę, np. ogólny sentyment lub alerty."
}

Portfel wejściowy (TSV):
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

    if isinstance(result.value, str):
        output_json = result.value
    else:
        output_json = "".join([chunk.content for chunk in result.value])

    parsed_json = parse_result_to_json(output_json)
    output_html = render_html_report(parsed_json)

    metadata = result.metadata or {}
    usage = metadata.get("usage", {})

    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cost_input = prompt_tokens * 0.01 / 1000
    cost_output = completion_tokens * 0.03 / 1000
    total_cost = round(cost_input + cost_output, 4)

    logging.info(f"Prompt tokens: {prompt_tokens}, Completion tokens: {completion_tokens}, Total cost: {total_cost}")

    return output_html, prompt_tokens, completion_tokens, total_cost


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
    if is_dev:
        os._exit(0)

@app.function_name(name="daily_review")
@app.timer_trigger(
    schedule="0 0 12 10 * *",
    arg_name="myTimer",
    run_on_startup=is_dev,
    use_monitor=not is_dev
    )
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

