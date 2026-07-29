FROM public.ecr.aws/docker/library/node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl git python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/opt/venv/bin:${PATH}"

COPY . ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python3", "main.py"]
