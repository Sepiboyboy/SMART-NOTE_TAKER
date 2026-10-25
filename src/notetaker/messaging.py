"""Kafka publishing and consuming."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaConnectionError
from pydantic import ValidationError

from .models import NoteEvent

log = logging.getLogger(__name__)

Handler = Callable[[str], Awaitable[None]]


class EventBus(Protocol):
    async def publish(self, topic: str, event: NoteEvent) -> None: ...


class KafkaEventBus:
    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        self._producer = await _start_with_retry(
            lambda: AIOKafkaProducer(
                bootstrap_servers=self._bootstrap_servers, acks="all", enable_idempotence=True
            )
        )

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()

    async def publish(self, topic: str, event: NoteEvent) -> None:
        # Keying by note id keeps every event for a note on one partition, so a single
        # consumer handles a given note at a time.
        assert self._producer is not None, "KafkaEventBus.start() was not called"
        await self._producer.send_and_wait(
            topic, value=event.model_dump_json().encode(), key=event.note_id.encode()
        )


async def consume(topic: str, group_id: str, bootstrap_servers: str, handler: Handler) -> None:
    """Run `handler` for every event on `topic`, committing offsets only after it succeeds.

    This gives at-least-once delivery. If the handler raises, the process exits without
    committing and the message is redelivered after restart.
    """
    consumer = await _start_with_retry(
        lambda: AIOKafkaConsumer(
            topic,
            bootstrap_servers=bootstrap_servers,
            group_id=group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            # Transcribing a long recording can take minutes; don't get kicked from the group.
            max_poll_interval_ms=15 * 60 * 1000,
        )
    )
    log.info("Consuming %s as %s", topic, group_id)
    try:
        async for message in consumer:
            try:
                event = NoteEvent.model_validate_json(message.value or b"")
            except ValidationError:
                log.error(
                    "Skipping malformed message at offset %s: %r", message.offset, message.value
                )
            else:
                await handler(event.note_id)
            await consumer.commit()
    finally:
        await consumer.stop()


async def _start_with_retry[C: (AIOKafkaProducer, AIOKafkaConsumer)](
    factory: Callable[[], C], attempts: int = 30, delay: float = 2.0
) -> C:
    """Start a fresh client per attempt; Kafka is often still booting when we come up."""
    for attempt in range(1, attempts + 1):
        client = factory()
        try:
            await client.start()
            return client
        except KafkaConnectionError:
            await client.stop()
            if attempt == attempts:
                raise
            log.warning("Kafka not reachable (attempt %d/%d), retrying...", attempt, attempts)
            await asyncio.sleep(delay)
