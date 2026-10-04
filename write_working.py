#!/usr/bin/env python3
"""Regenerate working_keys.txt / .json from history.json, without the GUI.

Useful for a cron job, a Hermes run, or after a headless check — anything that
should read "which keys actually answered a model" without opening the app.

  python3 tools/write_working.py                      # uses ../history.json
  python3 tools/write_working.py --history other.json --out .
  python3 tools/write_working.py --json               # print JSON to stdout
  python3 tools/write_working.py --print              # print the .txt to stdout

Exit code is 0 always (an empty report is a valid report), unless --require-any
is passed, which makes it exit 1 when no key has answered — handy for alerting.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from report import (  # noqa: E402
    WORKING_TXT,
    format_working_json,
    format_working_txt,
    records_from_history,
    write_working_file,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=str(ROOT / "history.json"))
    ap.add_argument("--out", default=str(ROOT))
    ap.add_argument("--filename", default=WORKING_TXT)
    ap.add_argument("--json", action="store_true", help="print the JSON to stdout")
    ap.add_argument("--print", dest="show", action="store_true", help="print the .txt to stdout")
    ap.add_argument("--require-any", action="store_true",
                    help="exit 1 when nothing has answered")
    args = ap.parse_args()

    records = records_from_history(args.history)
    text, n = format_working_txt(records, source_dir=args.out)

    if args.json:
        print(json.dumps(format_working_json(records), indent=2, ensure_ascii=False))
    elif args.show:
        print(text)
    else:
        txt, js, n = write_working_file(records, args.out, filename=args.filename)
        print(f"wrote {txt}")
        print(f"wrote {js}")
        print(f"{len({r['fingerprint'] for r in format_working_json(records)['records']})} key(s), "
              f"{n} working model(s)")

    if args.require_any and n == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
