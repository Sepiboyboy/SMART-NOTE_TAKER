"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .. import __version__
from ..config import Settings, get_settings
from ..log_config import configure_logging
from ..messaging import EventBus, KafkaEventBus
from ..repository import MongoNoteRepository, NoteRepository
from ..storage import AudioStorage
from .routes import router

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    repo: NoteRepository | None = None,
    bus: EventBus | None = None,
) -> FastAPI:
    """Build the app. Passing `repo`/`bus` skips connecting to MongoDB/Kafka (used in tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mongo = kafka = None
        if repo is None:
            mongo = MongoNoteRepository(settings.mongo_url, settings.mongo_database)
            await mongo.init()
        if bus is None:
            kafka = KafkaEventBus(settings.kafka_bootstrap_servers)
            await kafka.start()

        app.state.settings = settings
        app.state.repo = repo or mongo
        app.state.bus = bus or kafka
        app.state.storage = AudioStorage(settings.storage_dir)
        try:
            yield
        finally:
            if kafka is not None:
                await kafka.stop()
            if mongo is not None:
                await mongo.close()

    app = FastAPI(
        title="AI Note Taker",
        description="Upload audio and get back a transcript and a structured summary.",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
