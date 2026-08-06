FROM python:3.11-slim

WORKDIR /app

# Copy package metadata separately to make dependency-layer changes explicit.
COPY pyproject.toml README.md ./
COPY app/ ./app/
RUN pip install --no-cache-dir -e .

COPY static/ ./static/
COPY assets/ ./assets/

# Host networking is used at runtime. Agent-SIP uses web 8090/TCP,
# MCP 8765/TCP, SIP 5062/UDP, and RTP 40000-40100/UDP.
CMD ["agent-sip"]
