# One container runs everything: the agent, the simulated Paytm and the console.
FROM node:22-slim AS console
WORKDIR /console
COPY console/package.json console/package-lock.json ./
RUN npm ci
COPY console/ ./
RUN npm run build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# Hosts such as Hugging Face Spaces run as user 1000, so the app and its data must be theirs.
RUN useradd -m -u 1000 app
WORKDIR /app
COPY --chown=app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY --chown=app . .
COPY --from=console --chown=app /console/dist ./console/dist
RUN mkdir -p data && chown app data
USER app
ENV INPROCESS_MOCK=1 PYTHONUNBUFFERED=1 PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "uv run --no-sync uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
