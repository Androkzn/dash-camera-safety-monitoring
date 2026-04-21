.PHONY: install dev test lint run start stop docker-build docker-up docker-down clean

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	pytest tests/ -v --tb=short

lint:
	python -m py_compile road_safety/server.py
	python -m py_compile road_safety/config.py
	python -m py_compile start.py

run:
	python start.py

run-cloud:
	python start.py --cloud

start:
	python start.py --skip-tests --no-browser

stop:
	@pids=$$(lsof -ti tcp:3000 tcp:8001 2>/dev/null | sort -u); \
	if [ -n "$$pids" ]; then \
		echo "stopping PIDs: $$pids"; \
		kill $$pids 2>/dev/null; \
		sleep 1; \
		pids=$$(lsof -ti tcp:3000 tcp:8001 2>/dev/null | sort -u); \
		[ -n "$$pids" ] && kill -9 $$pids 2>/dev/null; \
		echo "stopped"; \
	else \
		echo "no server running on :3000 or :8001"; \
	fi

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-up-cloud:
	docker compose --profile cloud up -d

docker-down:
	docker compose down

clean:
	rm -rf dist/ build/ *.egg-info .pytest_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
