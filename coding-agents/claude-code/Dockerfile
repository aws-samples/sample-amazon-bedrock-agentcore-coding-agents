ARG WORKSHOP_RUNTIME_BASE_IMAGE
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

COPY _toolchain/ /opt/workshop-cli/
RUN python3 /opt/workshop-cli/cli_versions.py install --cli claude-code \
      --home /home/agent --user agent --receipt /opt/workshop-cli/installed.json

COPY --chown=agent:agent CLAUDE.md /home/agent/CLAUDE.md
# The backend-engineering harness skill, staged by the shared build helper.
# The agent reads it from ~/skills and builds from principles (not a file spec).
COPY --chown=agent:agent skills/ /home/agent/skills/
COPY --chown=agent:agent healthcheck.py /app/healthcheck.py
COPY run.sh /app/run.sh
COPY entrypoint.sh /app/entrypoint.sh
COPY otel-collector-config.yaml /app/otel-collector-config.yaml
RUN chmod +x /app/run.sh /app/entrypoint.sh

# Pre-create Claude Code config to skip onboarding wizard.
RUN mkdir -p /home/agent/.claude && \
    echo '{"numStartups":1,"hasCompletedOnboarding":true,"lastOnboardingVersion":"9.9.999","opusProMigrationComplete":true,"sonnet1m45MigrationComplete":true,"migrationVersion":13,"officialMarketplaceAutoInstallAttempted":true,"officialMarketplaceAutoInstalled":true,"projects":{"/home/agent":{"hasTrustDialogAccepted":true,"hasCompletedProjectOnboarding":true,"lastGracefulShutdown":true}}}' > /home/agent/.claude.json && \
    chown -R agent:agent /home/agent/.claude /home/agent/.claude.json
COPY --chown=agent:agent settings.json /home/agent/.claude/settings.json

ENV CLAUDE_CODE_USE_BEDROCK=1
# The CLI is installed globally as root, so the `agent` user cannot write the npm
# prefix and its auto-updater fails. In a DISPATCHED run this never showed, because
# roles.py passes DISABLE_AUTOUPDATER=1 in the role env; a HAND-OPENED Lab 1 shell gets
# no dispatch env, so it printed a persistent red
# `✘ Auto-update failed: no write permission to npm prefix · Run claude doctor`
# in the TUI footer. Seen on a live event box (2026-09-01, CLI 2.1.257). That is the
# first thing an attendee sees inside the backend agent, at exactly the point the page
# says "confirm it is alive", and it reads as a broken environment. Keep the image
# default; run.sh also exports it for command shells that omit the image environment.
ENV DISABLE_AUTOUPDATER=1
# Log stream name the collector writes to in /workshop/coding-agents/telemetry.
ENV WORKSHOP_AGENT_NAME=claude_code
WORKDIR /app
EXPOSE 8080
USER agent

# Reapply the manifest policy after the role's settings COPY, preserving its values.
RUN python3 /opt/workshop-cli/cli_versions.py configure --cli claude-code \
      --home /home/agent

# entrypoint.sh starts the OTel collector at boot, then exec's the CMD.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "/app/healthcheck.py"]
