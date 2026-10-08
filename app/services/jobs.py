"""Where slow work (tagging, link analysis) runs: after the response, or inside the request."""

from collections.abc import Callable
from typing import Any

from fastapi import BackgroundTasks
from fastapi.concurrency import run_in_threadpool

from app.config import Settings


async def run_or_schedule(
    background: BackgroundTasks, settings: Settings, job: Callable[..., Any], *args: Any
) -> None:
    """A long-running server queues the job for after the response. A serverless function may
    be frozen as soon as it has responded, so with PROCESS_IN_REQUEST the job runs first."""
    if settings.process_in_request:
        await run_in_threadpool(job, *args)
    else:
        background.add_task(job, *args)
