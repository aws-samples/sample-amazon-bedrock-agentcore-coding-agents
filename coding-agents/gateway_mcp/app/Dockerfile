FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS uv-tool
RUN cp "$(command -v uv)" /uv-binary

FROM public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:06da5a3362eda00c5114227ac81abe56dd9b943395553b7c64a310457f4d9e2b

RUN dnf install -y --allowerasing \
      ca-certificates python3.13 python3.13-pip python3.13-devel shadow-utils \
      openssl-snapsafe-libs openssl-devel gcc rust cargo pkgconf-pkg-config \
    && dnf clean all
COPY --from=uv-tool /uv-binary /usr/local/bin/uv

ARG UV_DEFAULT_INDEX
ARG UV_INDEX

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_PYTHON=/usr/bin/python3.13 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_PROGRESS=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    UV_INDEX=${UV_INDEX} \
    PATH="/app/.venv/bin:$PATH" \
    OPENSSL_NO_VENDOR=1 \
    OPENSSL_LIB_DIR=/usr/lib64 \
    OPENSSL_INCLUDE_DIR=/usr/include

RUN useradd -m -u 1000 bedrock_agentcore

COPY pyproject.toml uv.lock verify_snapshot_crypto.py ./
# A bundled wheel can carry its own OpenSSL. The source build must dynamically
# link the supported system crypto; the verification below rejects a static wheel.
RUN uv sync --frozen --no-dev --no-install-project --no-binary-package cryptography

COPY --chown=bedrock_agentcore:bedrock_agentcore . .
RUN uv sync --frozen --no-dev --no-binary-package cryptography \
    && /app/.venv/bin/python verify_snapshot_crypto.py

USER bedrock_agentcore

# AgentCore Runtime service contract ports
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html
# 8080: HTTP Mode
# 8000: MCP Mode
# 9000: A2A Mode
EXPOSE 8080 8000 9000

CMD ["opentelemetry-instrument", "python", "-m", "main"]
