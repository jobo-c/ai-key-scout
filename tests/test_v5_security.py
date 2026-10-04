from types import SimpleNamespace

from ranking import format_best_report
from report import format_working_json, format_working_txt


def rec():
    return SimpleNamespace(
        key="SUPERSECRET-DO-NOT-EXPORT",
        provider="openai",
        status="valid",
        score=95,
        remaining=10.0,
        balance_summary="ok",
        info="account",
        models=["gpt-5"],
        working_models=["gpt-5"],
        sources={"fixture"},
        details={"model_tests":[{"model":"gpt-5","ok":True,"tier":"paid","latency_ms":12,"reply":"hi"}]},
    )


def test_working_report_does_not_export_raw_key():
    r = rec()
    txt, _ = format_working_txt([r])
    js = format_working_json([r])
    assert r.key not in txt
    assert r.key not in str(js)
    assert js["records"][0]["fingerprint"]


def test_best_report_does_not_export_raw_key():
    r = rec()
    out = format_best_report([r])
    assert r.key not in out
    assert "fingerprint:" in out
