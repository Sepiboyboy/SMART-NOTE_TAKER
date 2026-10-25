"""Run a pipeline worker: `python -m notetaker.workers {transcriber|summarizer}`."""

import argparse
import asyncio
import contextlib
import signal

from ..ai import build_ai
from ..config import get_settings
from ..log_config import configure_logging
from ..messaging import KafkaEventBus, consume
from ..pipeline import Pipeline
from ..repository import MongoNoteRepository
from ..storage import AudioStorage


async def run(stage: str) -> None:
    settings = get_settings()
    repo = MongoNoteRepository(settings.mongo_url, settings.mongo_database)
    await repo.init()
    bus = KafkaEventBus(settings.kafka_bootstrap_servers)
    await bus.start()

    transcriber, summarizer = build_ai(settings)
    pipeline = Pipeline(
        repo=repo,
        storage=AudioStorage(settings.storage_dir),
        bus=bus,
        transcriber=transcriber,
        summarizer=summarizer,
        transcribed_topic=settings.topic_transcribed,
    )
    if stage == "transcriber":
        topic, group, handler = (
            settings.topic_uploaded,
            settings.transcriber_group,
            pipeline.transcribe,
        )
    else:
        topic, group, handler = (
            settings.topic_transcribed,
            settings.summarizer_group,
            pipeline.summarize,
        )

    try:
        await consume(topic, group, settings.kafka_bootstrap_servers, handler)
    finally:
        await bus.stop()
        await repo.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["transcriber", "summarizer"])
    args = parser.parse_args()
    configure_logging(get_settings().log_level)

    async def _main() -> None:
        task = asyncio.create_task(run(args.stage))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_main())


if __name__ == "__main__":
    main()
