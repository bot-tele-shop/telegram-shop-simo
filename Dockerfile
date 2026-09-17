FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.12.14-slim

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

RUN uv sync --frozen --no-group dev

USER app
EXPOSE 8000
CMD ["uvicorn", "digital_shelf.api:app", "--host", "0.0.0.0", "--port", "8000"]
