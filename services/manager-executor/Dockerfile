FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

WORKDIR /app

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY services/manager-web/instance_adapters.py .
COPY services/manager-executor/executor.py .

CMD ["python", "executor.py"]
