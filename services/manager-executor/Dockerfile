FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

WORKDIR /app

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
RUN apt-get update \
    && apt-get install -y --no-install-recommends apache2-utils iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY services/manager-executor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/manager-web/instance_adapters.py .
COPY services/manager-web/product_capabilities.py .
COPY services/manager-executor/executor.py .
COPY services/manager-executor/api.py .
COPY services/manager-executor/activity_adapters.py .

CMD ["python", "executor.py"]
