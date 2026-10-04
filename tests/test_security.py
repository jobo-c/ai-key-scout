from types import SimpleNamespace

from security import fingerprint, mask_secret, redact_text, safe_record


def test_fingerprint_is_stable_and_non_secret():
    assert fingerprint("abc") == fingerprint("abc")
    assert "abc" not in fingerprint("abc")


def test_mask_secret():
    assert mask_secret("sk-example-secret") == "sk-e…cret"


def test_redact_text():
    out = redact_text("api_key=SUPERSECRET")
    assert "SUPERSECRET" not in out
    assert "[REDACTED]" in out


def test_safe_record_never_contains_raw_key():
    rec = SimpleNamespace(key="SUPERSECRET", provider="openai", status="valid", score=99)
    out = safe_record(rec)
    assert "SUPERSECRET" not in str(out)
    assert out["masked_key"].startswith("SUPE")
