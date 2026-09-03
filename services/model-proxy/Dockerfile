FROM python:3.12-slim

WORKDIR /app

COPY services/model-proxy/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/model-proxy/app.py ./app.py
COPY services/observability ./observability

ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=8081

CMD ["python", "app.py"]
