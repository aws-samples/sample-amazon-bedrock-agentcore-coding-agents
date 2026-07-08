FROM public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y --allowerasing git curl nodejs npm python3 python3-pip jq tar gzip unzip which findutils && dnf clean all

# OpenTelemetry collector sidecar (otelcol-contrib): baked in so entrypoint.sh can
# start it at container boot and re-sign the agent's unsigned OTLP to CloudWatch
# Logs / X-Ray / metrics. AgentCore Runtime is arm64 (Graviton).
ARG OTELCOL_VERSION=0.116.1
RUN curl -fsSL -o /tmp/otelcol.tar.gz \
      "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_VERSION}/otelcol-contrib_${OTELCOL_VERSION}_linux_arm64.tar.gz" \
    && tar -xzf /tmp/otelcol.tar.gz -C /usr/local/bin otelcol-contrib \
    && rm /tmp/otelcol.tar.gz \
    && /usr/local/bin/otelcol-contrib --version

RUN npm install -g typescript ts-node

# Non-root user
RUN useradd -m -d /home/agent -u 1000 agent

# Kiro CLI (installed as agent user so it lands in ~/.local/bin)
USER agent
ENV HOME=/home/agent
RUN curl -fsSL https://cli.kiro.dev/install | bash || true
USER root

# boto3 + awscurl for AgentCore Identity Token Vault and MCP tool calls
RUN pip3 install --quiet boto3 awscurl

COPY --chown=agent:agent healthcheck.py /app/healthcheck.py
COPY run.sh /app/run.sh
COPY entrypoint.sh /app/entrypoint.sh
COPY otel-collector-config.yaml /app/otel-collector-config.yaml
RUN chmod +x /app/run.sh /app/entrypoint.sh

# Steering files (project-level instructions for kiro-cli)
RUN mkdir -p /home/agent/.kiro/steering && chown -R agent:agent /home/agent/.kiro
COPY --chown=agent:agent steering/agent.md /home/agent/.kiro/steering/agent.md

RUN git config --global user.name "AgentCore Bot" && \
    git config --global user.email "agentcore@example.com" && \
    git config --global --add safe.directory '*' && \
    su - agent -c 'git config --global user.name "AgentCore Bot" && git config --global user.email "agentcore@example.com"'

ENV PATH="/home/agent/.local/bin:${PATH}"
ENV WORKSHOP_AGENT_NAME=kiro
WORKDIR /app
EXPOSE 8080
USER agent

# entrypoint.sh starts the OTel collector at boot, then exec's the CMD.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "/app/healthcheck.py"]
