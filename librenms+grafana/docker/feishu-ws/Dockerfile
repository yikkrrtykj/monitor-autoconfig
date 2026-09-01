FROM python:3.13-slim

# Feishu command sidecar: polls the configured group and retains message-receive
# long connection as a degraded fallback. It dials OUT to Feishu, so it works
# behind venue NAT without an inbound public endpoint. Version pinned for the
# message event API used by the client.
RUN pip install --no-cache-dir lark-oapi==1.7.1
