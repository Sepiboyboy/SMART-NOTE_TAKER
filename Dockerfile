# One image for the API and both workers; docker-compose picks the command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg normalizes and splits audio before transcription.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app
WORKDIR /app

# Install dependencies in their own layer (against an empty package), so code changes
# don't re-download them.
COPY pyproject.toml README.md ./
RUN mkdir -p src/notetaker && touch src/notetaker/__init__.py \
    && pip install --default-timeout=60 --retries 10 hatchling . \
    && mkdir -p /data/audio && chown app:app /data/audio

COPY src ./src
RUN pip install --no-deps --no-build-isolation --force-reinstall .

USER app
ENV STORAGE_DIR=/data/audio
EXPOSE 8000

CMD ["uvicorn", "notetaker.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
