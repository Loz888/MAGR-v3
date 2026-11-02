from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.rate_limit import acquire
from mgar_v3.settings import Settings


def test_real_redis_atomic_rate_limit():
    url = Settings().redis_url.get_secret_value()
    scope = "test:" + uuid4().hex

    def attempt(_):
        try:
            acquire(url, scope, limit=5)
            return True
        except DomainError as exc:
            assert exc.code == "RATE_LIMITED"
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(attempt, range(20))).count(True) == 5


def test_rate_backend_unavailable_pauses_calls():
    with pytest.raises(DomainError) as error:
        acquire("redis://127.0.0.1:1/0", "unavailable")
    assert error.value.code == "RATE_LIMIT_UNAVAILABLE"
