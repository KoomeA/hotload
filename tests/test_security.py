import pytest

from hotload.config import Settings
from hotload.security import UnsafeURL, validate_url


def PUBLIC(host, port):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def PRIVATE(host, port):
    return [(2, 1, 6, "", ("10.0.0.5", port))]


def test_accepts_public_https():
    assert validate_url("https://example.com/v?x=1", Settings(), resolve=PUBLIC) == "https://example.com/v?x=1"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/a", "javascript:alert(1)",
    "http://user:pw@example.com/", "http:///nohost", "http://127.0.0.1/", "http://[::1]/",
    "http://169.254.169.254/latest/meta-data", "http://10.1.2.3/", "http://192.168.0.1:8080/",
])
def test_rejects_unsafe(url):
    with pytest.raises(UnsafeURL):
        validate_url(url, Settings(), resolve=PUBLIC)


def test_rejects_hostname_resolving_to_private():
    with pytest.raises(UnsafeURL):
        validate_url("https://evil.example/", Settings(), resolve=PRIVATE)


def test_blocklist_and_allowlist():
    with pytest.raises(UnsafeURL):
        validate_url("https://sub.bad.example/", Settings(blocked_hosts=["bad.example"]), resolve=PUBLIC)
    with pytest.raises(UnsafeURL):
        validate_url("https://other.example/", Settings(allowed_hosts=["good.example"]), resolve=PUBLIC)
    validate_url("https://cdn.good.example/", Settings(allowed_hosts=["good.example"]), resolve=PUBLIC)


def test_rejects_overlong():
    with pytest.raises(UnsafeURL):
        validate_url("https://example.com/" + "a" * 3000, Settings(), resolve=PUBLIC)
