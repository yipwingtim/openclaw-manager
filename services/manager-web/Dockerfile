FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

WORKDIR /app

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

RUN apt-get update \
    && apt-get install -y --no-install-recommends apache2-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY metadata_store.py .
COPY templates ./templates

ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=8080

CMD ["python", "app.py"]
