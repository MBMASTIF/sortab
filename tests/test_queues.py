"""Proves the priority claim in core/queues.py by actually running a
worker against fake Redis and checking execution order — not just trusting
the RQ documentation."""

import fakeredis
from rq import SimpleWorker

from core.queues import enqueue_job

execution_order: list[str] = []


def record(label: str) -> None:
    execution_order.append(label)


def test_paid_job_runs_before_earlier_enqueued_free_job():
    execution_order.clear()
    redis_conn = fakeredis.FakeStrictRedis()

    # Free job enqueued FIRST...
    enqueue_job(redis_conn, record, "free-job", is_paid=False)
    # ...then a paid job enqueued SECOND.
    enqueue_job(redis_conn, record, "paid-job", is_paid=True)

    worker = SimpleWorker(["paid", "free"], connection=redis_conn)
    worker.work(burst=True)  # process everything currently queued, then stop

    # If priority works, paid-job must run first despite being enqueued later.
    assert execution_order == ["paid-job", "free-job"]


def test_free_job_still_runs_when_no_paid_jobs_pending():
    execution_order.clear()
    redis_conn = fakeredis.FakeStrictRedis()

    enqueue_job(redis_conn, record, "free-only", is_paid=False)

    worker = SimpleWorker(["paid", "free"], connection=redis_conn)
    worker.work(burst=True)

    assert execution_order == ["free-only"]
