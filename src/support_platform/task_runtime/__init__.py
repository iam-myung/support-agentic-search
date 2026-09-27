"""Task runtime: PostgreSQL is the sole durable owner of task state and finals."""

from __future__ import annotations

from support_platform.task_runtime.service import (
    count_final_outputs,
    create_queued_investigation,
    get_task,
    mark_running,
    persist_final_output,
)

__all__ = [
    "count_final_outputs",
    "create_queued_investigation",
    "get_task",
    "mark_running",
    "persist_final_output",
]
