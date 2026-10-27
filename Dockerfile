ARG PYTHON_IMAGE=python:3.12-alpine
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8765
ARG ALPINE_MIRROR=
RUN set -eu; \
    if [ -n "$ALPINE_MIRROR" ]; then \
      sed -i "s|dl-cdn.alpinelinux.org|${ALPINE_MIRROR}|g" /etc/apk/repositories; \
    fi; \
    apk add --no-cache tzdata; \
    addgroup -S quest; \
    adduser -S -G quest -u 10001 quest; \
    mkdir -p /data; \
    chown quest:quest /data
WORKDIR /app
COPY --chown=quest:quest app.py backup.py manifest.webmanifest ./
COPY --chown=quest:quest static ./static
# Fix file access when a NAS unpacker preserves restrictive source file modes.
RUN chmod -R a+rX /app
USER quest
EXPOSE 8765
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2)" || exit 1
CMD ["python", "-u", "/app/app.py"]
