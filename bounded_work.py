"""Concurrency throttling and in-flight request deduplication."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import copy_context
from dataclasses import dataclass
import threading
import time

from agents.runtime import OperationDeadlineExceeded


class WorkBusy(RuntimeError):
    pass


@dataclass
class Job:
    future: object
    deadline: float


_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='arbi-operation')
_slots = threading.BoundedSemaphore(4)
_lock = threading.Lock()
_jobs = {}


def run(key, function, timeout_seconds):
    """Execute a task in a bounded thread pool, deduplicating identical in-flight requests by key."""
    if timeout_seconds <= 0:
        raise OperationDeadlineExceeded('This operation has no time remaining; no work was started.')

    with _lock:
        for old in list(_jobs):
            if _jobs[old].future.done():
                del _jobs[old]

        job = _jobs.get(key)
        if job is None:
            if not _slots.acquire(blocking=False):
                raise WorkBusy('Other operations are still finishing. Please try again shortly.')
            try:
                # Inherit contextvars so budget tracking remains consistent across worker threads
                future = _pool.submit(copy_context().run, function)
            except BaseException:
                _slots.release()
                raise
            future.add_done_callback(lambda _, lease=_slots: lease.release())
            job = _jobs[key] = Job(future, time.monotonic() + timeout_seconds)

    remaining = job.deadline - time.monotonic()
    if remaining <= 0:
        raise OperationDeadlineExceeded('This operation has reached its time limit; its existing request is still finishing. No duplicate request was started.')

    try:
        return job.future.result(timeout=remaining)
    except FutureTimeout as exc:
        raise OperationDeadlineExceeded('This operation reached its time limit. Please retry shortly; an existing request will not be duplicated while it finishes.') from exc
