#!/usr/bin/env python3
"""AI Key Scout v5 entry point."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from datetime import datetime


def setup_logging(here: str) -> str:
    log_path = os.path.join(here, "debug_log.txt")
    level_name = os.environ.get("KEY_SCOUT_LOG", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logging.info("=" * 60)
    logging.info("AI Key Scout v4 starting  log=%s  level=%s", log_path, level_name)
    logging.info("time=%s  cwd=%s", datetime.now().isoformat(timespec="seconds"), here)
    return log_path


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    log_path = setup_logging(here)
    log = logging.getLogger("main")

    def _excepthook(etype, exc, tb):
        msg = "".join(traceback.format_exception(etype, exc, tb))
        logging.error("UNCAUGHT\n%s", msg)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("UNCAUGHT " + msg + "\n")
        except Exception:
            pass
        sys.__excepthook__(etype, exc, tb)

    sys.excepthook = _excepthook

    try:
        from PyQt6.QtWidgets import QApplication
        from gui import MainWindow
        from providers import PROVIDERS
        import __init__ as package_info

        log.info("version=%s  providers=%d", package_info.__version__, len(PROVIDERS))
        app = QApplication(sys.argv)
        app.setApplicationName("AI Key Scout")
        win = MainWindow()
        win.show()
        log.info("GUI shown - interact with the window; logs stream here and to debug_log.txt")
        code = app.exec()
        log.info("GUI closed  exit=%s", code)
        return int(code)
    except Exception:
        log.exception("Failed to start GUI")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
