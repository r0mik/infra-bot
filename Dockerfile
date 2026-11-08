# docker buildx build --platform linux/amd64 -t <registry>/infra-bot:<tag> --push .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.23 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY cluster_bot ./cluster_bot

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home app
WORKDIR /app
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
CMD ["python", "-m", "cluster_bot.main"]
