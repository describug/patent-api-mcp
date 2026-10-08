# patent-api 원격 서버(HTTP 모드) 이미지. 로컬 stdio·.mcpb 확장에는 쓰지 않는다.
# 키·비밀값은 이미지에 넣지 않는다 — 실행할 때 환경변수(Cloud Run이면 Secret Manager)로 받는다.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 의존성 먼저(코드만 바뀌면 이 층은 다시 받지 않는다). uv.lock 그대로 설치한다.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY server.py ./
COPY core ./core
COPY remote ./remote

# 비루트 사용자로 실행. 쓰기는 /tmp(캐시)만 한다.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app

ENV PATENT_API_TRANSPORT=http \
    PATENT_API_HOST=0.0.0.0 \
    PORT=8080 \
    PATENT_API_CACHE_PATH=/tmp/patent-api-mcp/cache.sqlite3 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

EXPOSE 8080
CMD ["python", "server.py"]
