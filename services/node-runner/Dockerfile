FROM node:24-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
RUN npm install -g @earendil-works/pi-coding-agent@1.1.0 --no-audit --no-fund && \
    python3 -m venv /opt/runner && /opt/runner/bin/pip install --no-cache-dir httpx==0.28.1 && \
    useradd --uid 10001 --create-home piworker
COPY services/node-runner/runner.py /opt/runner/runner.py
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/opt/runner/bin/python", "/opt/runner/runner.py"]
