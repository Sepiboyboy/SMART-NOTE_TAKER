# One image for the API and both workers; docker-compose picks the command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && mkdir -p /data/audio && chown app:app /data/audio

USER app
ENV STORAGE_DIR=/data/audio
EXPOSE 8000

CMD ["uvicorn", "notetaker.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
