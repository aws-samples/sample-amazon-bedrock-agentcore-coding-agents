ARG WORKSHOP_RUNTIME_BASE_IMAGE
FROM ${WORKSHOP_RUNTIME_BASE_IMAGE}

RUN dnf install -y --allowerasing \
    git curl nodejs22 nodejs22-npm python3.13 python3.13-pip jq tar gzip unzip \
    openssl-snapsafe-libs which findutils awscli \
    && dnf clean all \
    && ln -s /usr/bin/node-22 /usr/local/bin/node \
    && ln -s /usr/bin/npm-22 /usr/local/bin/npm \
    && ln -s /usr/bin/npx-22 /usr/local/bin/npx \
    && python3.13 -m venv /opt/workshop-python
ENV PATH="/opt/workshop-python/bin:/usr/local/bin:${PATH}"
RUN node --version && npm --version && npx --version && python3 --version \
    && python3 -c 'import ssl; print(ssl.OPENSSL_VERSION)' \
    && rpm -q openssl-snapsafe-libs

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

# Verify the immutable archive before running its installer as the agent user.
# Both kiro-cli and its re-executed chat sibling must report the pinned version.
COPY _toolchain/ /opt/workshop-cli/
RUN python3 /opt/workshop-cli/cli_versions.py install --cli kiro \
      --home /home/agent --user agent --receipt /opt/workshop-cli/installed.json
ENV HOME=/home/agent

# boto3 + awscurl for AgentCore Identity Token Vault and MCP tool calls. The AWS
# CLI above is also required by orchestrator/runtime_exec.py for the Lab 2 source
# archive download and result upload; the dispatched path bypasses run.sh.
RUN python3 -m pip install --quiet awscurl $(python3 /opt/workshop-cli/cli_versions.py sdk-requirements)

COPY --chown=agent:agent healthcheck.py /app/healthcheck.py
COPY run.sh /app/run.sh
COPY entrypoint.sh /app/entrypoint.sh
COPY otel-collector-config.yaml /app/otel-collector-config.yaml
RUN chmod +x /app/run.sh /app/entrypoint.sh

# Steering files (project-level instructions for kiro-cli).
# The SOURCE is flat (`steering/agent.md`) because a Docker build context cannot COPY
# from `../../orchestrator/harness/kiro/.kiro/`, but the DESTINATION filename must be
# the one `roles.py` declares (`.kiro/steering/validator.md`). runtime_exec.py stages
# steering with `if test -f $HOME/<steering_file>`, an UNGUARDED silent no-op when the
# name differs, so baking this as `agent.md` dispatched the validator with no role at
# all. kiro-cli reads every `.md` in `.kiro/steering/` with `inclusion: always`, so the
# filename is free on its side and must match the registry on ours.
RUN mkdir -p /home/agent/.kiro/steering && chown -R agent:agent /home/agent/.kiro
COPY --chown=agent:agent steering/agent.md /home/agent/.kiro/steering/validator.md

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
