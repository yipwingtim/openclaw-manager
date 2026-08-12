FROM python:3.12-slim

WORKDIR /app

COPY services/manager-control/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/manager-control/app.py .
COPY services/manager-control/hermes_auth_bridge.py .
COPY services/manager-web/metadata_store.py .
COPY services/manager-web/product_capabilities.py .

CMD ["gunicorn", "--bind", "0.0.0.0:8082", "--workers", "1", "--timeout", "30", "app:app"]
