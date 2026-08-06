FROM public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y --allowerasing \
    git curl nodejs npm python3 python3-pip jq tar gzip unzip which findutils awscli \
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

RUN npm install -g typescript ts-node

# Non-root user
RUN useradd -m -d /home/agent -u 1000 agent

# Kiro CLI (installed as agent user so it lands in ~/.local/bin).
#
# This step FAILS THE BUILD when the CLI does not land, and that is the whole point
# of its shape. It used to end `|| true` with nothing verifying the binary
# afterwards, so a broken install produced a green image whose first symptom was an
# unexplained nonzero exit on the attendee's first Lab 2 gate. The CFN bootstrap
# treats this image as fail-loud, but its guard only checks that setup.sh exited
# zero and that agent.config has an ECR_URI, neither of which notices an absent
# kiro-cli. Same reasoning as the otelcol step above, which has always verified.
#
# Two properties are borrowed from the host install in static/aws/cfn.yaml, read out
# of the vendor installer itself:
#   * Downloaded to a FILE rather than piped into bash, so a truncated transfer
#     fails on curl's exit code instead of executing half a script.
#   * `--force` skips the installer's interactive pre-existing-binary prompt, which
#     has no tty to read here.
# The two probes are separate on purpose: `kiro-cli` alone is not enough, because
# `kiro-cli chat` (the ONLY form run.sh and the orchestrator dispatch ever use)
# re-execs the sibling `kiro-cli-chat`, and a missing sibling dies at run time with
# "No such file or directory (os error 2)".
USER agent
ENV HOME=/home/agent
RUN curl -fsSL https://cli.kiro.dev/install -o /tmp/kiro-install.sh \
    && bash /tmp/kiro-install.sh --force \
    && rm -f /tmp/kiro-install.sh \
    && "$HOME/.local/bin/kiro-cli" --version \
    && test -x "$HOME/.local/bin/kiro-cli-chat"
USER root

# boto3 + awscurl for AgentCore Identity Token Vault and MCP tool calls. The AWS
# CLI above is also required by orchestrator/runtime_exec.py for the Lab 2 source
# archive download and result upload; the dispatched path bypasses run.sh.
RUN pip3 install --quiet boto3 awscurl

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
