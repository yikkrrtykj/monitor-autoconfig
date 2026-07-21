FROM python:3.13-slim

# Feishu long-connection (WebSocket) sidecar: receives in-card button
# callbacks (card.action.trigger) without any inbound public endpoint —
# the client dials OUT to Feishu, so it works behind venue NAT exactly
# like the outbound alert webhooks do. Version pinned to the SDK release
# the callback handling was written against.
RUN pip install --no-cache-dir lark-oapi==1.7.1
