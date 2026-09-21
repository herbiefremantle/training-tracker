# Railway builds this automatically when it finds a Dockerfile (railway.json also names it explicitly).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# listen on all interfaces inside the container (the app itself defaults to this computer only)
ENV HOST=0.0.0.0

# a deployed copy must have a login: the app refuses to start unless APP_PASSWORD is set (see app/auth.py)
ENV REQUIRE_AUTH=1

# the SQLite database lives on the Railway volume mounted at /data - see README "Deploy to Railway"
ENV FITNESS_DB=/data/app.db

WORKDIR /app

# dependencies first, so this layer is cached until requirements.txt changes
COPY requirements.txt .
RUN pip install -r requirements.txt

# only the app itself: no .env, no local database, no tests (see .dockerignore)
COPY app ./app
COPY static ./static

# Railway mounts the volume over /data at runtime. (Railway doesn't support the VOLUME keyword, so there isn't one.)
RUN mkdir -p /data

# Documentation only: the app listens on $PORT, which Railway sets. 8000 is the fallback for `docker run` without it.
EXPOSE 8000

# `python -m app` reads $PORT and $HOST from the environment (app/__main__.py)
CMD ["python", "-m", "app"]
