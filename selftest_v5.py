#!/usr/bin/env python3
"""AI Key Scout v5 local self-test.

Run with:
    python selftest_v5.py
"""
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    cmd = [sys.executable, "-m", "pytest"]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
