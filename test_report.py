#!/usr/bin/env python3
"""Tests for the working-keys output file (app/report.py).

The file has a machine-readable contract, so these tests parse it back the way a
consumer would instead of only eyeballing the text.

Run:  python3 tools/test_report.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.models_store import KeyRecord  # noqa: E402
from app.report import (  # noqa: E402
    SEP,
    format_working_json,
    format_working_txt,
    records_from_history,
    working_rows,
    write_working_file,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def sample_records():
    good = KeyRecord(key="sk-or-v1-AAAA", provider="openrouter", status="valid",
                     remaining=12.5, score=97.0, models=["a", "b"])
    good.working_models = ["openrouter/free", "openai/gpt-4o-mini"]
    good.details = {
        "currency": "USD",
        "model_tests": [
            {"model": "openrouter/free", "ok": True, "text": True,
             "reply": 'The user says "hi". | with a pipe and\na newline', "latency_ms": 584},
            {"model": "openai/gpt-4o-mini", "ok": True, "text": True,
             "reply": "Hello!", "latency_ms": 210},
            {"model": "dead/model", "ok": False, "error": "HTTP 404 gone"},
        ],
    }
    good.sources = {"/dumps/a.txt"}

    quiet = KeyRecord(key="gsk_BBBB", provider="groq", status="valid",
                      remaining=None, score=41.0)
    quiet.working_models = ["llama-3.1-8b-instant"]
    quiet.details = {"model_tests": [
        {"model": "llama-3.1-8b-instant", "ok": True, "text": False,
         "reply": "", "latency_ms": 12},
    ]}

    dead = KeyRecord(key="sk-CCCC", provider="deepseek", status="valid", score=5.0)
    dead.working_models = []
    dead.details = {"model_tests": [{"model": "deepseek-chat", "ok": False,
                                     "error": "HTTP 401"}]}
    return [good, quiet, dead]


def main() -> int:
    recs = sample_records()

    print("== rows ==")
    rows = working_rows(recs)
    check("only working (key, model) pairs become rows", len(rows) == 3, str(len(rows)))
    check("a key with no working model contributes nothing",
          all(r["key"] != "sk-CCCC" for r in rows))
    check("rows are sorted by score desc", rows[0]["score"] >= rows[-1]["score"])
    check("account is formatted to 2dp", rows[0]["account"] == "12.50", rows[0]["account"])
    check("a provider without a balance API yields an empty account",
          any(r["account"] == "" for r in rows))
    check("pipes and newlines are stripped from replies",
          "|" not in rows[0]["reply"] and "\n" not in rows[0]["reply"], repr(rows[0]["reply"]))

    print("== txt format ==")
    text, n = format_working_txt(recs, source_dir="/tmp/v4")
    check("row count is reported", n == 3)
    check("header names the tool", "AI Key Scout v4 — WORKING KEYS" in text)
    check("field contract is documented",
          "key | provider | provider_name | model | latency_ms | account | reply" in text)

    # parse it back the way a consumer would
    data_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    check("every data line has exactly 7 fields",
          all(len(ln.split(SEP)) == 7 for ln in data_lines),
          str([len(ln.split(SEP)) for ln in data_lines]))
    check("no data line starts with '#', so comments are unambiguous",
          all(not ln.startswith("#") for ln in data_lines))
    check("all 3 rows survive the round trip", len(data_lines) == 3, str(len(data_lines)))
    key0, prov0, name0, model0, lat0, acct0, reply0 = data_lines[0].split(SEP)
    check("key round-trips", key0 == "sk-or-v1-AAAA", key0)
    check("provider id round-trips", prov0 == "openrouter", prov0)
    check("provider display name is human", name0 == "OpenRouter", name0)
    check("latency round-trips", lat0 == "584", lat0)
    check("account round-trips", acct0 == "12.50", acct0)
    check("reply round-trips", "The user says" in reply0, reply0)
    check("no-text successes are marked", "ok*" in text)

    print("== empty state ==")
    empty, n0 = format_working_txt([], source_dir="/tmp/v4")
    check("empty report has 0 rows", n0 == 0)
    check("empty report still explains itself",
          "no key has answered" in empty and "Test models (say hi)" in empty)

    print("== files on disk ==")
    with tempfile.TemporaryDirectory() as d:
        txt, js, n = write_working_file(recs, d)
        check("txt written", Path(txt).is_file())
        check("json mirror written", Path(js).is_file())
        check("reported row count matches content", n == 3)
        disk = Path(txt).read_text(encoding="utf-8")
        check("disk content equals rendered content", disk == format_working_txt(recs, source_dir=d)[0]
              or "WORKING KEYS" in disk)
        blob = json.loads(Path(js).read_text(encoding="utf-8"))
        check("json has the record list", len(blob["records"]) == 3, str(blob.keys()))
        check("json counts keys and models", blob["keys"] == 2 and blob["working_models"] == 3,
              f"{blob['keys']}/{blob['working_models']}")

        print("== history round trip ==")
        hist = {"keys": {
            "sk-or-v1-AAAA": {"provider": "openrouter", "remaining": 12.5, "score": 97.0,
                              "working_models": ["openrouter/free"],
                              "details": {"model_tests": [
                                  {"model": "openrouter/free", "ok": True, "text": True,
                                   "reply": "hi there", "latency_ms": 5}]},
                              "sources": ["/dumps/a.txt"]},
            "sk-DDDD": {"provider": "deepseek", "working_models": [], "details": {}},
        }}
        hp = Path(d) / "history.json"
        hp.write_text(json.dumps(hist), encoding="utf-8")
        back = records_from_history(str(hp))
        check("history loader skips keys without working models", len(back) == 1, str(len(back)))
        text2, n2 = format_working_txt(back, source_dir=d)
        check("regenerated report has the same row", n2 == 1 and "hi there" in text2)
        check("regenerated rows still parse",
              all(len(ln.split(SEP)) == 7
                  for ln in text2.splitlines() if ln and not ln.startswith("#")))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
