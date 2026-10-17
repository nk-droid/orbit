"""A stand-in for Redis that keeps lists and how long each key is to live, in memory."""

from redis.exceptions import ConnectionError as RedisConnectionError


class FakeRedis:
    def __init__(self, *, down: bool = False) -> None:
        self.lists: dict[str, list[str]] = {}
        self.ttl: dict[str, int] = {}
        self.down = down
        self.closed = False

    def _check(self) -> None:
        if self.down:
            raise RedisConnectionError("Error 61 connecting to localhost:6379.")

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        self._check()
        kept = self.lists.get(key, [])
        return kept[start:] if end == -1 else kept[start : end + 1]

    async def expire(self, key: str, seconds: int) -> bool:
        self._check()
        if key not in self.lists:
            return False
        self.ttl[key] = seconds
        return True

    def pipeline(self, transaction: bool = True) -> "Pipeline":
        return Pipeline(self)

    async def aclose(self) -> None:
        self.closed = True


class Pipeline:
    """Commands gathered and then carried out together, as a Redis pipeline does."""

    def __init__(self, store: FakeRedis) -> None:
        self.store, self.queued = store, []

    async def __aenter__(self) -> "Pipeline":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def rpush(self, key: str, value: str) -> None:
        self.queued.append(("rpush", key, value))

    def expire(self, key: str, seconds: int) -> None:
        self.queued.append(("expire", key, seconds))

    async def execute(self) -> list:
        self.store._check()
        for command, key, value in self.queued:
            if command == "rpush":
                self.store.lists.setdefault(key, []).append(value)
            else:
                self.store.ttl[key] = value
        return [True] * len(self.queued)
