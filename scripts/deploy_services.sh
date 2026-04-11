#!/bin/bash

set -e

echo "==> Deploying services..."

cd "$(dirname "$0")/../services"

docker compose up -d --build

echo "==> Services deployed successfully!"
