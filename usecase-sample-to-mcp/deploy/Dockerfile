FROM public.ecr.aws/docker/library/python:3.13-slim

RUN useradd -m -u 1000 agent
WORKDIR /app

RUN pip install --no-cache-dir "fastmcp>=2.11,<3"
COPY runtime_app.py mcp_server.py cost_analyzer.py ./
RUN chown -R agent:agent /app

USER agent
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["python3", "runtime_app.py"]
