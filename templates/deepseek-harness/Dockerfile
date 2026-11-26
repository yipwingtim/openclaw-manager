FROM node:24-bookworm
RUN npm install --global @deepseek-ai/dsh@0.2.2-rc.5
USER node
ENV DSH_HOME=/state
WORKDIR /workspace
ENTRYPOINT ["dsh", "web", "--no-open"]
