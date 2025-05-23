# PythonGPTStock

**PythonGPTStock** is an Azure Functions-based application that generates investment recommendations and portfolio analysis using AI models.

## Features

- Loads investment portfolio from a local file or Azure Blob Storage.
- Generates recommendations (buy, sell, hold) and company analysis in JSON format.
- Sends email reports.
- Integrates with Application Insights for monitoring and logging.
- Supports both development and production environments.

## Requirements

- Python 3.10+
- Azure Functions Core Tools (for local development)
- Azure account with configured resources: Blob Storage, Application Insights, Azure Communication Services (Email)
- Required environment variables for credentials and configuration

## Installation

1. Clone the repository:
   ```
   git clone https://github.com/ChrisPolewiak/PythonGPTStock.git
   cd PythonGPTStock
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set environment variables (e.g., in a `.env` file):
   - `AZURE_FUNCTIONS_ENVIRONMENT`
   - `AZURE_STORAGE_ACCOUNT_NAME`
   - `APPLICATIONINSIGHTS_CONNECTION_STRING` or `APPINSIGHTS_INSTRUMENTATIONKEY`
   - Other variables as required by the app

4. Run locally:
   ```
   func start
   ```

## Project Structure

- `function_app.py` – main Azure Function logic
- `portfolio.tsv` – sample investment portfolio (TSV)
- `requirements.txt` – Python dependencies
- `README.MD` – project documentation
- `SECURITY.md` – security policy
- `LICENSE` – MIT license

## Usage

The app automatically loads the portfolio, generates recommendations and analysis, and can send a report via email. Configuration details are managed via environment variables.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.