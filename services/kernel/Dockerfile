FROM ghcr.io/astral-sh/uv:0.12.18 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra harness --extra browser --no-install-project
COPY src src
COPY migrations migrations
COPY alembic.ini models.yaml ./
RUN uv sync --frozen --no-dev --extra harness --extra browser && \
    useradd --uid 10001 --create-home kernel && mkdir -p /workspace && \
    chown -R kernel:kernel /workspace /app
USER kernel
EXPOSE 8080
CMD ["uv", "run", "--no-sync", "uvicorn", "niucai.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
