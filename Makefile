# Create a local environment file without overwriting an existing one.
setup:
	@if [ -f .env ]; then \
		echo ".env already exists; leaving it unchanged"; \
	else \
		cp .env.docker.example .env; \
	fi
	@echo "Edit .env and run make up"

# Build and start Agent-SIP (falls back to docker run without Compose).
up:
	@if docker compose version >/dev/null 2>&1; then \
		docker compose up -d --build; \
	else \
		echo "Docker Compose is not available; using the docker run alternative."; \
		docker run -d --name agent-sip --network host --env-file .env \
			-v "$(CURDIR)/var:/app/var" \
			ghcr.io/ai-redcode/agent-sip:latest; \
	fi

# Stop and remove Agent-SIP.
down:
	@if docker compose version >/dev/null 2>&1; then \
		docker compose down; \
	else \
		echo "Docker Compose is not available; removing the agent-sip container."; \
		docker rm -f agent-sip; \
	fi

# Follow the latest Agent-SIP logs.
logs:
	@if docker compose version >/dev/null 2>&1; then \
		docker compose logs -f --tail=100 agent-sip; \
	else \
		echo "Docker Compose is not available; showing docker logs."; \
		docker logs -f --tail=100 agent-sip; \
	fi

# Restart Agent-SIP.
restart: down up

# Show Agent-SIP container status.
ps:
	@if docker compose version >/dev/null 2>&1; then \
		docker compose ps agent-sip; \
	else \
		echo "Docker Compose is not available; showing docker container status."; \
		docker ps -a --filter name=agent-sip; \
	fi

.PHONY: setup up down logs restart ps
