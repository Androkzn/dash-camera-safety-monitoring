---
name: deploy
description: Containerized deploy of fleet-safety-demo via docker compose (edge-only or with cloud receiver). Use when the user wants to ship a build locally or to a host with Docker.
disable-model-invocation: true
allowed-tools: Bash(make docker-build), Bash(make docker-up), Bash(make docker-up-cloud), Bash(make docker-down), Bash(docker compose:*), Bash(git status), Bash(git log:*)
---

# /deploy

Bring up the fleet-safety stack via Docker Compose. There is no remote / cloud-provider deploy in this repo — this is a local-or-host containerized run.

Pass `cloud` to also start the cloud receiver (port 8001).

## Pre-flight

1. `git status` — confirm a clean working tree (or that the user knows about pending changes).
2. Confirm `.env` exists at the repo root (not committed). Required vars: `ROAD_ADMIN_TOKEN`, `ROAD_DSAR_TOKEN`, `ROAD_VEHICLE_ID`, `ROAD_ID`, `ROAD_DRIVER_ID`. See [.env.example](.env.example).
3. Confirm YOLO weights are present (`yolov8n.pt` / `yolov8s.pt`) — they are gitignored and download on first run.

## Steps

1. **Build**: `make docker-build`
2. **Up**:
   - default: `make docker-up`
   - with cloud receiver: `make docker-up-cloud`
3. **Verify**: `curl -s http://localhost:8000/api/live/status` returns 200.
4. (cloud) `curl -s http://localhost:8001/health` returns 200.

## Tear-down

- `make docker-down`

## Reporting

- On success: print exposed ports (8000 edge, 8001 cloud if enabled) and the admin URL.
- On failure: show the last `docker compose logs` lines for the failing service.
- Never push images or change tags without explicit user request.

## Do NOT

- Do not modify `.env` or write secrets to any committed file.
- Do not push to a remote registry, cloud provider, or `git push` from this skill.
