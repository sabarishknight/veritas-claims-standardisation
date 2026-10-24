# Veritas Claims standardisation pipeline.
#
# Distroless-adjacent and deliberately boring: the application has one runtime
# dependency (PyYAML), so there is very little to build and very little to
# attack. Cloud extras are installed only in the image that needs them.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    VERITAS_LOG_FORMAT=json

WORKDIR /app

# Dependencies first, so a source change does not invalidate the layer.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[gcp,genai]"

# Config ships with the image so a deploy is an atomic, versioned artefact:
# the run ledger's config fingerprint then identifies an exact image.
COPY config/ ./config/

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin veritas \
    && chown -R veritas:veritas /app
USER 10001

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)" \
  || exit 1

ENTRYPOINT ["python", "-m", "veritas"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--no-browser"]
