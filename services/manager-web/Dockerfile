FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY user_app.py .
COPY admin_app.py .
COPY web_common.py .
COPY auth_providers.py .
COPY product_capabilities.py .
COPY control_client.py .
COPY executor_client.py .
COPY templates ./templates

ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=8080

CMD ["python", "user_app.py"]
