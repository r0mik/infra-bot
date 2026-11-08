# Build (amd64):
#   docker buildx build --platform linux/amd64 -t <registry>/ops-agent:<tag> --push .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.23 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY ops_agent ./ops_agent

FROM python:3.12-slim
# ssh client for node_command (the nodes only accept the forced read-only command)
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app
WORKDIR /app
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
CMD ["python", "-m", "ops_agent.main"]
