"""Application-level mirror of the task/step transition guards in SQL."""

TASK_TRANSITIONS = {
    "QUEUED": {"PLANNING", "RECOVERING", "FAILED", "CANCELLED"},
    "PLANNING": {"RUNNING", "RECOVERING", "FAILED", "CANCELLED"},
    "RUNNING": {"VERIFYING", "RECOVERING", "COMPLETED", "COMPLETED_WITH_LIMITATIONS", "FAILED", "CANCELLED"},
    "VERIFYING": {"RUNNING", "RECOVERING", "COMPLETED", "COMPLETED_WITH_LIMITATIONS", "FAILED", "CANCELLED"},
    "RECOVERING": {"PLANNING", "RUNNING", "COMPLETED", "COMPLETED_WITH_LIMITATIONS", "FAILED", "CANCELLED"},
    "FAILED": {"RECOVERING"},
    "COMPLETED_WITH_LIMITATIONS": {"RECOVERING"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}

STEP_TRANSITIONS = {
    "PENDING": {"RUNNING", "SKIPPED"},
    "RUNNING": {"RETRYING", "SUCCEEDED", "VERIFIED", "FAILED", "SKIPPED"},
    "RETRYING": {"RUNNING", "FAILED", "SKIPPED"},
    "FAILED": {"RETRYING", "RUNNING"},
    "SUCCEEDED": {"VERIFIED"},
    "VERIFIED": set(),
    "SKIPPED": set(),
    "AWAITING_APPROVAL": set(),
}


def validate_transition(current: str, target: str, *, kind: str) -> None:
    allowed = TASK_TRANSITIONS if kind == "task" else STEP_TRANSITIONS
    if target != current and target not in allowed.get(current, set()):
        raise ValueError(f"Illegal {kind} status transition: {current} -> {target}")


def validate_task_completion(status: str, step_statuses: list[str]) -> None:
    if status not in {"COMPLETED", "COMPLETED_WITH_LIMITATIONS"}:
        return
    if any(step in {"PENDING", "RUNNING", "RETRYING"} for step in step_statuses):
        raise ValueError("A task cannot finish while a step remains unresolved")
