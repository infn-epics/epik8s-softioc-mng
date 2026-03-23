FROM python:3.11-slim

LABEL maintainer="INFN Beamline Controls"
LABEL description="IOC Manager — pluggable task/job framework with REST API"

# Install git and EPICS build deps (needed for softioc and runtime plugin cloning)
RUN apt-get update && \
    apt-get install -y --no-install-recommends git build-essential libreadline-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install the library
COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir ".[all]"

# Default plugin directory
RUN mkdir -p /data/plugins
ENV IOCMNG_PLUGINS_DIR=/data/plugins
ENV IOCMNG_HOST=0.0.0.0
ENV IOCMNG_PORT=8080
ENV IOCMNG_DISABLE_OPHYD=true
ENV IOCMNG_LOG_LEVEL=info

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/v1/health')" || exit 1

ENTRYPOINT ["iocmng-server"]
