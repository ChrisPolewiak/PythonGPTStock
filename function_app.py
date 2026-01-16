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

# Import Application Insights
from applicationinsights import TelemetryClient
from applicationinsights.logging import LoggingHandler




app = func.FunctionApp()
is_dev = os.getenv("AZURE_FUNCTIONS_ENVIRONMENT") == "Development"

# Model configuration
MODEL_DEPLOYMENT_NAME = "gpt-5.2-chat"

# Initialize Application Insights
connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
instrumentation_key = os.environ.get("APPINSIGHTS_INSTRUMENTATIONKEY")
telemetry_client = None

if connection_string:
    telemetry_client = TelemetryClient(connection_string)
    telemetry_client.context.application.ver = '1.0.0'  # Set your application version
    
    # Add Application Insights logging handler
    log_handler = LoggingHandler(connection_string)
    log_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(log_handler)
    logging.info("Application Insights initialized with connection string")
elif instrumentation_key:
    telemetry_client = TelemetryClient(instrumentation_key)
    telemetry_client.context.application.ver = '1.0.0'
    
    # Add Application Insights logging handler
    log_handler = LoggingHandler(instrumentation_key)
    log_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(log_handler)
    logging.info("Application Insights initialized with instrumentation key")
else:
    logging.warning("No Application Insights configuration found. Telemetry will not be sent.")

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
        parsed = json.loads(json_text)
        
        # Track successful parse
        if telemetry_client:
            telemetry_client.track_event("JSONParseSuccess", {
                "response_length": str(len(json_text)),
                "response_preview": json_text[:200]
            })
        
        return parsed
    except Exception as e:
        logging.error(f"Failed to parse model output to JSON: {e}")
        
        # Track parse failure with details
        if telemetry_client:
            telemetry_client.track_event("JSONParseFailed", {
                "error": str(e),
                "response_length": str(len(json_text)),
                "response_preview": json_text[:500]
            })
            telemetry_client.track_exception()
        
        return {}


runmode = os.getenv("AZURE_FUNCTIONS_ENVIRONMENT") or "Production"

async def querymodel():
    start_time = datetime.now()
    
    if telemetry_client:
        telemetry_client.track_event("QueryModelStarted")
    
    try:
        portfolio_data = load_portfolio()
        input_data = portfolio_to_tsv(portfolio_data)

        prompt = """
Dziś jest {datetime.now().strftime('%Y-%m-%d')}.
Na podstawie poniższego portfela inwestycyjnego wygeneruj analizę w postaci poprawnego obiektu JSON.
Nie dodawaj żadnych opisów ani komentarzy — tylko czysty JSON.
JSON musi być poprawny składniowo i możliwy do sparsowania przez json.loads().

Cel:
- Wygeneruj rekomendacje inwestycyjne oraz analizę portfela.

Wymagania:
- Podaj rekomendacje dla:
  - minimum 10 spółek, które już są w portfelu,
  - minimum 5 nowych spółek.
- Interesują mnie wyłącznie:
  - spółki z rynku amerykańskiego,
  - dostępne w XTB,
  - o profilu:
    - wysokiej dywidendy lub
    - potencjale dużego wzrostu w ciągu maksymalnie 3 miesięcy.

Podział rekomendacji:
- "buy" – spółki już w portfelu, które warto dokupić,
- "buy-new" – nowe spółki warte dodania,
- "hold" – spółki do utrzymania,
- "sell" – spółki do rozważenia sprzedaży.    

W sekcji "analysis":
- Uwzględnij wszystkie spółki z portfela.
- Komentarz do każdej spółki:
  - po polsku,
  - maksymalnie 1 zdanie,
  - krótki i konkretny.
    
Nie podawaj konkretnych cen akcji.
Jeśli nie masz danych bieżących, opieraj się na trendach i sentymencie z ostatnich miesięcy.

Struktura JSON:

{
  "recommendations": {
    "buy": [
      { "symbol": "...", "company": "...", "reason": "..." }
    ],
    "buy-new": [
      { "symbol": "...", "company": "...", "reason": "..." }
    ],
    "hold": [
      { "symbol": "...", "company": "...", "reason": "..." }
    ],
    "sell": [
      { "symbol": "...", "company": "...", "reason": "..." }
    ]
  },
  "analysis": [
    {
      "symbol": "...",
      "company": "...",
      "highlights": [ "..." ]
    }
  ],
  "notes": "Krótke podsumowanie sentymentu rynku lub główne ryzyka."
}

Portfel wejściowy (TSV):
"""


        prompt = html.escape(prompt + input_data)

        kernel = Kernel()
        chat_service = AzureChatCompletion(
            deployment_name=MODEL_DEPLOYMENT_NAME,
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

        # Track model response
        if telemetry_client:
            telemetry_client.track_event("ModelResponseReceived", {
                "response_length": str(len(output_json)),
                "response_type": "string" if isinstance(result.value, str) else "chunks"
            })

        parsed_json = parse_result_to_json(output_json)
        output_html = render_html_report(parsed_json)

        metadata = result.metadata or {}
        usage = metadata.get("usage", None)
        
        # Extract token usage - handle both dict and object
        if usage:
            if hasattr(usage, 'prompt_tokens'):
                prompt_tokens = usage.prompt_tokens
                completion_tokens = usage.completion_tokens
            else:
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
        else:
            prompt_tokens = 0
            completion_tokens = 0

        cost_input = prompt_tokens * 0.01 / 1000
        cost_output = completion_tokens * 0.03 / 1000
        total_cost = round(cost_input + cost_output, 4)

        logging.info(f"Prompt tokens: {prompt_tokens}, Completion tokens: {completion_tokens}, Total cost: {total_cost}")
        
        # Track metrics in Application Insights
        if telemetry_client:
            duration = (datetime.now() - start_time).total_seconds() * 1000
            telemetry_client.track_metric("PromptTokens", prompt_tokens)
            telemetry_client.track_metric("CompletionTokens", completion_tokens)
            telemetry_client.track_metric("TotalCost", total_cost)
            telemetry_client.track_metric("QueryDuration", duration)
            
            # Track success event
            properties = {
                "cost": str(total_cost),
                "promptTokens": str(prompt_tokens),
                "completionTokens": str(completion_tokens)
            }
            telemetry_client.track_event("QueryModelCompleted", properties)
            telemetry_client.flush()

        return output_html, prompt_tokens, completion_tokens, total_cost
    except Exception as e:
        error_msg = str(e)
        logging.error(f"Error in querymodel: {error_msg}")
        
        # Track exception in Application Insights
        if telemetry_client:
            telemetry_client.track_exception()
            telemetry_client.flush()
            
        raise
def send_report(html_body: str, prompt_tokens: int, completion_tokens: int, total_cost: float):
    start_time = datetime.now()
    
    if telemetry_client:
        telemetry_client.track_event("SendReportStarted")
    
    try:
        acs_connection_string = os.environ["ACS_CONNECTION_STRING"]
        sender_email = os.environ["SENDER_EMAIL"]
        receiver_email = os.environ["RECEIVER_EMAIL"]

        cost_note = f"<hr><p style='font-size:small;color:gray'>🔍 Wykorzystano {prompt_tokens} tokenów promptu, {completion_tokens} tokenów odpowiedzi (łącznie: {prompt_tokens + completion_tokens} tokenów).<br>💸 Szacunkowy koszt: <b>${total_cost}</b> ({MODEL_DEPLOYMENT_NAME}).<br>${runmode}</p>"
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
        
        # Track success and duration in Application Insights
        if telemetry_client:
            duration = (datetime.now() - start_time).total_seconds() * 1000
            telemetry_client.track_metric("EmailSendDuration", duration)
            
            properties = {
                "recipient": receiver_email,
                "sender": sender_email,
                "reportDate": datetime.now().strftime('%Y-%m-%d')
            }
            telemetry_client.track_event("EmailSent", properties)
            telemetry_client.flush()
    except Exception as e:
        error_msg = str(e)
        logging.error(f"Error sending report: {error_msg}")
        
        # Track exception in Application Insights
        if telemetry_client:
            telemetry_client.track_exception()
            telemetry_client.flush()
            
        raise
async def run_daily_review():
    if telemetry_client:
        telemetry_client.track_event("DailyReviewStarted", {"runMode": runmode})
    
    try:
        html_body, prompt_tokens, completion_tokens, total_cost = await querymodel()
        send_report(html_body, prompt_tokens, completion_tokens, total_cost)
        
        if telemetry_client:
            telemetry_client.track_event("DailyReviewCompleted", {"runMode": runmode})
            telemetry_client.flush()
            
        if is_dev:
            os._exit(0)
    except Exception as e:
        if telemetry_client:
            telemetry_client.track_exception()
            telemetry_client.flush()
        raise

@app.function_name(name="daily_review")
@app.timer_trigger(
    schedule="0 12 * * *",
    arg_name="myTimer",
    run_on_startup=is_dev,
    use_monitor=not is_dev
    )
def daily_review(myTimer: func.TimerRequest) -> None:
    if telemetry_client:
        properties = {"pastDue": str(myTimer and myTimer.past_due)}
        telemetry_client.track_event("TimerTriggered", properties)
    
    if myTimer and myTimer.past_due:
        logging.info('The timer is past due!')
    asyncio.run(run_daily_review())

@app.route(route="runreview", auth_level=func.AuthLevel.FUNCTION)
def run_review_http(req: func.HttpRequest) -> func.HttpResponse:
    start_time = datetime.now()
    
    if telemetry_client:
        properties = {
            "clientIp": req.headers.get("X-Forwarded-For", "unknown"),
            "userAgent": req.headers.get("User-Agent", "unknown"),
            "method": req.method
        }
        telemetry_client.track_event("HttpTriggered", properties)
    
    try:
        asyncio.run(run_daily_review())
        
        # Track success metrics
        if telemetry_client:
            duration = (datetime.now() - start_time).total_seconds() * 1000
            telemetry_client.track_metric("HttpTriggerDuration", duration)
            telemetry_client.track_event("HttpTriggerSucceeded")
            telemetry_client.flush()
            
        return func.HttpResponse("✅ Daily report has been manually triggered.", status_code=200)
    except Exception as e:
        error_msg = str(e)
        logging.error(f"Error in HTTP trigger: {error_msg}")
        
        # Track failure metrics
        if telemetry_client:
            telemetry_client.track_exception()
            telemetry_client.track_event("HttpTriggerFailed", {"error": error_msg})
            telemetry_client.flush()
            
        return func.HttpResponse(f"❌ Error while triggering daily report: {e}", status_code=500)

