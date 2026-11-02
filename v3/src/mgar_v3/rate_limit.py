import hashlib

from redis import Redis
from redis.exceptions import RedisError

from mgar_v3.domain import DomainError

SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
if count > tonumber(ARGV[1]) then return 0 end
return 1
"""


def acquire(redis_url: str, scope: str, limit: int = 30, window_ms: int = 60000) -> None:
    key = "mgar-v3:rate:" + hashlib.sha256(scope.encode()).hexdigest()
    try:
        with Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2) as client:
            accepted = client.eval(SCRIPT, 1, key, limit, window_ms)
    except (RedisError, OSError):
        raise DomainError("RATE_LIMIT_UNAVAILABLE", "New external calls are paused", 503) from None
    if accepted != 1:
        raise DomainError("RATE_LIMITED", "External request rate limit reached", 429)
