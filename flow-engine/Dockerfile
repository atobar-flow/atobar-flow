FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/atobar-flow/atobar-flow"
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 fe
WORKDIR /app
COPY contracts/ ./contracts/
COPY flow-engine/ ./flow-engine/
RUN pip install --no-cache-dir ./contracts ./flow-engine
USER fe
EXPOSE 8081
CMD ["uvicorn", "flow_engine.api:app", "--host", "0.0.0.0", "--port", "8081"]
