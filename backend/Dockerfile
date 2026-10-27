# Build context is the repository root so the offline fixtures can be copied in.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/pyproject.toml ./pyproject.toml
RUN python -c "import subprocess,tomllib; p=tomllib.load(open('pyproject.toml','rb')); subprocess.check_call(['pip','install','--no-cache-dir',*p['project']['dependencies']])"

COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini
COPY evals ./evals

RUN pip install --no-cache-dir --no-deps .

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/objects /data/work \
    && chown -R app:app /data
USER app

ENV OFFLINE_FIXTURES_DIR=/app/evals/fixtures/web
ENV STORAGE_DIR=/data/objects
ENV WORK_DIR=/data/work

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
