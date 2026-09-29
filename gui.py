"""Simple Best-first GUI — paste/scan/check without freezing."""

from __future__ import annotations

import csv
import json
import logging
import os
from typing import Optional

log = logging.getLogger("gui")

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QListWidget, QListWidgetItem,
    QFileDialog, QProgressBar, QCheckBox, QComboBox, QDoubleSpinBox, QSpinBox,
    QMessageBox, QGroupBox, QSplitter, QDialog, QDialogButtonBox, QFormLayout,
    QStatusBar, QAbstractItemView, QTableWidget, QTableWidgetItem, QHeaderView,
    QMenu,
)

from . import __version__
from .models_store import KeyStore
from .providers import (
    PROVIDERS, detect_keys_in_text, provider_name, all_provider_ids,
    count_by_provider, detectable_provider_ids,
)
from .ranking import format_best_report, is_best_candidate, score_record, sort_best
from .workers import ScanWorker, CheckWorker, ModelTestWorker
from . import history as key_history
from . import report as key_report


DEFAULT_EXTS = ".txt"  # fastest default — only plaintext dumps
ALL_LIST_CAP = 800          # never render thousands of table rows
CHECK_WARN_AT = 300         # confirm before huge checks
CHECK_HARD_CAP = 10000      # refuse beyond this in one run (prevents crash)
TABLE_COLS = ["Status", "Provider", "Remaining", "Score", "Works", "Summary", "Key", "Context"]
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class SortItem(QTableWidgetItem):
    """Keep display text clean; numeric sort via SORT_ROLE (fixes weird Remaining/Score)."""

    def __lt__(self, other: QTableWidgetItem) -> bool:  # type: ignore[override]
        a = self.data(SORT_ROLE)
        b = other.data(SORT_ROLE)
        if a is not None and b is not None:
            try:
                return float(a) < float(b)
            except Exception:
                pass
        return (self.text() or "") < (other.text() or "")


class SettingsDialog(QDialog):
    def __init__(self, parent, settings: dict):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(380)
        form = QFormLayout(self)
        self.conc = QSpinBox(); self.conc.setRange(1, 100); self.conc.setValue(settings.get("concurrency", 40))
        self.timeout = QSpinBox(); self.timeout.setRange(3, 60); self.timeout.setValue(settings.get("timeout", 10))
        self.maxmb = QSpinBox(); self.maxmb.setRange(0, 8192); self.maxmb.setValue(settings.get("max_mb", 0))
        self.maxmb.setSpecialValueText("unlimited")
        self.proxy = QLineEdit(settings.get("proxy", ""))
        self.proxy.setPlaceholderText("http://user:pass@host:port (optional)")
        form.addRow("Check concurrency", self.conc)
        form.addRow("Timeout (seconds)", self.timeout)
        form.addRow("Max MB / file (0=whole file)", self.maxmb)
        form.addRow("Proxy", self.proxy)
        self.deep = QCheckBox("Deep check only (skip the cheap sweep; slowest)")
        self.deep.setChecked(bool(settings.get("deep_check", False)))
        form.addRow("Check mode", self.deep)
        self.enrich = QCheckBox("Enrich valid keys afterwards (balances, quotas, catalogs)")
        self.enrich.setChecked(bool(settings.get("enrich_valid", True)))
        form.addRow("Post-check", self.enrich)
        self.testmodels = QCheckBox("Test models automatically after a check (says 'hi')")
        self.testmodels.setChecked(bool(settings.get("test_models", False)))
        form.addRow("", self.testmodels)
        self.perkey = QSpinBox(); self.perkey.setRange(1, 8)
        self.perkey.setValue(int(settings.get("models_per_key", 3)))
        self.perkey.setToolTip("How many models to try per key (stops early once one answers)")
        form.addRow("Models tested / key", self.perkey)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> dict:
        return {
            "concurrency": self.conc.value(),
            "timeout": self.timeout.value(),
            "max_mb": self.maxmb.value(),
            "proxy": self.proxy.text().strip(),
            "deep_check": self.deep.isChecked(),
            "enrich_valid": self.enrich.isChecked(),
            "test_models": self.testmodels.isChecked(),
            "models_per_key": self.perkey.value(),
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"AI Key Scout v{__version__}")
        self.resize(1180, 760)

        self.store = KeyStore()
        # max_mb 0 = no size skip (v2 read-whole-file + regex)
        self.settings = {"concurrency": 40, "timeout": 10, "max_mb": 0, "proxy": "",
                         "deep_check": False, "enrich_valid": True,
                         "test_models": False, "models_per_key": 3}
        self.scan_worker: Optional[ScanWorker] = None
        self.check_worker: Optional[CheckWorker] = None
        self.model_worker: Optional[ModelTestWorker] = None
        self._selected_key: Optional[str] = None
        self._ui_dirty = False
        self._check_ok = 0
        self._check_bad = 0
        self._check_retry = 0
        self._model_tested = 0
        self._model_worked = 0
        self._model_autostart = False
        self._working_writes = 0
        self._busy = False

        # Batch UI redraws — rebuilding lists on every key was freezing/crashing at 6k+ keys
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(500)
        self._refresh_timer.timeout.connect(self._flush_ui_refresh)

        self._build_ui()
        self._apply_style()
        self._set_status("Ready — paste keys or scan a folder of .txt dumps")
        log.info("MainWindow ready")

    # ---------- UI ----------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 12, 12, 8)
        outer.setSpacing(8)

        # Header
        header = QHBoxLayout()
        title = QLabel("AI Key Scout")
        title.setObjectName("title")
        sub = QLabel("Paste or scan → Check → Best keys on top")
        sub.setObjectName("subtitle")
        header.addWidget(title)
        header.addWidget(sub)
        header.addStretch(1)
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.open_settings)
        self.export_btn = QPushButton("Export best")
        self.export_btn.clicked.connect(self.export_best)
        self.save_hist_btn = QPushButton("Save history")
        self.save_hist_btn.setToolTip("Append current keys into history.json (keeps growing across runs)")
        self.save_hist_btn.clicked.connect(self.save_history)
        self.load_hist_btn = QPushButton("Load history")
        self.load_hist_btn.setToolTip("Load keys from history.json into the table")
        self.load_hist_btn.clicked.connect(self.load_history)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_all)
        header.addWidget(self.settings_btn)
        header.addWidget(self.export_btn)
        header.addWidget(self.save_hist_btn)
        header.addWidget(self.load_hist_btn)
        header.addWidget(self.clear_btn)
        outer.addLayout(header)

        split = QSplitter(Qt.Orientation.Horizontal)

        # LEFT: add keys
        left = QWidget()
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 8, 0)

        g_add = QGroupBox("1. Add keys")
        a = QVBoxLayout(g_add)
        self.paste = QTextEdit()
        self.paste.setPlaceholderText(
            "Paste API keys here (one per line or mixed dump text).\n"
            "OpenRouter, OpenAI, Anthropic, Groq, DeepSeek, Gemini, …\n\n"
            "Then click Check keys."
        )
        self.paste.setMinimumHeight(160)
        a.addWidget(self.paste)

        row_chk = QHBoxLayout()
        self.check_btn = QPushButton("Check keys")
        self.check_btn.setObjectName("primary")
        self.check_btn.setToolTip("Check pending + all keys in the current provider filter")
        self.check_btn.clicked.connect(lambda: self.start_check(mode="all"))
        self.recheck_invalid_btn = QPushButton("Recheck invalids")
        self.recheck_invalid_btn.setToolTip("Only re-check keys that previously failed (invalid)")
        self.recheck_invalid_btn.clicked.connect(lambda: self.start_check(mode="invalid"))
        self.recheck_google_btn = QPushButton("Recheck dead Google")
        self.recheck_google_btn.setToolTip(
            "Re-test previously-failed Google keys through the Gemini probe "
            "to recover real Gemini keys wrongly marked BAD."
        )
        self.recheck_google_btn.clicked.connect(self.recheck_dead_google)
        self.test_models_btn = QPushButton("Test models (say hi)")
        self.test_models_btn.setToolTip(
            "For every VALID key, send a real one-token 'hi' to a cheap model and\n"
            "record which models actually answered. Proves the key works, not just\n"
            "that it authenticated."
        )
        self.test_models_btn.clicked.connect(self.start_model_test)
        self.stop_check_btn = QPushButton("Stop")
        self.stop_check_btn.setEnabled(False)
        self.stop_check_btn.clicked.connect(self.stop_check)
        self.load_file_btn = QPushButton("Load file…")
        self.load_file_btn.clicked.connect(self.load_file)
        row_chk.addWidget(self.check_btn)
        row_chk.addWidget(self.recheck_invalid_btn)
        row_chk.addWidget(self.recheck_google_btn)
        row_chk.addWidget(self.test_models_btn)
        row_chk.addWidget(self.stop_check_btn)
        row_chk.addWidget(self.load_file_btn)
        a.addLayout(row_chk)

        # Scan
        scan_row = QHBoxLayout()
        self.dir_edit = QLineEdit("")
        self.dir_edit.setPlaceholderText("Or scan a folder (great for .txt dumps)")
        browse = QPushButton("Browse")
        browse.clicked.connect(self.browse_dir)
        scan_row.addWidget(self.dir_edit)
        scan_row.addWidget(browse)
        a.addLayout(scan_row)
        ext_row = QHBoxLayout()
        self.ext_edit = QLineEdit(DEFAULT_EXTS)
        self.ext_edit.setPlaceholderText(".txt")
        self.ext_edit.setMaximumWidth(160)
        self.txt_only_btn = QPushButton(".txt only")
        self.txt_only_btn.setToolTip("Fastest: reset extensions to .txt only")
        self.txt_only_btn.clicked.connect(lambda: self.ext_edit.setText(".txt"))
        ext_row.addWidget(QLabel("Exts"))
        ext_row.addWidget(self.ext_edit)
        ext_row.addWidget(self.txt_only_btn)
        ext_row.addStretch(1)
        a.addLayout(ext_row)
        self.scan_all = QCheckBox("Scan ALL file types (slow — avoid)")
        self.scan_all.setChecked(False)
        a.addWidget(self.scan_all)
        mb_row = QHBoxLayout()
        mb_row.addWidget(QLabel("Max MB / file"))
        self.maxmb_spin = QSpinBox()
        self.maxmb_spin.setRange(0, 8192)
        self.maxmb_spin.setValue(int(self.settings.get("max_mb", 0)))
        self.maxmb_spin.setSpecialValueText("no limit")
        self.maxmb_spin.setToolTip(
            "Skip files larger than this (same as v2).\n"
            "0 = no skip — read the whole file into RAM then regex (fast)."
        )
        self.maxmb_spin.valueChanged.connect(self._on_maxmb_changed)
        mb_row.addWidget(self.maxmb_spin)
        mb_row.addStretch(1)
        a.addLayout(mb_row)
        scan_btns = QHBoxLayout()
        self.scan_btn = QPushButton("Scan .txt")
        self.scan_btn.setObjectName("primary")
        self.scan_btn.setToolTip(
            "Same fast scanner as v2: walk folder, read each file, search key prefixes.\n"
            "Max MB / file 0 = scan everything including GB dumps."
        )
        self.scan_btn.clicked.connect(self.start_scan)
        self.stop_scan_btn = QPushButton("Stop scan")
        self.stop_scan_btn.setEnabled(False)
        self.stop_scan_btn.clicked.connect(self.stop_scan)
        scan_btns.addWidget(self.scan_btn)
        scan_btns.addWidget(self.stop_scan_btn)
        a.addLayout(scan_btns)
        self.scan_prog = QProgressBar()
        self.scan_prog.setTextVisible(False)
        a.addWidget(self.scan_prog)
        left_l.addWidget(g_add)

        # Filters
        g_f = QGroupBox("2. Filters")
        f = QGridLayout(g_f)
        self.prov_filter = QComboBox()
        self.prov_filter.currentIndexChanged.connect(self.refresh_lists)
        self.prov_hint = QLabel(
            "Filter applies to list + Check. Auto-detect: "
            + ", ".join(provider_name(p) for p in sorted(detectable_provider_ids(), key=provider_name)[:8])
            + "…"
        )
        self.prov_hint.setWordWrap(True)
        self.prov_hint.setStyleSheet("color:#9aa0a6; font-size:11px;")
        self.valid_only = QCheckBox("Valid only")
        self.valid_only.setChecked(False)
        self.valid_only.stateChanged.connect(self.refresh_lists)
        self.min_rem = QDoubleSpinBox()
        self.min_rem.setRange(0.0, 100000.0)
        self.min_rem.setDecimals(2)
        self.min_rem.setValue(0.0)
        self.min_rem.setPrefix("Min $ ")
        self.min_rem.setToolTip("Hide keys below this remaining balance (0 = show all)")
        self.min_rem.valueChanged.connect(self.refresh_lists)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search key / info…")
        self.search.textChanged.connect(self.refresh_lists)
        f.addWidget(QLabel("Provider"), 0, 0)
        f.addWidget(self.prov_filter, 0, 1)
        f.addWidget(self.prov_hint, 1, 0, 1, 2)
        f.addWidget(self.valid_only, 2, 0, 1, 2)
        f.addWidget(self.min_rem, 3, 0, 1, 2)
        f.addWidget(self.search, 4, 0, 1, 2)
        left_l.addWidget(g_f)
        left_l.addStretch(1)
        self._rebuild_provider_filter()

        # RIGHT: keys table (main) + detail
        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(8, 0, 0, 0)

        g_all = QGroupBox("3. Keys — drag column edges to resize · right-click header to show/hide · click header to sort")
        al = QVBoxLayout(g_all)
        self.keys_table = QTableWidget(0, len(TABLE_COLS))
        self.keys_table.setHorizontalHeaderLabels(TABLE_COLS)
        self.keys_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.keys_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.keys_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.keys_table.setAlternatingRowColors(True)
        self.keys_table.verticalHeader().setVisible(False)
        self.keys_table.setSortingEnabled(True)
        self.keys_table.setWordWrap(False)
        hdr = self.keys_table.horizontalHeader()
        hdr.setSectionsMovable(True)  # drag columns to reorder
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)  # user-adjustable widths
        hdr.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self._header_menu)
        # Sensible default widths (user can drag to change)
        for i, w in enumerate([72, 120, 100, 64, 150, 300, 150, 280]):
            self.keys_table.setColumnWidth(i, w)
        self.keys_table.itemSelectionChanged.connect(self.on_table_selected)
        al.addWidget(self.keys_table)
        self.check_prog = QProgressBar()
        al.addWidget(self.check_prog)
        right_l.addWidget(g_all, stretch=5)

        g_det = QGroupBox("Details")
        d = QVBoxLayout(g_det)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(160)
        d.addWidget(self.detail)
        det_btns = QHBoxLayout()
        self.copy_key_btn = QPushButton("Copy key")
        self.copy_key_btn.clicked.connect(self.copy_selected_key)
        self.copy_sum_btn = QPushButton("Copy summary")
        self.copy_sum_btn.clicked.connect(self.copy_selected_summary)
        det_btns.addWidget(self.copy_key_btn)
        det_btns.addWidget(self.copy_sum_btn)
        det_btns.addStretch(1)
        d.addLayout(det_btns)
        right_l.addWidget(g_det, stretch=2)

        # keep attrs so old code paths don't crash if referenced
        self.best_list = None
        self.all_list = None

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        outer.addWidget(split, stretch=1)

        self.setStatusBar(QStatusBar())

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#12141a; color:#e8eaed; font-family:'Segoe UI', Consolas, sans-serif; font-size:13px; }
            QLabel#title { font-size:22px; font-weight:700; color:#fff; }
            QLabel#subtitle { color:#9aa0a6; padding-left:10px; }
            QGroupBox { border:1px solid #2a2f3a; border-radius:10px; margin-top:12px; padding:12px; background:#181b22; }
            QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 6px; color:#8ab4f8; font-weight:600; }
            QPushButton { background:#2a2f3a; border:1px solid #3c4454; border-radius:8px; padding:8px 14px; }
            QPushButton:hover { background:#343b4a; }
            QPushButton:disabled { color:#666; background:#1c1f27; }
            QPushButton#primary { background:#1a73e8; border:none; font-weight:600; color:white; }
            QPushButton#primary:hover { background:#1765cc; }
            QTextEdit, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QListWidget, QTableWidget {
                background:#0e1016; border:1px solid #2a2f3a; border-radius:8px; padding:6px; color:#e8eaed;
            }
            QListWidget::item { padding:10px; border-bottom:1px solid #22262f; }
            QListWidget::item:selected { background:#1a73e8; color:white; }
            QTableWidget { gridline-color:#2a2f3a; alternate-background-color:#141820; }
            QTableWidget::item:selected { background:#1a73e8; color:white; }
            QHeaderView::section { background:#1c212b; color:#8ab4f8; padding:6px; border:1px solid #2a2f3a; }
            QProgressBar { background:#0e1016; border:1px solid #2a2f3a; border-radius:6px; min-height:10px; }
            QProgressBar::chunk { background:#1a73e8; border-radius:6px; }
            QStatusBar { background:#0e1016; color:#9aa0a6; }
            QCheckBox { spacing:8px; }
        """)

    def _set_status(self, msg: str):
        self.statusBar().showMessage(msg)

    def _provider_breakdown(self) -> str:
        counts = count_by_provider(self.store.all())
        if not counts:
            return "no keys"
        parts = [
            f"{provider_name(pid)}={n}"
            for pid, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return ", ".join(parts[:12]) + (f" (+{len(parts) - 12} more)" if len(parts) > 12 else "")

    def _rebuild_provider_filter(self):
        """Dropdown shows live counts so it's obvious we handle more than OpenRouter."""
        cur = self.prov_filter.currentData() if self.prov_filter.count() else ""
        counts = count_by_provider(self.store.all())
        self.prov_filter.blockSignals(True)
        self.prov_filter.clear()
        total = len(self.store)
        self.prov_filter.addItem(f"All providers ({total})", "")
        # Providers present in store first, then the rest
        present = sorted(counts.keys(), key=lambda p: (-counts[p], provider_name(p).lower()))
        rest = [p for p in sorted(all_provider_ids(), key=lambda p: provider_name(p).lower()) if p not in counts]
        for pid in present + rest:
            n = counts.get(pid, 0)
            label = f"{provider_name(pid)} ({n})" if n else provider_name(pid)
            self.prov_filter.addItem(label, pid)
        # restore selection
        idx = 0
        for i in range(self.prov_filter.count()):
            if self.prov_filter.itemData(i) == cur:
                idx = i
                break
        self.prov_filter.setCurrentIndex(idx)
        self.prov_filter.blockSignals(False)

    def _request_ui_refresh(self):
        """Mark UI dirty; actual redraw happens on timer (prevents freeze)."""
        self._ui_dirty = True
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _flush_ui_refresh(self):
        if not self._ui_dirty:
            if not self._busy:
                self._refresh_timer.stop()
            return
        self._ui_dirty = False
        self._rebuild_provider_filter()
        self.refresh_lists()
        if not self._busy:
            self._refresh_timer.stop()

    # ---------- settings / files ----------
    def _on_maxmb_changed(self, value: int):
        self.settings["max_mb"] = int(value)

    def open_settings(self):
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings.update(dlg.values())
            self.maxmb_spin.blockSignals(True)
            self.maxmb_spin.setValue(int(self.settings.get("max_mb", 0)))
            self.maxmb_spin.blockSignals(False)
            self._set_status("Settings saved")

    def browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select folder to scan", self.dir_edit.text() or os.path.expanduser("~"))
        if d:
            self.dir_edit.setText(d)

    def load_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load keys file", "", "Text (*.txt *.csv *.json *.log *.env);;All (*.*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            QMessageBox.warning(self, "Load", str(e))
            return
        self.paste.setPlainText(text)
        n = self._ingest_text(text, source=path)
        self._rebuild_provider_filter()
        self.refresh_lists()
        self._set_status(f"Loaded file · {n} new · store={len(self.store)} · {self._provider_breakdown()}")
        log.info("Loaded file %s · %d new keys · store=%d · %s", path, n, len(self.store), self._provider_breakdown())

    def clear_all(self):
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.stop()
        if self.check_worker and self.check_worker.isRunning():
            self.check_worker.stop()
        self.store.clear()
        self.paste.clear()
        self.detail.clear()
        self._selected_key = None
        self._rebuild_provider_filter()
        self.refresh_lists()
        self._set_status("Cleared")

    # ---------- ingest ----------
    def _ingest_text(self, text: str, source: str = "pasted") -> int:
        added = 0
        for hit in detect_keys_in_text(text):
            if len(hit) >= 3:
                key, pid, ctx = hit[0], hit[1], hit[2]
            else:
                key, pid, ctx = hit[0], hit[1], ""
            before = len(self.store)
            self.store.add(key, pid, source=source, context=ctx)
            if len(self.store) > before:
                added += 1
        return added

    def _ingest_paste_box(self) -> int:
        return self._ingest_text(self.paste.toPlainText(), source="pasted")

    # ---------- scan ----------
    def start_scan(self):
        root = self.dir_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.information(self, "Scan", "Pick a folder first.")
            return
        exts = {e.strip().lower() for e in self.ext_edit.text().split(",") if e.strip()}
        if not exts:
            exts = {".txt"}
            self.ext_edit.setText(".txt")
        # Normalize: ensure leading dots
        exts = {e if e.startswith(".") else f".{e}" for e in exts}
        if self.scan_all.isChecked():
            r = QMessageBox.question(
                self, "Slow scan?",
                "Scan ALL file types is much slower.\n\nPrefer .txt only for speed. Continue anyway?",
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        self.scan_btn.setEnabled(False)
        self.stop_scan_btn.setEnabled(True)
        self.scan_prog.setRange(0, 0)
        self._busy = True
        max_mb = int(self.maxmb_spin.value())
        self.settings["max_mb"] = max_mb
        self.scan_worker = ScanWorker(
            root, exts, max_mb=max_mb, scan_all=self.scan_all.isChecked(),
            batch_size=200,
        )
        self.scan_worker.found_batch.connect(self.on_scan_batch)
        self.scan_worker.progress.connect(self._set_status)
        self.scan_worker.done.connect(self.on_scan_done)
        self.scan_worker.start()
        self._request_ui_refresh()
        lim = "no size skip" if max_mb <= 0 else f"skip >{max_mb} MB"
        self._set_status(f"Scanning (v2) · {lim}")
        log.info("Scan started root=%s exts=%s max_mb=%s", root, sorted(exts), max_mb)

    def stop_scan(self):
        if self.scan_worker:
            self.scan_worker.stop()
            self._set_status("Stopping scan…")

    def on_scan_batch(self, batch: list):
        for item in batch:
            if len(item) >= 4:
                key, provider, source, ctx = item[0], item[1], item[2], item[3]
            else:
                key, provider, source, ctx = item[0], item[1], item[2], ""
            self.store.add(key, provider, source=source, context=ctx)
        self._request_ui_refresh()

    def on_scan_done(self, count: int, diag: dict):
        self._busy = False
        self.scan_btn.setEnabled(True)
        self.stop_scan_btn.setEnabled(False)
        self.scan_prog.setRange(0, 1)
        self.scan_prog.setValue(1)
        self._ui_dirty = True
        self._flush_ui_refresh()
        raw = diag.get("raw_matches", count)
        method = diag.get("method", "?")
        checked = diag.get("files_checked", "?")
        skipped_sz = diag.get("files_skipped_size", 0)
        self._rebuild_provider_filter()
        extra = f" · skipped {skipped_sz} oversized" if skipped_sz else ""
        self._set_status(
            f"Scan done ({method}) · {checked} files · {raw} raw · "
            f"{len(self.store)} unique · {self._provider_breakdown()}{extra}"
        )
        log.info("Scan done unique=%s store=%s providers=%s diag=%s", count, len(self.store), self._provider_breakdown(), diag)
        self._autosave_history("scan")
        if len(self.store) > CHECK_WARN_AT:
            QMessageBox.information(
                self, "Large scan",
                f"Found {len(self.store)} unique keys.\n\n"
                f"Tip: filter by provider before Check, or Check will ask to confirm.\n"
                f"Hard limit per check run: {CHECK_HARD_CAP} keys (prevents crash).",
            )

    # ---------- check ----------
    def start_check(self, mode: str = "all"):
        """mode: all | invalid | pending | valid"""
        if mode == "all":
            self._ingest_paste_box()

        def testable(r) -> bool:
            return r.provider in PROVIDERS and bool(PROVIDERS[r.provider].get("validate"))

        records = [r for r in self.store.all() if testable(r)]
        pid = self.prov_filter.currentData()
        if pid:
            records = [r for r in records if r.provider == pid]

        if mode == "invalid":
            records = [r for r in records if r.status == "invalid"]
            empty_msg = "No invalid keys to recheck.\nRun Check keys first, or nothing failed."
        elif mode == "pending":
            records = [r for r in records if r.status in ("pending", "", "error", "rate_limited", "unknown")]
            empty_msg = "No pending keys left to check."
        elif mode == "valid":
            records = [r for r in records if r.status == "valid"]
            empty_msg = "No valid keys to recheck."
        else:
            # all: prefer unchecked first, but include everything in scope
            empty_msg = "No keys to check.\nPaste keys (or scan a folder), then click Check keys."

        items = [(r.key, r.provider) for r in records]
        if not items:
            QMessageBox.information(self, "Check", empty_msg)
            return
        if len(items) > CHECK_HARD_CAP:
            QMessageBox.warning(
                self, "Too many keys",
                f"{len(items)} keys is too many for one run (limit {CHECK_HARD_CAP}).\n\n"
                f"Filter by provider in the dropdown, or Clear and paste a smaller set.",
            )
            return
        mode_label = {
            "all": "all",
            "invalid": "invalids only",
            "pending": "pending only",
            "valid": "valid only",
        }.get(mode, mode)
        if len(items) > CHECK_WARN_AT:
            r = QMessageBox.question(
                self, "Check many keys?",
                f"About to check {len(items)} keys ({mode_label}).\n\n"
                f"This can take a while. Continue?\n"
                f"(UI will stay responsive; use Stop anytime.)",
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        self._launch_check(items, mode_label, scope=provider_name(pid) if pid else "All providers")

    def stop_check(self):
        if self.check_worker:
            self.check_worker.stop()
        if self.model_worker:
            self.model_worker.stop()
        if self.check_worker or self.model_worker:
            self._set_status("Stopping…")

    def on_check_result(self, key: str, payload: dict):
        rec = self.store.get(key)
        if not rec:
            return
        # Map the provider state onto the record. `rate_limited` / `unknown` are
        # deliberately NOT collapsed into "invalid" — v3 did that and permanently
        # marked throttled keys dead.
        state = payload.get("state") or ("valid" if payload.get("valid") else "invalid")
        if state in ("valid", "valid_no_funds"):
            rec.status = "valid"
        elif state == "rate_limited":
            rec.status = "rate_limited"
        elif state == "unknown":
            rec.status = "unknown"
        else:
            rec.status = "invalid"
        rec.remaining = payload.get("remaining")
        rec.balance_summary = payload.get("balance_summary") or ""
        # Don't keep huge raw payloads in memory
        details = payload.get("details") or {}
        if isinstance(details, dict) and "raw" in details:
            details = {k: v for k, v in details.items() if k != "raw"}
        if isinstance(details, dict):
            details["state"] = state
        rec.details = details
        rec.models = (payload.get("models") or [])[:5]
        rec.info = payload.get("info") or ""
        rec.error = payload.get("error") or ""
        rec.score = float(payload.get("score") or score_record(rec))
        if rec.status == "valid":
            self._check_ok += 1
            if self._check_ok <= 30 or self._check_ok % 25 == 0:
                log.info(
                    "OK %s (%s) state=%s remaining=%s score=%.1f | %s",
                    rec.mask(), rec.provider, state, rec.remaining, rec.score,
                    (rec.balance_summary or "")[:110],
                )
        elif rec.status in ("rate_limited", "unknown"):
            self._check_retry += 1
            if self._check_retry <= 10:
                log.info("RETRY %s (%s) state=%s err=%s", rec.mask(), rec.provider, state, rec.error)
        else:
            self._check_bad += 1
            if self._check_bad <= 10 or self._check_bad % 50 == 0:
                log.warning("BAD %s (%s) err=%s (bad_total=%d)", rec.mask(), rec.provider, rec.error, self._check_bad)
        self._request_ui_refresh()
        if self._selected_key == key:
            self.show_detail(key)

    def on_check_finished(self):
        self._busy = False
        self.check_btn.setEnabled(True)
        self.recheck_invalid_btn.setEnabled(True)
        self.recheck_google_btn.setEnabled(True)
        self.test_models_btn.setEnabled(True)
        self.stop_check_btn.setEnabled(False)
        best_n = sum(1 for r in self.store.all() if is_best_candidate(r, self.min_rem.value()))
        self._set_status(
            f"Check finished · ok={self._check_ok} bad={self._check_bad} "
            f"retry={self._check_retry} · {len(self.store)} keys · {best_n} best"
        )
        log.info(
            "Check finished store=%d ok=%d bad=%d retry=%d best=%d",
            len(self.store), self._check_ok, self._check_bad, self._check_retry, best_n,
        )
        self._autosave_history("check")
        self._ui_dirty = True
        self._flush_ui_refresh()
        # Refresh the working-keys file too: a re-check can change balances, and
        # keys tested in an earlier session are still in the store.
        if any(r.working_models for r in self.store.all()):
            self._write_working_file("check-finished")
        self._maybe_autostart_models()

    def _maybe_autostart_models(self):
        """Run the 'say hi' pass automatically if the setting is on.

        Guarded by _model_autostart so it cannot loop: the flag is only set here
        (once, from the check-finished path) and cleared before starting.
        """
        if not bool(self.settings.get("test_models", False)):
            return
        if self._model_autostart:
            return
        if not any(r.status == "valid" for r in self.store.all()):
            return
        self._model_autostart = True
        QTimer.singleShot(300, self._autostart_models_now)

    def _autostart_models_now(self):
        if not self._model_autostart or self._busy:
            self._model_autostart = False
            return
        self._model_autostart = False
        self.start_model_test()

    def on_check_phase(self, phase: str):
        if phase == "enrich":
            self._set_status("Sweep done — enriching valid keys…")

    # ---------- model testing ("say hi") ----------
    def start_model_test(self, only_key: Optional[str] = None):
        """Send a real 'hi' to each valid key's models and record what answers."""
        pid = self.prov_filter.currentData()
        items = []
        for r in self.store.all():
            if r.status != "valid":
                continue
            if only_key and r.key != only_key:
                continue
            if pid and r.provider != pid:
                continue
            chat = (PROVIDERS.get(r.provider) or {}).get("chat") or {}
            if not chat or chat.get("unsupported"):
                continue  # nothing to say hi to
            catalog = list(r.models or []) + list((r.details or {}).get("catalog_preview") or [])
            items.append((r.key, r.provider, catalog))
        if not items:
            QMessageBox.information(
                self, "Test models",
                "No valid keys with a chat-capable provider.\n\n"
                "Run Check keys first (valid keys only are tested).",
            )
            return
        self._launch_model_test(items, f"{len(items)} valid keys")

    def _launch_model_test(self, items, label):
        for b in (self.check_btn, self.recheck_invalid_btn, self.recheck_google_btn,
                  self.test_models_btn):
            b.setEnabled(False)
        self.stop_check_btn.setEnabled(True)
        self.check_prog.setRange(0, len(items))
        self.check_prog.setValue(0)
        self._model_tested = 0
        self._model_worked = 0
        self._busy = True
        self.model_worker = ModelTestWorker(
            items,
            concurrency=max(2, min(10, int(self.settings["concurrency"]) // 4)),
            timeout=float(max(self.settings["timeout"], 15)),
            proxy=self.settings.get("proxy", ""),
            models_per_key=int(self.settings.get("models_per_key", 3)),
        )
        self.model_worker.result.connect(self.on_model_result)
        self.model_worker.progress.connect(lambda d, t: self.check_prog.setValue(d))
        self.model_worker.status.connect(self._set_status)
        self.model_worker.finished_ok.connect(self.on_model_test_finished)
        log.info("Model test started n=%d label=%s", len(items), label)
        self._set_status(f"Testing models with {label}…")
        self.model_worker.start()

    def on_model_result(self, key: str, payload: dict):
        rec = self.store.get(key)
        if not rec:
            return
        details = rec.details or {}
        details["model_tests"] = payload.get("tested") or []
        details["model_test_note"] = payload.get("note") or ""
        rec.details = details
        working = payload.get("working") or []
        # Merge rather than clobber, so re-testing only ever adds knowledge.
        merged = list(rec.working_models or [])
        for m in working:
            if m not in merged:
                merged.append(m)
        rec.working_models = merged
        self._model_tested += 1
        if merged:
            self._model_worked += 1
            # The verified answer is the most useful line in the record — put it
            # first so it survives the 140-char table clip.
            summary = payload.get("summary") or ""
            if summary:
                rec.balance_summary = summary[:220]
            if rec.status == "valid" and not rec.error:
                log.info("MODEL OK %s (%s) %s", rec.mask(), rec.provider, summary[:120])
        elif working == [] and payload.get("tested"):
            log.info("MODEL NONE %s (%s) %s", rec.mask(), rec.provider,
                     (payload.get("summary") or "")[:100])
        rec.score = score_record(rec)
        # Keep the working-keys output file fresh while the run is in progress, so
        # a stop/crash still leaves usable results behind.
        self._working_writes += 1
        if self._working_writes % 20 == 0:
            self._write_working_file("modeltest-progress")
        self._request_ui_refresh()
        if self._selected_key == key:
            self.show_detail(key)

    def _write_working_file(self, reason: str = "", quiet: bool = True):
        """Write working_keys.txt (+ .json) for every key that answered.

        Format is documented in app/report.py and is meant to be consumed as well
        as read — pipe-delimited rows with a comment header.
        """
        out_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            txt, js, n = key_report.write_working_file(self.store.all(), out_dir)
        except Exception as e:
            log.warning("working-file write failed (%s): %s", reason, e)
            return None
        log.info("Wrote %s · %d working model(s) · reason=%s", txt, n, reason or "-")
        if not quiet:
            self._set_status(
                f"Wrote {os.path.basename(txt)} · {n} working model(s) → {txt}"
            )
        return txt

    def on_model_test_finished(self):
        self._busy = False
        for b in (self.check_btn, self.recheck_invalid_btn, self.recheck_google_btn,
                  self.test_models_btn):
            b.setEnabled(True)
        self.stop_check_btn.setEnabled(False)
        out = self._write_working_file("modeltest-finished")
        n_rows = key_report.format_working_txt(self.store.all())[1]
        self._set_status(
            f"Model test finished · {self._model_worked}/{self._model_tested} keys answered "
            f"a 'hi' prompt · {n_rows} working model row(s) written"
            + (f" → {os.path.basename(out)}" if out else "")
        )
        log.info("Model test finished tested=%d worked=%d rows=%d",
                 self._model_tested, self._model_worked, n_rows)
        self._autosave_history("modeltest")
        self._ui_dirty = True
        self._flush_ui_refresh()

    def _launch_check(self, items, label, scope=None):
        self.check_btn.setEnabled(False)
        self.recheck_invalid_btn.setEnabled(False)
        self.recheck_google_btn.setEnabled(False)
        self.test_models_btn.setEnabled(False)
        self.stop_check_btn.setEnabled(True)
        self.check_prog.setRange(0, len(items))
        self.check_prog.setValue(0)
        self._check_ok = 0
        self._check_bad = 0
        self._check_retry = 0
        self._busy = True
        self.check_worker = CheckWorker(
            items,
            concurrency=self.settings["concurrency"],
            timeout=float(self.settings["timeout"]),
            proxy=self.settings.get("proxy", ""),
            light=not self.settings.get("deep_check", False),
            enrich_valid=bool(self.settings.get("enrich_valid", True)),
        )
        self.check_worker.result.connect(self.on_check_result)
        self.check_worker.phase.connect(self.on_check_phase)
        self.check_worker.progress.connect(lambda d, t: self.check_prog.setValue(d))
        self.check_worker.status.connect(self._set_status)
        self.check_worker.finished_ok.connect(self.on_check_finished)
        self.check_worker.start()
        self._request_ui_refresh()
        by = {}
        for _, p in items:
            by[p] = by.get(p, 0) + 1
        mix = ", ".join(f"{provider_name(p)}={n}" for p, n in sorted(by.items(), key=lambda kv: -kv[1]))
        scope_txt = scope or "selected"
        self._set_status(f"Checking {len(items)} ({label}) [{scope_txt}] · {mix}")
        log.info(
            "Check started label=%s scope=%s n=%d mix=%s concurrency=%s timeout=%s",
            label, scope_txt, len(items), mix,
            self.settings["concurrency"], self.settings["timeout"],
        )

    def recheck_dead_google(self):
        """Re-test previously-failed Google keys through the Gemini probe.

        Reads all_results.csv (the last full export) for Google keys whose status is
        not 'valid', loads them into the store, and re-checks them. Recovers real
        Gemini keys that were wrongly flagged BAD (most scraped AIza… keys are
        YouTube/Maps/Firebase, not Gemini).
        """
        import csv as _csv
        import os as _os
        out_dir = _os.path.dirname(_os.path.abspath(__file__))
        out_dir = _os.path.dirname(out_dir)  # v4/
        csv_path = _os.path.join(out_dir, "all_results.csv")
        if not _os.path.exists(csv_path):
            QMessageBox.information(
                self, "Recheck dead Google",
                "No all_results.csv found.\n\nRun a Check first — it writes results to "
                "all_results.csv — then use this to recover Gemini keys.",
            )
            return
        dead, seen = [], set()
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = _csv.DictReader(f)
                for row in reader:
                    prov = (row.get("provider") or "").strip().lower()
                    status = (row.get("status") or "").strip().lower()
                    key = (row.get("key") or "").strip()
                    if prov == "google" and status != "valid" and key and key not in seen:
                        seen.add(key)
                        dead.append(key)
        except Exception as e:
            QMessageBox.warning(self, "Recheck dead Google", f"Could not read {csv_path}:\n{e}")
            return
        if not dead:
            QMessageBox.information(
                self, "Recheck dead Google",
                "No dead Google keys in all_results.csv.\n"
                "(They may already be marked valid, or none were Google.)",
            )
            return
        if len(dead) > CHECK_HARD_CAP:
            QMessageBox.warning(
                self, "Too many keys",
                f"{len(dead)} dead Google keys exceeds the per-run cap ({CHECK_HARD_CAP}).\n"
                f"Use a normal Check with the Google provider filter instead.",
            )
            return
        # Ensure each is in the store (so on_check_result can update it), marked pending.
        for k in dead:
            rec = self.store.get(k)
            if rec is None:
                rec = self.store.add(k, "google", source="recheck")
            rec.status = "pending"
        self._launch_check([(k, "google") for k in dead], f"dead Google recheck ({len(dead)})")

    # ---------- lists / detail ----------
    def refresh_lists(self):
        """Rebuild keys table with HARD CAP — never create 6k+ rows."""
        table = self.keys_table
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.setRowCount(0)

        records = self.store.all()
        pid = self.prov_filter.currentData()
        q = self.search.text().strip().lower()
        min_rem = self.min_rem.value()

        def match(r) -> bool:
            if pid and r.provider != pid:
                return False
            if q and q not in f"{r.key} {r.balance_summary} {r.info} {r.provider} {r.error}".lower():
                return False
            if self.valid_only.isChecked() and r.status != "valid":
                return False
            if min_rem > 0:
                if r.remaining is None or float(r.remaining) < min_rem:
                    return False
            return True

        filtered = [r for r in records if match(r)]
        # Valid (by score) first, then pending, then retryable, then invalid
        valid = sort_best([r for r in filtered if r.status == "valid"])
        pending = [r for r in filtered if r.status in ("pending", "", "error")]
        retryable = [r for r in filtered if r.status in ("rate_limited", "unknown")]
        invalid = [r for r in filtered if r.status == "invalid"]
        shown = (valid + pending + retryable + invalid)[:ALL_LIST_CAP]

        for r in shown:
            row = table.rowCount()
            table.insertRow(row)
            # Clean display text (never show sort sentinels like -1e18)
            cur = ""
            if r.remaining is not None:
                cur = (r.details or {}).get("currency") or ""
                if cur and cur not in ("USD", "$"):
                    rem_txt = f"{float(r.remaining):.2f} {cur}"
                else:
                    rem_txt = f"${float(r.remaining):.2f}"
            else:
                rem_txt = "—"
            rem_sort = float(r.remaining) if r.remaining is not None else -1e18
            score_txt = f"{float(r.score):.1f}" if r.score else "—"
            score_sort = float(r.score) if r.score else -1e18
            ctx = (r.context_lines[0] if r.context_lines else (r.info or r.error or ""))[:160]
            tests = (r.details or {}).get("model_tests") or []
            if r.working_models:
                works_txt = "✓ " + ", ".join(m.split("/")[-1] for m in r.working_models[:2])
                works_sort = len(r.working_models)
            elif tests:
                works_txt = f"✗ tested ({len(tests)})"
                works_sort = -1
            elif (r.details or {}).get("model_test_note"):
                works_txt = "— n/a"
                works_sort = -2
            else:
                works_txt = "—"
                works_sort = -3
            cells = [
                (r.status or "pending", None),
                (provider_name(r.provider), None),
                (rem_txt, rem_sort),
                (score_txt, score_sort),
                (works_txt, works_sort),
                ((r.balance_summary or r.error or "")[:140], None),
                (r.mask(8), None),
                (ctx, None),
            ]
            for col, (text, sort_val) in enumerate(cells):
                item = SortItem(text)
                item.setData(Qt.ItemDataRole.UserRole, r.key)
                if sort_val is not None:
                    item.setData(SORT_ROLE, sort_val)
                if r.status == "valid":
                    if r.remaining is not None and r.remaining > 0:
                        item.setForeground(QColor("#81c995"))
                    else:
                        item.setForeground(QColor("#8ab4f8"))
                elif r.status == "invalid":
                    item.setForeground(QColor("#f28b82"))
                elif r.status in ("rate_limited", "unknown"):
                    item.setForeground(QColor("#fdd663"))
                table.setItem(row, col, item)

        table.setSortingEnabled(True)
        # Default: most remaining credit on top (numeric via SortItem)
        table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        table.blockSignals(False)
        if len(filtered) > ALL_LIST_CAP:
            self._set_status(
                f"Showing {ALL_LIST_CAP}/{len(filtered)} rows — filter provider or Export for full set · {self._provider_breakdown()}"
            )

    def _header_menu(self, pos):
        """Right-click column header → show/hide fields."""
        menu = QMenu(self)
        hdr = self.keys_table.horizontalHeader()
        for i, name in enumerate(TABLE_COLS):
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(not self.keys_table.isColumnHidden(i))
            act.toggled.connect(lambda checked, col=i: self.keys_table.setColumnHidden(col, not checked))
        menu.addSeparator()
        reset = menu.addAction("Reset column widths")
        reset.triggered.connect(self._reset_column_widths)
        test_sel = menu.addAction("Test models for these keys (say hi)")
        test_sel.triggered.connect(self.start_model_test)
        write_working = menu.addAction("Write working_keys.txt now")
        write_working.triggered.connect(lambda: self._write_working_file("manual", quiet=False))
        by_rem = menu.addAction("Sort by Remaining (high → low)")
        by_rem.triggered.connect(
            lambda: self.keys_table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        )
        by_score = menu.addAction("Sort by Score (high → low)")
        by_score.triggered.connect(
            lambda: self.keys_table.sortByColumn(3, Qt.SortOrder.DescendingOrder)
        )
        by_works = menu.addAction("Sort by Working models (high → low)")
        by_works.triggered.connect(
            lambda: self.keys_table.sortByColumn(4, Qt.SortOrder.DescendingOrder)
        )
        menu.exec(hdr.mapToGlobal(pos))

    def _reset_column_widths(self):
        for i, w in enumerate([72, 120, 100, 64, 150, 300, 150, 280]):
            self.keys_table.setColumnWidth(i, w)
            self.keys_table.setColumnHidden(i, False)

    def on_table_selected(self):
        items = self.keys_table.selectedItems()
        if not items:
            return
        key = items[0].data(Qt.ItemDataRole.UserRole)
        if key:
            self.show_detail(key)

    def show_detail(self, key: str):
        self._selected_key = key
        r = self.store.get(key)
        if not r:
            self.detail.setPlainText("")
            return
        lines = [
            f"Provider : {provider_name(r.provider)} ({r.provider})",
            f"Status   : {r.status}",
            f"Score    : {r.score:.1f}",
            f"Remaining: {f'${r.remaining:.2f}' if r.remaining is not None else 'n/a'}",
            f"Summary  : {r.balance_summary or '—'}",
            f"Info     : {r.info or '—'}",
            f"Error    : {r.error or '—'}",
            f"Sources  : {'; '.join(sorted(r.sources))}",
            f"Key      : {r.key}",
            "",
            "Found in line(s):",
        ]
        if r.context_lines:
            for i, ln in enumerate(r.context_lines, 1):
                lines.append(f"  [{i}] {ln}")
        else:
            lines.append("  (no line context captured)")
        lines += [
            "",
            "Models:",
            (", ".join(r.models[:30]) + (f" … +{len(r.models)-30} more" if len(r.models) > 30 else "")) if r.models else "(none)",
        ]
        d = r.details or {}
        tests = d.get("model_tests") or []
        lines += ["", "Model test (sent 'hi'):"]
        if r.working_models:
            lines.append(f"  WORKS: {', '.join(r.working_models)}")
        if tests:
            for t in tests:
                if not isinstance(t, dict):
                    continue
                if t.get("ok"):
                    lines.append(f"  [ok] {t.get('model')} → {t.get('reply')!r} "
                                 f"({t.get('latency_ms', '?')}ms)")
                else:
                    lines.append(f"  [--] {t.get('model')} → {t.get('error') or t.get('status')}")
        elif d.get("model_test_note"):
            lines.append(f"  ({d['model_test_note']})")
        else:
            lines.append("  (not tested — use 'Test models (say hi)' or enable it in Settings)")
        lines += [
            "",
            "Details JSON:",
            json.dumps(r.details, indent=2, default=str)[:4000],
        ]
        self.detail.setPlainText("\n".join(lines))

    def copy_selected_key(self):
        if not self._selected_key:
            return
        QApplication.clipboard().setText(self._selected_key)
        self._set_status("Key copied")

    def copy_selected_summary(self):
        if not self._selected_key:
            return
        r = self.store.get(self._selected_key)
        if not r:
            return
        QApplication.clipboard().setText(
            f"{r.key}\n{provider_name(r.provider)} | {r.balance_summary} | remaining={r.remaining}"
        )
        self._set_status("Summary copied")

    # ---------- export ----------
    def _autosave_history(self, reason: str = ""):
        try:
            data = key_history.load_history()
            added = key_history.merge_records(data, self.store.all())
            path = key_history.save_history(data)
            log.info(
                "History autosave (%s): +%d new · total=%d → %s",
                reason, added, len(data.get("keys") or {}), path,
            )
        except Exception as e:
            log.warning("History autosave failed: %s", e)

    def save_history(self):
        if len(self.store) == 0:
            QMessageBox.information(self, "History", "No keys in the table to save.")
            return
        data = key_history.load_history()
        added = key_history.merge_records(data, self.store.all())
        path = key_history.save_history(data)
        self._set_status(f"History saved (+{added} new) → {path}")
        QMessageBox.information(
            self, "History",
            f"Saved to history.json\n+{added} new keys\nTotal stored: {len(data.get('keys') or {})}\n\n{path}",
        )

    def load_history(self):
        path = key_history.history_path()
        data = key_history.load_history(path)
        keys_map = data.get("keys") or {}
        if not keys_map:
            QMessageBox.information(self, "History", f"No keys in history yet.\n{path}")
            return
        added = 0
        for key, row in keys_map.items():
            before = len(self.store)
            prov = row.get("provider") or "generic"
            if prov not in PROVIDERS:
                from .providers import detect_provider_for_key
                prov = detect_provider_for_key(key) or "openrouter"
            rec = self.store.add(key, prov, source="history.json")
            if len(self.store) > before:
                added += 1
            # restore last known check fields
            if row.get("status"):
                rec.status = row["status"]
            if row.get("remaining") is not None:
                rec.remaining = row["remaining"]
            if row.get("balance_summary"):
                rec.balance_summary = row["balance_summary"]
            if row.get("score"):
                rec.score = float(row["score"])
            if row.get("info"):
                rec.info = row["info"]
            if row.get("models"):
                rec.models = list(row["models"])
            if row.get("working_models"):
                rec.working_models = list(row["working_models"])
            if row.get("details"):
                rec.details = dict(row["details"])
            for s in row.get("sources") or []:
                rec.sources.add(s)
        self._rebuild_provider_filter()
        self.refresh_lists()
        self._set_status(f"Loaded history · +{added} new · store={len(self.store)} · file has {len(keys_map)}")
        log.info("Loaded history from %s (+%d new, file=%d)", path, added, len(keys_map))

    def export_best(self):
        records = self.store.all()
        if not any(r.status == "valid" for r in records):
            QMessageBox.information(self, "Export", "No checked/valid keys yet. Run Check keys first.")
            return
        out_dir = os.path.dirname(os.path.abspath(__file__))
        out_dir = os.path.dirname(out_dir)  # v4/
        txt_path = os.path.join(out_dir, "best_keys.txt")
        json_path = os.path.join(out_dir, "best_keys.json")
        csv_path = os.path.join(out_dir, "all_results.csv")
        report = format_best_report(records, min_remaining=self.min_rem.value())
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        best = [r for r in sort_best(records) if is_best_candidate(r, self.min_rem.value())]
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in best], f, indent=2)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["key", "provider", "status", "remaining", "score",
                        "summary", "info", "models_count", "working_models",
                        "error", "sources", "details"])
            for r in records:
                w.writerow([
                    r.key, r.provider, r.status, r.remaining, r.score,
                    r.balance_summary, r.info,
                    len(r.models),
                    "; ".join(r.working_models),
                    r.error,
                    "; ".join(sorted(r.sources)),
                    json.dumps(r.details, ensure_ascii=False),
                ])
        self._set_status(f"Exported → {txt_path}")
        working_txt = self._write_working_file("export")
        log.info("Exported best=%d → %s | %s | %s", len(best), txt_path, json_path, csv_path)
        extra = f"\n• {working_txt}" if working_txt else ""
        QMessageBox.information(
            self, "Exported",
            f"Wrote:\n• {txt_path}\n• {json_path}\n• {csv_path}{extra}"
        )
