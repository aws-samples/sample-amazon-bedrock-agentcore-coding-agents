# Built with named contexts: docker buildx bake <role>, from the repository root (docker-bake.hcl).
ARG WORKSHOP_RUNTIME_BASE_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:06da5a3362eda00c5114227ac81abe56dd9b943395553b7c64a310457f4d9e2b
FROM ${WORKSHOP_RUNTIME_BASE_IMAGE}

RUN dnf install -y --allowerasing \
    git curl ca-certificates awscli python3.13 python3.13-pip \
    nodejs22 nodejs22-npm openssl-snapsafe-libs tar gzip which findutils \
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

RUN useradd -m -d /home/agent -u 1000 agent && \
    git config --global user.name "AgentCore Bot" && \
    git config --global user.email "agentcore@example.com"

COPY --from=toolchain cli-versions.json cli_versions.py /opt/workshop-cli/
RUN python3 /opt/workshop-cli/cli_versions.py install --cli claude-code \
      --home /home/agent --user agent --receipt /opt/workshop-cli/installed.json

COPY --chown=agent:agent CLAUDE.md /home/agent/CLAUDE.md
COPY --chown=agent:agent --from=toolchain healthcheck.py /app/healthcheck.py
COPY run.sh /app/run.sh
COPY entrypoint.sh /app/entrypoint.sh
COPY otel-collector-config.yaml /app/otel-collector-config.yaml
RUN chmod +x /app/run.sh /app/entrypoint.sh

# Pre-create Claude Code config to skip onboarding wizard.
RUN mkdir -p /home/agent/.claude && \
    echo '{"numStartups":1,"hasCompletedOnboarding":true,"lastOnboardingVersion":"9.9.999","opusProMigrationComplete":true,"sonnet1m45MigrationComplete":true,"migrationVersion":13,"officialMarketplaceAutoInstallAttempted":true,"officialMarketplaceAutoInstalled":true,"projects":{"/home/agent":{"hasTrustDialogAccepted":true,"hasCompletedProjectOnboarding":true,"lastGracefulShutdown":true}}}' > /home/agent/.claude.json && \
    chown -R agent:agent /home/agent/.claude /home/agent/.claude.json
# settings.json pins Claude Code's three model aliases and maps its built-in Haiku 4.5
# default (modelOverrides). Claude Code checks each UNPINNED tier at start-up, and a tier
# only counts as pinned when its alias names a model of that family, so the Haiku tier
# (unset, or aliased to Sonnet 5.5) was probed on every session: Haiku 4.5 got the Marketplace
# 403 and Haiku 3.5 a 404 (grant event c27c50a7, October 29, 2.1.304). A tier whose
# default has an override is not checked. The override value must differ from every
# configured id: Claude Code maps an override value back to its key, and an equal value
# made Sonnet 5.5 requests carry Haiku 4.5's thinking type and fail once.
COPY --chown=agent:agent settings.json /home/agent/.claude/settings.json

ENV CLAUDE_CODE_USE_BEDROCK=1
# Same reason as the backend image: the CLI is installed as root, so the agent
# user's auto-updater fails and prints a red error in a hand-opened shell. The
# dispatch env sets this, an interactive shell does not, so bake it.
ENV DISABLE_AUTOUPDATER=1
# Log stream name the collector writes to in /workshop/coding-agents/telemetry.
ENV WORKSHOP_AGENT_NAME=claude_code_validator
WORKDIR /app
EXPOSE 8080
USER agent

# entrypoint.sh starts the OTel collector at boot, then exec's the CMD.
ENTRYPOINT ["/app/entrypoint.sh"]
# /ping: HealthyBusy while the agent CLI runs, so a detached session keeps working.
CMD ["python3", "/app/healthcheck.py", "--agent", "claude-code-validator", "--busy", "claude", "--busy", "claude-code"]
