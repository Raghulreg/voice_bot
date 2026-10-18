"""Celery application — Redis broker for background jobs."""

from __future__ import annotations

import logging

from celery import Celery

from config import get_settings

logger = logging.getLogger(__name__)


def _broker_url() -> str:
    s = get_settings()
    return (s.celery_broker_url or s.redis_url or "redis://localhost:6379/0").strip()


def _result_url() -> str:
    s = get_settings()
    return (s.celery_result_backend or s.redis_url or "redis://localhost:6379/1").strip()


celery_app = Celery(
    "call_agent",
    broker=_broker_url(),
    backend=_result_url(),
    include=["worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
)


def enqueue_call_finished(call_id: str) -> None:
    """Queue post-call pipeline: summary → analytics → optional email."""
    s = get_settings()
    if not getattr(s, "celery_enabled", False):
        logger.debug("Celery disabled — skipping enqueue call_id=%s", call_id)
        return
    try:
        celery_app.send_task(
            "worker.tasks.process_call_finished",
            args=[call_id],
            ignore_result=True,
        )
        logger.info("Enqueued process_call_finished call_id=%s", call_id)
    except Exception:
        logger.warning(
            "Celery enqueue failed call_id=%s (is Redis running?). Continuing without background jobs.",
            call_id,
            exc_info=True,
        )
