"""Chạy Redis worker pool: python -m repair_estimate_api.worker (số worker theo VRE_API_WORKERS)."""
from redis import Redis
from rq.worker_pool import WorkerPool

from .config import ApiSettings


def main() -> None:
    settings = ApiSettings.from_env()
    connection = Redis.from_url(settings.redis_url)
    WorkerPool([settings.queue_name], connection=connection, num_workers=settings.worker_count).start()


if __name__ == "__main__":
    main()
