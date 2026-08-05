FROM public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y --allowerasing \
    git curl python3 python3-pip jq tar gzip nodejs npm which findutils awscli \
    && dnf clean all

# Codex CLI (OpenAI): latest for GPT-5.5 metadata support
RUN npm install -g @openai/codex@latest typescript ts-node

RUN useradd -m -d /home/agent -u 1000 agent

# boto3 + awscurl support AWS and MCP calls. Codex authenticates to Bedrock
# through the runtime role's normal AWS SDK credential chain.
RUN pip3 install --quiet boto3 awscurl

# Codex config: use Amazon Bedrock (Mantle) instead of direct OpenAI
RUN mkdir -p /home/agent/.codex && chown -R agent:agent /home/agent/.codex
COPY --chown=agent:agent .codex/config.toml /home/agent/.codex/config.toml

COPY --chown=agent:agent healthcheck.py /app/healthcheck.py
COPY run.sh /app/run.sh
RUN chmod +x /app/run.sh

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
EXPOSE 8080
USER agent

CMD ["python3", "/app/healthcheck.py"]
