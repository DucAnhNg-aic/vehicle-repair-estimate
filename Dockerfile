# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="vehicle-repair-estimate"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN addgroup --system --gid 10001 appgroup \
    && adduser --system --uid 10001 --ingroup appgroup appuser

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install -r requirements.txt

COPY --chown=10001:10001 . .
RUN mkdir -p /app/outputs /tmp/vre-api/uploads \
    && chown -R 10001:10001 /app/outputs /tmp/vre-api

USER 10001:10001

EXPOSE 8000
CMD ["python", "-m", "repair_estimate_api"]
