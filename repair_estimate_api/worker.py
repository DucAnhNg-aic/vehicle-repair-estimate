"""Chạy Redis worker: python -m repair_estimate_api.worker."""
from redis import Redis
from rq import Queue, Worker

from .config import ApiSettings


def main() -> None:
    settings = ApiSettings.from_env()
    connection = Redis.from_url(settings.redis_url)
    Worker([Queue(settings.queue_name, connection=connection)], connection=connection).work()


if __name__ == "__main__":
    main()
