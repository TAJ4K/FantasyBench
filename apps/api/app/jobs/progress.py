from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.entities import JobRun


class ManagerJobProgress:
    """Persist each completed manager action so a retry only pays for unfinished work."""

    def __init__(self, factory: sessionmaker[Session], job_id: str, attempt: int) -> None:
        self.factory = factory
        self.job_id = job_id
        self.attempt = attempt
        with factory() as db:
            job = db.get(JobRun, job_id)
            if job is None or job.status != "RUNNING" or job.attempt_count != attempt:
                raise RuntimeError("job lease lost before resuming manager progress")
            self.results: dict[str, str] = {
                key: value
                for key, value in (job.details or {}).items()
                if not key.startswith("_") and isinstance(value, str)
            }

    def complete(self, key: str) -> bool:
        value = self.results.get(key)
        return value is not None and not value.startswith("FAILED:")

    def record(self, key: str, value: str) -> None:
        with self.factory() as db:
            job = db.scalar(select(JobRun).where(JobRun.id == self.job_id).with_for_update())
            if job is None or job.status != "RUNNING" or job.attempt_count != self.attempt:
                raise RuntimeError("job lease lost while recording manager progress")
            job.details = {**(job.details or {}), key: value}
            db.commit()
        self.results[key] = value
