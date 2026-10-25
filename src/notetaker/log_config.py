import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # The Kafka client is very chatty at INFO.
    logging.getLogger("aiokafka").setLevel(logging.WARNING)
