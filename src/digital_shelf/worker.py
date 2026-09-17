"""Background worker process entry point.

Slice 0 deliberately registers no commerce jobs. Later slices add durable job
handlers behind this process without creating another runtime.
"""

import asyncio
import logging
import signal

from digital_shelf.config import get_settings
from digital_shelf.db import create_engine, database_ready
from digital_shelf.logging import configure_logging

LOGGER = logging.getLogger(__name__)


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings.database_url.get_secret_value())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except NotImplementedError:
            pass

    try:
        if not await database_ready(engine):
            raise RuntimeError("database is not ready")
        LOGGER.info("canonical worker scaffold ready; no jobs are registered")
        await stop.wait()
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
