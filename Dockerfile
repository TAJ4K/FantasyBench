FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --upgrade pip \
    && python -m pip wheel --wheel-dir /wheels .

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/app/.local/bin:$PATH

RUN groupadd --system app && useradd --system --gid app --create-home app
WORKDIR /srv/app

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels

COPY alembic.ini ./
COPY migrations ./migrations
COPY docker/entrypoint.sh /usr/local/bin/fantasy-entrypoint
RUN chmod 0555 /usr/local/bin/fantasy-entrypoint && chown -R app:app /srv/app

USER app
EXPOSE 8000
ENTRYPOINT ["fantasy-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]

