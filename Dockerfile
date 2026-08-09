FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    OPENCEPGEO_DATABASE=/data/opencepgeo.sqlite \
    OPENCEPGEO_BIND=0.0.0.0 \
    OPENCEPGEO_PORT=8080

WORKDIR /app

COPY src/ /app/src/
COPY LICENSE NOTICE.md /app/

USER 65532:65532

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-m", "opencepgeo.service", "--check-ready"]

CMD ["python", "-m", "opencepgeo.service"]
