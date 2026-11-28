FROM node:24-bookworm
RUN npm install --global @deepseek-ai/dsh@0.2.2-rc.5
COPY --chmod=0644 gateway.mjs /opt/harness-gateway.mjs
COPY --chmod=0644 start.mjs /opt/harness-start.mjs
USER node
ENV DSH_HOME=/state
WORKDIR /workspace
ENTRYPOINT ["node", "/opt/harness-start.mjs"]
