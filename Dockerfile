# Один образ, один процесс: python -m hub (Telegram long polling + веб на :8080).
# Стадии: assets (CSS и htmx) -> deps -> test (по запросу) -> финальный образ.

# --- assets: закреплённые бинарники по assets.lock, сборка app.css ---
FROM debian:bookworm-slim AS assets
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl ca-certificates coreutils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY scripts ./scripts
COPY styles/app.css ./styles/app.css
COPY hub/web/templates ./hub/web/templates
# CRLF из Windows-чекаута ломает bash и сравнение sha256
RUN sed -i 's/\r$//' scripts/fetch_assets.sh scripts/assets.lock styles/app.css \
    && bash scripts/fetch_assets.sh \
    && mv .tools/tailwindcss /usr/local/bin/tailwindcss \
    && rm -rf .tools \
    && tailwindcss -i styles/app.css -o hub/web/static/app.css --minify \
    && grep -q hub-light hub/web/static/app.css \
    && test -s hub/web/static/htmx.min.js

# --- deps: python и зависимости строго по uv.lock ---
FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    TZ=Europe/Moscow \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Сначала только зависимости — слой кэшируется, пока не меняется uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# --- test: dev-зависимости и tests/ для Linux-only тестов в одноразовом контейнере ---
#   docker build --target test -t personal-hub:test .
#   docker run --rm personal-hub:test pytest -q tests/test_main.py tests/test_ics_parse_isolation.py
FROM deps AS test
RUN uv sync --frozen --no-install-project
COPY hub ./hub
COPY migrations ./migrations
COPY tests ./tests
COPY --from=assets /build/hub/web/static/ ./hub/web/static/
RUN chmod -R a+rX hub/web/static
RUN uv sync --frozen
ENV PATH="/app/.venv/bin:$PATH"
CMD ["pytest", "-q"]

# --- финальный образ ---
FROM deps
# Код. Проект ставится editable из /app, поэтому migrations/ лежит
# рядом с hub/ и db.MIGRATIONS_DIR указывает на /app/migrations.
COPY hub ./hub
COPY migrations ./migrations
COPY --from=assets /build/hub/web/static/ ./hub/web/static/
RUN chmod -R a+rX hub/web/static
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Работаем не от root. /data — том с базой и бэкапами.
RUN useradd --system --uid 10001 --home-dir /app hub && mkdir /data && chown hub /data
USER hub

EXPOSE 8080
CMD ["python", "-m", "hub"]
