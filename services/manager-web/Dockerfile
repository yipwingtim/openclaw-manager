FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

WORKDIR /app

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=8080

CMD ["python", "app.py"]
