import pytest

from hotload.config import Settings
from hotload.proxies import DIRECT, NoProxyAvailable, ProxyPool, mask

URL = "https://www.example.com/v"


def test_round_robin_and_no_proxy_means_direct():
    assert ProxyPool().select(URL) is None
    pool = ProxyPool(default=["http://a:1", "http://b:2"])
    assert [pool.select(URL) for _ in range(4)] == ["http://a:1", "http://b:2", "http://a:1", "http://b:2"]


def test_site_routes_use_longest_suffix_and_direct():
    pool = ProxyPool(default=["http://d:1"], routes={"example.com": ["http://e:1"], "www.example.com": [DIRECT]})
    assert pool.select("https://www.example.com/x") is None  # "direct" route
    assert pool.select("https://cdn.example.com/x") == "http://e:1"
    assert pool.select("https://other.org/x") == "http://d:1"


def test_cooldown_after_failures_and_recovery():
    now = [0.0]
    pool = ProxyPool(default=["http://a:1", "http://b:2"], threshold=2, cooldown=100, clock=lambda: now[0])
    for _ in range(2):
        pool.report("http://a:1", False)
    assert [pool.select(URL) for _ in range(3)] == ["http://b:2"] * 3
    assert pool.health()[0]["cooling_down"] is True
    now[0] = 101
    assert "http://a:1" in {pool.select(URL), pool.select(URL)}
    pool.report("http://a:1", True)


def test_all_cooling_still_uses_soonest_never_direct():
    now = [0.0]
    pool = ProxyPool(default=["http://a:1", "http://b:2"], threshold=1, cooldown=100, clock=lambda: now[0])
    pool.report("http://a:1", False)
    now[0] = 10
    pool.report("http://b:2", False)
    assert pool.select(URL) == "http://a:1"  # its cooldown ends first


def test_exclude_exhaustion_raises():
    pool = ProxyPool(default=["http://a:1"])
    with pytest.raises(NoProxyAvailable):
        pool.select(URL, exclude={"http://a:1"})


def test_mask_hides_credentials_and_from_settings():
    assert mask("http://user:secret@proxy.example:8080") == "http://proxy.example:8080"
    pool = ProxyPool.from_settings(Settings(proxy_url="http://x:1", proxy_urls=["http://y:2"]))
    assert pool.default == ["http://y:2", "http://x:1"]
    assert "secret" not in str(ProxyPool(default=["http://u:secret@p:1"]).health())
