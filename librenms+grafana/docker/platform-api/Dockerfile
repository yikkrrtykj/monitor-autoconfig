FROM python:3.13-slim

# Keep both diagnostic clients inside the service that executes them. This
# avoids depending on a host Docker CLI for cross-container `docker exec`.
# Python 3.13 removed telnetlib; telnetlib3 preserves its blocking API.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iperf3 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir telnetlib3==4.0.5
