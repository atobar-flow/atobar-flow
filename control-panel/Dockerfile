FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/atobar-flow/atobar-flow"
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 cp \
    && mkdir -p /var/lib/atobar/screenshots \
    && chown -R cp:cp /var/lib/atobar \
    && chmod 0700 /var/lib/atobar/screenshots
WORKDIR /app
COPY contracts/ ./contracts/
COPY control-panel/ ./control-panel/
COPY scripts/onboard.sh ./scripts/onboard.sh
RUN pip install --no-cache-dir ./contracts ./control-panel
USER cp
EXPOSE 8080
CMD ["uvicorn", "control_panel.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
