from models_store import KeyRecord
from providers import PROVIDERS, detect_provider_for_key
from ranking import score_record


def test_v52_tool_registry_has_15_new_services():
    expected = {
        "firecrawl", "browser_use", "browserless", "apify", "serper", "exa",
        "scrapingbee", "scraperapi", "brightdata", "deepgram", "assemblyai",
        "pinecone", "fal", "unstructured", "modal",
    }
    assert expected.issubset(PROVIDERS)
    assert all(PROVIDERS[p].get("provider_kind") == "tool" for p in expected)


def test_v52_strong_tool_detection():
    samples = {
        "fc-abcdefghijklmnopqrstuvwxyz": "firecrawl",
        "bu_abcdefghijklmnopqrstuvwxyz": "browser_use",
        "bless_abcdefghijklmnop": "browserless",
        "apify_api_abcdefghijklmnopqrstuvwxyz": "apify",
        "exa_abcdefghijklmnopqrstuvwxyz": "exa",
        "brd_abcdefghijklmnopqrstuvwxyz": "brightdata",
        "pcsk_abcdefghijklmnopqrstuvwxyz": "pinecone",
        "fal_abcdefghijklmnopqrstuvwxyz": "fal",
        "ak-abcdefghijklmnop": "modal",
    }
    for key, provider in samples.items():
        assert detect_provider_for_key(key) == provider


def test_v52_score_is_bounded_and_has_breakdown():
    rec = KeyRecord("dummy", "browser_use", status="valid")
    rec.details = {
        "provider_kind": "tool",
        "capabilities": ["browser", "agent", "sessions"],
        "http_status": 200,
        "latency_ms": 120,
        "rate_limit": {"remaining": "100"},
        "account": "demo",
    }
    score = score_record(rec)
    assert 0.0 <= score <= 100.0
    assert rec.details["score_version"] == "5.2"
    assert rec.details["score_breakdown"]
