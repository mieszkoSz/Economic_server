FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ECONOMIC_API_PORT=8080 \
    ECONOMIC_DASHBOARD_PORT=8081

WORKDIR /app
COPY --chown=10001:10001 server.py /app/server.py
COPY --chown=10001:10001 docs /app/docs
RUN mkdir -p /app/data && chown -R 10001:10001 /app

USER 10001:10001
EXPOSE 8080 8081
CMD ["python", "/app/server.py"]
