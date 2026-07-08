FROM public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y --allowerasing \
    git curl python3 python3-pip jq tar gzip nodejs npm which findutils awscli \
    && dnf clean all

# opencode CLI: the frontend builder. It runs against Amazon Bedrock through the
# runtime role's AWS credential chain (no API key, no OpenAI/mantle path), so it
# is not affected by the GPT-5.x allowlisting that gates the Codex path.
RUN npm install -g opencode-ai typescript ts-node

RUN useradd -m -d /home/agent -u 1000 agent

# boto3 + awscurl support AWS and MCP calls. opencode authenticates to Bedrock
# through the runtime role's normal AWS SDK credential chain.
RUN pip3 install --quiet boto3 awscurl

# opencode config: Amazon Bedrock provider, Claude Sonnet 4.5. A per-run project
# AGENTS.md in the working directory carries the role/UI instructions.
RUN mkdir -p /home/agent/.config/opencode && chown -R agent:agent /home/agent/.config
COPY --chown=agent:agent opencode.json /home/agent/.config/opencode/opencode.json

COPY --chown=agent:agent healthcheck.py /app/healthcheck.py
COPY run.sh /app/run.sh
RUN chmod +x /app/run.sh

# Global instructions live at HOME; a per-run project overrides them with an
# AGENTS.md in its working directory.
COPY --chown=agent:agent AGENTS.md /home/agent/AGENTS.md

RUN git config --global user.name "AgentCore Bot" && \
    git config --global user.email "agentcore@example.com" && \
    git config --global --add safe.directory '*' && \
    su - agent -c 'git config --global user.name "AgentCore Bot" && git config --global user.email "agentcore@example.com"'

WORKDIR /app
EXPOSE 8080
USER agent

CMD ["python3", "/app/healthcheck.py"]
