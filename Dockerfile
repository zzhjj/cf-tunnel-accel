ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/data \
    CF_TUNNEL_ACCEL_CONFIG_DIR=/data \
    CF_TUNNEL_ACCEL_UI_DIR=/data \
    CF_TUNNEL_ACCEL_ENV_FILE=/config/env \
    CF_TUNNEL_ACCEL_RUNTIME=container

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl dnsutils iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --home-dir /data --no-create-home --shell /usr/sbin/nologin app \
    && install -d -o 1000 -g 1000 /data /app /config

WORKDIR /app
COPY --chown=0:0 LICENSE ./LICENSE
COPY --chown=0:0 cf-tunnel-accel cfa_config.py cfa_cdn.py cfa_ingress.py ./
COPY --chown=0:0 ui/server.py ui/profiles.py ./ui/
COPY --chown=0:0 ui/static/ ./ui/static/
COPY --chown=0:0 docker/healthcheck.py ./docker/healthcheck.py

USER 1000:1000
EXPOSE 8642
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD ["python", "/app/docker/healthcheck.py"]
CMD ["python", "-u", "/app/ui/server.py", "--host", "0.0.0.0", "--port", "8642", "--allow-remote-http"]
