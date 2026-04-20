"""Job queue — ThreadPoolExecutor-based async job runner with status tracking."""

import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor


class JobQueue:
    """In-memory job queue. max_workers=1 ensures serial execution per repo."""

    def __init__(self, max_queue_depth=3, job_ttl_seconds=3600):
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._jobs = {}  # job_id -> JobState
        self._lock = threading.Lock()
        self._max_queue = max_queue_depth
        self._ttl = job_ttl_seconds

    def submit(self, job_fn, job_meta):
        """Submit a job. Returns (job_id, error_msg).

        job_fn: callable(job_state) that runs the pipeline.
        job_meta: dict with repo, package, strategy, etc.
        """
        self._gc()

        # Check if a job is already running for this repo
        repo = job_meta.get("repo", "")
        with self._lock:
            for jid, js in self._jobs.items():
                if js.meta.get("repo") == repo and js.status in ("queued", "running"):
                    return None, f"Job already active for repo '{repo}': {jid}"

            # Check queue depth
            active = sum(1 for js in self._jobs.values() if js.status in ("queued", "running"))
            if active >= self._max_queue:
                return None, f"Queue full ({active}/{self._max_queue}). Wait for a job to finish."

            job_id = str(uuid.uuid4())
            state = JobState(job_id, job_meta)
            self._jobs[job_id] = state

        future = self._executor.submit(self._run_job, job_fn, state)
        return job_id, None

    def _run_job(self, job_fn, state):
        """Wrapper that catches exceptions and updates state."""
        state.status = "running"
        state.started_at = time.time()
        try:
            job_fn(state)
            if state.status == "running":
                # Determine overall result from steps
                failed = [s for s in state.steps if s["status"] == "failed"]
                state.status = "completed"
                state.result = "failed" if failed else "success"
                if failed:
                    state.failed_step = failed[0]["name"]
        except Exception as e:
            state.status = "completed"
            state.result = "error"
            state.error = str(e)
        finally:
            state.finished_at = time.time()

    def get_status(self, job_id):
        """Get job state dict. Returns None if not found."""
        self._gc()
        state = self._jobs.get(job_id)
        if not state:
            return None
        return state.to_dict()

    def cancel(self, job_id):
        """Mark a job as cancelled. Running step will finish but next steps skipped."""
        state = self._jobs.get(job_id)
        if not state:
            return False
        state.cancelled = True
        state.status = "completed"
        state.result = "cancelled"
        return True

    def _gc(self):
        """Remove expired jobs."""
        now = time.time()
        with self._lock:
            expired = [
                jid for jid, js in self._jobs.items()
                if js.finished_at and (now - js.finished_at) > self._ttl
            ]
            for jid in expired:
                del self._jobs[jid]

    def shutdown(self):
        """Shutdown executor gracefully."""
        self._executor.shutdown(wait=False)


class JobState:
    """Mutable state for a single job. Thread-safe via the queue lock."""

    def __init__(self, job_id, meta):
        self.job_id = job_id
        self.meta = meta
        self.status = "queued"  # queued -> running -> completed
        self.result = None      # success | failed | error | cancelled
        self.started_at = None
        self.finished_at = None
        self.cancelled = False
        self.error = None
        self.failed_step = None
        self.branch = None
        self.steps = []         # list of step dicts
        self.bundle_size = None
        self.verdict = None     # set after pipeline completes

    def add_step(self, name):
        """Add a new step (pending). Returns the step dict."""
        step = {
            "name": name,
            "status": "pending",
            "started_at": None,
            "finished_at": None,
            "duration_s": None,
            "output_tail": "",
        }
        self.steps.append(step)
        return step

    def start_step(self, name):
        """Mark step as running."""
        for s in self.steps:
            if s["name"] == name:
                s["status"] = "running"
                s["started_at"] = time.time()
                return s
        return None

    def finish_step(self, name, passed, output=""):
        """Mark step as passed or failed."""
        for s in self.steps:
            if s["name"] == name:
                s["status"] = "passed" if passed else "failed"
                s["finished_at"] = time.time()
                if s["started_at"]:
                    s["duration_s"] = round(s["finished_at"] - s["started_at"], 1)
                # Keep last 50 lines of output
                lines = output.strip().split("\n")
                s["output_tail"] = "\n".join(lines[-50:])
                return s
        return None

    def skip_remaining(self):
        """Mark all pending steps as skipped."""
        for s in self.steps:
            if s["status"] == "pending":
                s["status"] = "skipped"

    def to_dict(self):
        """Serialize to JSON-safe dict."""
        current = None
        for s in self.steps:
            if s["status"] == "running":
                current = s["name"]
                break

        d = {
            "job_id": self.job_id,
            "status": self.status,
            "result": self.result,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "failed_step": self.failed_step,
            "branch": self.branch,
            "cancelled": self.cancelled,
            "pipeline": {
                "steps": self.steps,
                "current_step": current,
            },
            "bundle_size": self.bundle_size,
            "verdict": self.verdict,
            "meta": self.meta,
        }
        return d
