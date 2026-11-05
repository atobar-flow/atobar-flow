import threading

from atobar_contracts.run_credentials import MintRateLimiter


def test_mint_limiter_allows_once_per_interval_per_key() -> None:
    lim = MintRateLimiter(interval_s=60.0)
    assert lim.try_acquire("run_a", now=100.0)
    assert not lim.try_acquire("run_a", now=159.9)
    assert lim.try_acquire("run_b", now=110.0)
    assert lim.try_acquire("run_a", now=160.0)


def test_mint_limiter_prunes_expired_keys() -> None:
    lim = MintRateLimiter(interval_s=60.0)
    for i in range(50):
        assert lim.try_acquire(f"run_{i}", now=float(i))
    assert lim.try_acquire("fresh", now=1000.0)
    assert len(lim) == 1


def test_mint_limiter_is_atomic_under_concurrency() -> None:
    lim = MintRateLimiter(interval_s=60.0)
    barrier = threading.Barrier(16)
    wins: list[bool] = []

    def worker() -> None:
        barrier.wait()
        wins.append(lim.try_acquire("same_run", now=5.0))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1
