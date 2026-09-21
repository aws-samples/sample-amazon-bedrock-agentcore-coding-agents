FROM public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:06da5a3362eda00c5114227ac81abe56dd9b943395553b7c64a310457f4d9e2b

RUN dnf install -y --allowerasing \
       ca-certificates curl git python3.13 python3.13-pip python3.13-devel \
       nodejs22 nodejs22-npm openssl-snapsafe-libs openssl-devel \
       gcc rust cargo pkgconf-pkg-config \
    && dnf clean all \
    && ln -s /usr/bin/node-22 /usr/local/bin/node \
    && ln -s /usr/bin/npm-22 /usr/local/bin/npm \
    && ln -s /usr/bin/npx-22 /usr/local/bin/npx

# Strands' MCP dependency also brings pyjwt[crypto]. Build that crypto backend
# against the same snapshot-safe system libraries as Python's own _ssl module.
ENV OPENSSL_NO_VENDOR=1 \
    OPENSSL_LIB_DIR=/usr/lib64 \
    OPENSSL_INCLUDE_DIR=/usr/include

WORKDIR /app

COPY requirements.txt verify_snapshot_crypto.py ./
RUN python3.13 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --no-binary=cryptography -r requirements.txt \
    && /opt/venv/bin/python verify_snapshot_crypto.py

ENV PATH="/opt/venv/bin:${PATH}"

COPY . ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python3", "main.py"]
