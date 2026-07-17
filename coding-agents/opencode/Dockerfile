FROM public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y --allowerasing \
    git curl python3 python3-pip jq tar gzip nodejs npm which findutils awscli \
    && dnf clean all

# OpenTelemetry collector sidecar (otelcol-contrib): baked in so entrypoint.sh can
# start it at container boot and re-sign the agent's unsigned OTLP to CloudWatch
# Logs / X-Ray / metrics. AgentCore Runtime is arm64 (Graviton).
ARG OTELCOL_VERSION=0.116.1
RUN curl -fsSL -o /tmp/otelcol.tar.gz \
      "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_VERSION}/otelcol-contrib_${OTELCOL_VERSION}_linux_arm64.tar.gz" \
    && tar -xzf /tmp/otelcol.tar.gz -C /usr/local/bin otelcol-contrib \
    && rm /tmp/otelcol.tar.gz \
    && /usr/local/bin/otelcol-contrib --version

# opencode CLI: the frontend builder. It runs against Amazon Bedrock through the
# runtime role's AWS credential chain (no API key, no OpenAI/mantle path), so it
# is not affected by the GPT-5.x allowlisting that gates the Codex path.
# Pinned: 1.18.x dropped the AI-SDK ai.streamText usage spans that Lab 3's
# per-user token query in aws/spans depends on. 1.17.20 is the last release
# that exports token usage over OTel traces.
RUN npm install -g opencode-ai@1.17.20 typescript ts-node

RUN useradd -m -d /home/agent -u 1000 agent

# boto3 + awscurl support AWS and MCP calls. opencode authenticates to Bedrock
# through the runtime role's normal AWS SDK credential chain.
RUN pip3 install --quiet boto3 awscurl

# opencode config: Amazon Bedrock provider, Claude Sonnet 4.5. A per-run project
# AGENTS.md in the working directory carries the role/UI instructions.
RUN mkdir -p /home/agent/.config/opencode && chown -R agent:agent /home/agent/.config
COPY --chown=agent:agent opencode.json /home/agent/.config/opencode/opencode.json

COPY --chown=agent:agent healthcheck.py /app/healthcheck.py
COPY --chown=agent:agent configure_opencode.py /app/configure_opencode.py
COPY run.sh /app/run.sh
COPY entrypoint.sh /app/entrypoint.sh
COPY otel-collector-config.yaml /app/otel-collector-config.yaml
RUN chmod +x /app/run.sh /app/entrypoint.sh

# Global instructions live at HOME; a per-run project overrides them with an
# AGENTS.md in its working directory.
COPY --chown=agent:agent AGENTS.md /home/agent/AGENTS.md

RUN git config --global user.name "AgentCore Bot" && \
    git config --global user.email "agentcore@example.com" && \
    git config --global --add safe.directory '*' && \
    su - agent -c 'git config --global user.name "AgentCore Bot" && git config --global user.email "agentcore@example.com"'

ENV WORKSHOP_AGENT_NAME=opencode
WORKDIR /app
EXPOSE 8080
USER agent

# entrypoint.sh starts the OTel collector at boot, then exec's the CMD.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "/app/healthcheck.py"]
