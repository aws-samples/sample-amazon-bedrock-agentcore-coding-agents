FROM public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y --allowerasing \
    git curl python3 python3-pip jq tar gzip nodejs npm which findutils awscli \
    && dnf clean all

# This version includes the Bedrock Runtime Responses provider.
RUN npm install -g @openai/codex@0.155.1 typescript ts-node

# Same pinned ARM64 collector as the other workshop Runtime images.
ARG OTELCOL_VERSION=0.116.1
RUN curl -fsSL -o /tmp/otelcol.tar.gz \
      "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_VERSION}/otelcol-contrib_${OTELCOL_VERSION}_linux_arm64.tar.gz" \
    && tar -xzf /tmp/otelcol.tar.gz -C /usr/local/bin otelcol-contrib \
    && rm /tmp/otelcol.tar.gz \
    && /usr/local/bin/otelcol-contrib --version

RUN useradd -m -d /home/agent -u 1000 agent

# boto3 supports AWS diagnostics. Codex authenticates to Bedrock
# through the runtime role's normal AWS SDK credential chain.
RUN pip3 install --quiet boto3

# Startup writes the actual region before either run.sh or direct dispatch runs.
RUN mkdir -p /home/agent/.codex && chown -R agent:agent /home/agent/.codex
COPY --chown=agent:agent .codex/config.toml /home/agent/.codex/config.toml

COPY --chown=agent:agent healthcheck.py /app/healthcheck.py
COPY configure_codex.py /app/configure_codex.py
COPY entrypoint.sh /app/entrypoint.sh
COPY otel-collector-config.yaml /app/otel-collector-config.yaml
COPY run.sh /app/run.sh
RUN chmod +x /app/entrypoint.sh /app/run.sh

# Global instructions live under CODEX_HOME. A per-run project overrides them
# with AGENTS.md in its working directory; config.toml remains under .codex/.
RUN mkdir -p /home/agent/.codex && chown -R agent:agent /home/agent/.codex
COPY --chown=agent:agent AGENTS.md /home/agent/.codex/AGENTS.md
# $HOME/AGENTS.md is the path the DISPATCH stages from: runtime_exec.py copies
# `$HOME/<Role.steering_file>` into the role's worktree, and that copy is guarded by a
# silent `test -f`, so a role whose steering is baked ONLY under CODEX_HOME dispatches
# with no steering and no error. Bake both: .codex/ is codex's own global-instruction
# convention, $HOME/ is the registry path the harness resolves.
COPY --chown=agent:agent AGENTS.md /home/agent/AGENTS.md

RUN git config --global user.name "AgentCore Bot" && \
    git config --global user.email "agentcore@example.com" && \
    git config --global --add safe.directory '*' && \
    su - agent -c 'git config --global user.name "AgentCore Bot" && git config --global user.email "agentcore@example.com"'

WORKDIR /app
ENV WORKSHOP_AGENT_NAME=codex
EXPOSE 8080
USER agent

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "/app/healthcheck.py"]
