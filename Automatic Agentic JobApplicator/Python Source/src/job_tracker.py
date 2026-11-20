"""
JSON tracker for jobs found in job-alert mail: what was screened, what is pending, deferred,
cancelled or applied, with dates. Every change is written to disk straight away.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# pending      - you want to apply (added after screening)
# deferred     - asked again on every run
# cancelled    - you decided not to apply
# applied      - application submitted (you confirmed)
# dropped      - screened out automatically (experience / closed / already applied on LinkedIn)
# not_interested - you declined at the fit check
STATUSES = ("pending", "deferred", "cancelled", "applied", "dropped", "not_interested")
OPEN_STATUSES = ("pending", "deferred")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class JobTracker:
    def __init__(self, path: str):
        self.path = Path(path)
        self.jobs: Dict[str, Dict[str, Any]] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self.jobs = {j["job_id"]: j for j in data.get("jobs", [])}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": now(), "jobs": sorted(self.jobs.values(), key=lambda j: j.get("scraped_at", ""))}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)  # atomic: an interrupted run never leaves a half-written file

    def known(self, job_id: str) -> bool:
        return job_id in self.jobs

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.jobs.get(job_id)

    def add(self, job_id: str, status: str, **fields: Any) -> Dict[str, Any]:
        """Record a screened job (new or re-seen) with its status."""
        job = self.jobs.get(job_id) or {"job_id": job_id, "scraped_at": now(), "history": []}
        job.update({k: v for k, v in fields.items() if v is not None})
        self.jobs[job_id] = job
        self.set_status(job_id, status, fields.get("note", ""))
        return job

    def set_status(self, job_id: str, status: str, note: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status}")
        job = self.jobs[job_id]
        job["status"], job["status_updated_at"] = status, now()
        job.setdefault("history", []).append({"status": status, "at": job["status_updated_at"], **({"note": note} if note else {})})
        self.save()

    def with_status(self, *statuses: str) -> List[Dict[str, Any]]:
        return [j for j in self.jobs.values() if j.get("status") in statuses]

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for j in self.jobs.values():
            out[j.get("status", "?")] = out.get(j.get("status", "?"), 0) + 1
        return out
