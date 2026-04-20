FROM python:3.12-slim AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY road_safety/ road_safety/
COPY cloud/ cloud/
COPY static/ static/
COPY data/corpus/ data/corpus/
COPY start.py ./

RUN mkdir -p data/thumbnails data/active_learning/pending

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/admin/health')" || exit 1

CMD ["python", "-m", "uvicorn", "road_safety.server:app", \
     "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
