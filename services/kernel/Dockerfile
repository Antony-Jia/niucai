FROM node:24-bookworm-slim AS pi
WORKDIR /opt/pi-runtime
COPY services/pi-runtime/package.json services/pi-runtime/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY services/pi-runtime/tsconfig.json ./
COPY services/pi-runtime/src src
RUN npm run check && npm run build && npm prune --omit=dev --ignore-scripts

FROM ghcr.io/astral-sh/uv:0.12.18 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /uvx /bin/
COPY --from=pi /usr/local/bin/node /usr/local/bin/node
COPY --from=pi /opt/pi-runtime /opt/pi-runtime
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    NIUCAI_PI_ENTRYPOINT=/opt/pi-runtime/dist/main.js \
    NIUCAI_PI_STORAGE=/var/lib/niucai/pi
COPY services/kernel/pyproject.toml services/kernel/uv.lock ./
RUN uv sync --frozen --no-dev --extra harness --extra browser --no-install-project
COPY services/kernel/src src
COPY services/kernel/migrations migrations
COPY services/kernel/alembic.ini services/kernel/models.yaml ./
RUN uv sync --frozen --no-dev --extra harness --extra browser && \
    useradd --uid 10001 --create-home kernel && mkdir -p /workspace /var/lib/niucai/pi && \
    chown -R kernel:kernel /workspace /var/lib/niucai /app
# Computer (gid 1000) and Worker share workspace without world-writable files.
RUN groupadd -g 1000 shared && usermod -aG 1000 kernel && \
    mkdir -p /workspace/artifacts /workspace/downloads && \
    chgrp -R shared /workspace && chmod 2775 /workspace /workspace/artifacts /workspace/downloads
USER kernel
EXPOSE 8080
CMD ["uv", "run", "--no-sync", "uvicorn", "niucai.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
