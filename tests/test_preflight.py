import json

from hotload import preflight
from hotload.config import Settings
from hotload.preflight import FAIL, OK, WARN, check

HAVE_ALL = lambda tool: f"/usr/bin/{tool}"  # noqa: E731


def levels(findings, topic):
    return {f.level for f in findings if f.topic == topic}


GOOD_HOSTED = dict(quotas_enabled=True, secret_key="x" * 40, public_url="https://hotload.example",
                   database_url="postgresql+asyncpg://u:p@db/hotload", redis_url="redis://redis:6379/0",
                   storage_backend="s3", s3_bucket="b", trust_proxy_headers=True, egress_filtered=True, metrics_token="m" * 20,
                   btcpay_url="https://btcpay.example", btcpay_store_id="s", btcpay_api_key="k", btcpay_webhook_secret="w")


def test_a_correct_hosted_configuration_has_no_failures():
    found = check(Settings(**GOOD_HOSTED), HAVE_ALL)
    assert not [f for f in found if f.level == FAIL], found
    assert levels(found, "secrets") == {OK} and levels(found, "cookies") == {OK}


def test_the_dangerous_mistakes_from_the_review_are_failures_when_hosted():
    bad = dict(quotas_enabled=True, allow_private_targets=True, require_age_gate=False, secret_key="", public_url="http://hotload.example")
    found = check(Settings(**bad), HAVE_ALL)
    for topic in ("ssrf", "compliance", "secrets", "transport", "cookies", "database"):
        assert FAIL in levels(found, topic), topic


def test_self_hosting_is_judged_more_leniently_but_the_ssrf_bypass_still_fails():
    found = check(Settings(), HAVE_ALL)
    assert not [f for f in found if f.level == FAIL]
    assert FAIL in levels(check(Settings(allow_private_targets=True), HAVE_ALL), "ssrf")


def test_https_public_url_makes_cookies_secure_and_explicit_override_is_flagged():
    assert levels(check(Settings(public_url="https://h.example"), HAVE_ALL), "cookies") == {OK}
    assert WARN in levels(check(Settings(public_url="https://h.example", cookie_secure=False), HAVE_ALL), "cookies")


def test_unconfirmed_egress_filter_and_paystack_policy_are_called_out():
    found = check(Settings(quotas_enabled=True, secret_key="x" * 40, paystack_secret_key="sk_live_x"), HAVE_ALL)
    assert WARN in levels(found, "egress") and any("adult" in f.message for f in found if f.topic == "payments")


def test_missing_tools_and_unsafe_integrations():
    found = check(Settings(), lambda t: None)
    assert FAIL in levels(found, "tools") and WARN in levels(found, "tools")
    assert FAIL in levels(check(Settings(btcpay_url="http://btcpay.example"), HAVE_ALL), "payments")
    assert FAIL in levels(check(Settings(telegram_bot_token="1:x"), HAVE_ALL), "bot")
    assert FAIL in levels(check(Settings(storage_backend="s3", s3_bucket="b", s3_prefix=""), HAVE_ALL), "storage")


def test_cli_exit_status_and_json(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "get_settings", lambda: Settings(allow_private_targets=True))
    assert preflight.main([]) == 1 and "FAIL" in capsys.readouterr().out
    monkeypatch.setattr(preflight, "get_settings", lambda: Settings(**GOOD_HOSTED))
    monkeypatch.setattr(preflight.shutil, "which", HAVE_ALL)
    assert preflight.main(["--json"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


MAIL = dict(smtp_host="smtp.example", mail_from="Hotload <no-reply@hotload.example>")


def test_email_checks():
    on = check(Settings(**GOOD_HOSTED, **MAIL), HAVE_ALL)
    assert OK in levels(on, "email") and FAIL not in levels(on, "email")
    half = check(Settings(**{**GOOD_HOSTED, "public_url": None}, smtp_host="smtp.example"), HAVE_ALL)
    assert WARN in levels(half, "email")  # configured host but mail cannot turn on
    plain = check(Settings(**GOOD_HOSTED, **MAIL, smtp_security="none"), HAVE_ALL)
    assert FAIL in levels(plain, "email")  # clear-text SMTP to a remote host, hosted
    local = check(Settings(**{**GOOD_HOSTED}, smtp_host="localhost", mail_from="a@hotload.example", smtp_security="none"), HAVE_ALL)
    assert FAIL not in levels(local, "email")  # a local relay is fine
    stuck = check(Settings(**GOOD_HOSTED, require_verified_email=True), HAVE_ALL)
    assert FAIL in levels(stuck, "email")  # nobody could ever verify


def test_hosted_without_a_mail_provider_is_warned():
    assert WARN in levels(check(Settings(**GOOD_HOSTED), HAVE_ALL), "email")
