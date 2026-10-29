FROM node:24-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
RUN npm install -g @earendil-works/pi-coding-agent@1.1.0 --no-audit --no-fund && \
    python3 -m venv /opt/runner && /opt/runner/bin/pip install --no-cache-dir httpx==0.28.1 && \
    useradd --uid 10001 --create-home piworker
# Optional engines, e.g. --build-arg ENGINES="pi codex claude". Versions are pinned; update event fixtures when bumping.
ARG ENGINES=pi
RUN case " $ENGINES " in *" codex "*) npm install -g @openai/codex@0.164.0 --no-audit --no-fund && \
    useradd --uid 10002 --create-home codexworker;; esac
RUN case " $ENGINES " in *" claude "*) npm install -g @anthropic-ai/claude-code@2.1.307 --no-audit --no-fund && \
    useradd --uid 10003 --create-home claudeworker;; esac
COPY services/node-runner/runner.py services/node-runner/board_mcp.py /opt/runner/
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/opt/runner/bin/python", "/opt/runner/runner.py"]
