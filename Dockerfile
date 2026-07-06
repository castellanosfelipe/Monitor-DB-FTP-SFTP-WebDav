# StabilityMonitor — Modo B (Docker)
FROM python:3.12-slim

# Dependencias primero: cachea la capa de pip entre builds.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Usuario no root; uid 1000 para que los volúmenes montados sean escribibles
# con el usuario por defecto del host en la mayoría de distros.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin monitor \
    && mkdir -p /app/data /app/reports /app/logs \
    && chown -R monitor:monitor /app

WORKDIR /app
COPY --chown=monitor:monitor app ./app
COPY --chown=monitor:monitor static ./static
COPY --chown=monitor:monitor templates ./templates

USER monitor

ENV MONITOR_MODE=docker \
    MONITOR_DATA_DIR=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8090

# Sin curl en slim: el healthcheck usa la stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; \
    r=urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4); \
    sys.exit(0 if r.status==200 else 1)"

CMD ["python", "-m", "app.main"]
