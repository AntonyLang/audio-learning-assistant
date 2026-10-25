"""Celery application with the documented reliability settings (doc §8.4)."""

from __future__ import annotations

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "audio_learning_assistant",
    broker=settings.redis_url,
    backend=None,
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_acks_on_failure_or_timeout=True,
    worker_prefetch_multiplier=1,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    task_ignore_result=True,
    task_soft_time_limit=settings.task_soft_time_limit,
    task_time_limit=settings.task_time_limit,
    broker_transport_options={"visibility_timeout": settings.visibility_timeout},
    result_backend_transport_options={"visibility_timeout": settings.visibility_timeout},
    visibility_timeout=settings.visibility_timeout,
    worker_concurrency=2,
    worker_pool="prefork",
    task_default_queue="ala",
    task_serializer="json",
    accept_content=["json"],
    timezone=settings.app_timezone,
    enable_utc=True,
)

celery_app.autodiscover_tasks(["app.jobs"])
