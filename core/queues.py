"""Job queue setup with strict priority: paid jobs are always drained
before free jobs are touched at all.

RQ workers process queues in the order they're listed — a worker created
as Worker(["paid", "free"], ...) will fully empty "paid" before it ever
looks at "free", even if a free job was enqueued earlier. This is RQ's
documented behaviour, not something built here: https://python-rq.org/docs/workers/

Known trade-off, accepted deliberately: this is STRICT priority, not
round-robin. If paid jobs arrive continuously without a gap, free jobs
can be starved indefinitely. At our expected scale (paid users are a
minority of traffic) this is the right default — revisit only if real
monitoring shows free-tier jobs waiting unreasonably long even when paid
load is light.
"""

from redis import Redis
from rq import Queue, Worker

PAID_QUEUE_NAME = "paid"
FREE_QUEUE_NAME = "free"


def make_queues(redis_conn: Redis) -> tuple[Queue, Queue]:
    paid = Queue(PAID_QUEUE_NAME, connection=redis_conn)
    free = Queue(FREE_QUEUE_NAME, connection=redis_conn)
    return paid, free


def start_worker(redis_conn: Redis) -> Worker:
    """Order here is the whole mechanism — paid listed first means paid
    always wins. Do not reorder this without understanding the trade-off
    documented above."""
    return Worker([PAID_QUEUE_NAME, FREE_QUEUE_NAME], connection=redis_conn)


def enqueue_job(redis_conn: Redis, func, *args, is_paid: bool, **kwargs):
    """Single entry point for submitting a processing job — callers should
    never touch Queue objects directly, so the paid/free routing can never
    be bypassed by a call site that forgot which queue to use."""
    queue_name = PAID_QUEUE_NAME if is_paid else FREE_QUEUE_NAME
    queue = Queue(queue_name, connection=redis_conn)
    return queue.enqueue(func, *args, **kwargs)
