"""RQ worker entry point.

Run with:
    python apps/worker/main.py
Or directly:
    rq worker triage --url redis://localhost:6379/0
"""

from __future__ import annotations

import logging
import os
import signal
import sys

import structlog
from redis import Redis
from rq import Queue, Worker

from core.config import settings

# ── Logging setup ─────────────────────────────────────────────────────────────
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    ),
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)

log = structlog.get_logger(__name__)


def main() -> None:
    log.info(
        "Starting RQ worker",
        queue=settings.worker_queue_name,
        redis_url=settings.redis_url,
        llm_provider=settings.llm_provider,
    )

    redis_conn = Redis.from_url(settings.redis_url)
    queues = [Queue(settings.worker_queue_name, connection=redis_conn)]

    worker = Worker(queues, connection=redis_conn)

    def _graceful_stop(signum: int, frame: object) -> None:
        log.info("Received signal, stopping worker gracefully", signal=signum)
        worker.stop_heartbeat()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_stop)
    signal.signal(signal.SIGINT, _graceful_stop)

    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
