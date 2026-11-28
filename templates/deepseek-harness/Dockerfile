FROM node:24-bookworm
RUN npm install --global @deepseek-ai/dsh@0.2.2-rc.5
COPY gateway.mjs /opt/harness-gateway.mjs
COPY start.mjs /opt/harness-start.mjs
USER node
ENV DSH_HOME=/state
WORKDIR /workspace
ENTRYPOINT ["node", "/opt/harness-start.mjs"]
