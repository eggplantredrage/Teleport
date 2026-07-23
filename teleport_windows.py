#!/usr/bin/env python3
"""
Teleport (Windows edition)
--------------------------
Companion to the Linux "Teleport" app. Lets you reboot straight from
Windows into another dual-booted OS (Linux/GRUB, another Windows install,
etc.) by setting a ONE-TIME UEFI firmware boot target via `bcdedit`, then
restarting.

How it works:
  - `bcdedit /enum firmware` lists every UEFI NVRAM boot entry Windows
    knows about (Windows Boot Manager, plus anything else like "ubuntu"
    or "grub" added by your Linux installer).
  - `bcdedit /set {fwbootmgr} bootsequence {GUID}` tells the firmware to
    boot that ONE entry next time only — after that one boot it reverts
    to your normal default automatically.
  - `shutdown /r /t 0` triggers the actual restart.

This needs Administrator rights. The app checks for elevation on launch
and, if needed, relaunches itself via UAC (no manual "Run as admin"
required).

Requires: PyQt6  ->  pip install PyQt6
Only runs on Windows with a UEFI firmware (not legacy BIOS/CSM, and not
Linux/macOS — use teleport.py for the Linux side).
"""

import os
import re
import sys
import ctypes
import subprocess

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QGroupBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont


# --------------------------------------------------------------------------
# Admin elevation
# --------------------------------------------------------------------------

def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin():
    """Re-run this same script/exe elevated via the UAC prompt."""
    params = " ".join(f'"{a}"' for a in sys.argv)
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )


# --------------------------------------------------------------------------
# bcdedit parsing
# --------------------------------------------------------------------------

class BootEntry:
    def __init__(self, identifier, description, device=None, is_windows=False):
        self.identifier = identifier   # {guid}
        self.description = description
        self.device = device            # e.g. "partition=\Device\HarddiskVolume2"
        self.is_windows = is_windows

    def __str__(self):
        tag = "  [Windows]" if self.is_windows else ""
        dev = f"\n    ↳ {self.device}" if self.device else ""
        return f"{self.description}{tag}{dev}"


def parse_bcdedit_firmware(output: str):
    """Parse `bcdedit /enum firmware` into a list of BootEntry, skipping
    the synthetic 'Firmware Boot Manager' / 'Windows Boot Manager' blocks
    and keeping only real GUID-identified firmware applications (this is
    where your GRUB/Linux entry, or a second Windows install, will show up)."""
    lines = output.replace("\r\n", "\n").split("\n")
    blocks = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped and set(stripped) == {"-"}:
            header = lines[i - 1].strip() if i > 0 else ""
            blocks.append({"header": header, "kv": {}})
            i += 1
            continue
        if stripped and blocks:
            m = re.match(r"^(\S+)\s+(.*)$", stripped)
            if m:
                key, val = m.groups()
                blocks[-1]["kv"].setdefault(key, val.strip())
        i += 1

    guid_re = re.compile(r"^\{[0-9A-Fa-f-]{36}\}$")
    entries = []
    for b in blocks:
        if b["header"] in ("Firmware Boot Manager", "Windows Boot Manager"):
            continue
        ident = b["kv"].get("identifier", "")
        if not guid_re.match(ident):
            continue
        desc = b["kv"].get("description", b["header"])
        device = b["kv"].get("device")
        is_win = "windows" in desc.lower()
        entries.append(BootEntry(ident, desc, device, is_win))
    return entries


# --------------------------------------------------------------------------
# Background workers
# --------------------------------------------------------------------------

class DetectWorker(QThread):
    finished_ok = pyqtSignal(list)
    finished_err = pyqtSignal(str)

    def run(self):
        try:
            result = subprocess.run(
                ["bcdedit", "/enum", "firmware"],
                capture_output=True, text=True, timeout=20
            )
            if result.returncode != 0:
                self.finished_err.emit(
                    result.stderr.strip() or "bcdedit /enum firmware failed."
                )
                return
            entries = parse_bcdedit_firmware(result.stdout)
            if not entries:
                self.finished_err.emit(
                    "No alternate firmware boot entries found — only "
                    "Windows Boot Manager is present. If you dual-boot "
                    "Linux, make sure its bootloader added itself to the "
                    "UEFI firmware (check with 'bcdedit /enum firmware' "
                    "in an admin cmd prompt)."
                )
                return
            self.finished_ok.emit(entries)
        except FileNotFoundError:
            self.finished_err.emit(
                "bcdedit not found. This tool only works on Windows with "
                "UEFI firmware (not legacy BIOS/CSM mode)."
            )
        except Exception as e:
            self.finished_err.emit(str(e))


class RebootWorker(QThread):
    finished_ok = pyqtSignal()
    finished_err = pyqtSignal(str)

    def __init__(self, entry: BootEntry):
        super().__init__()
        self.entry = entry

    def run(self):
        try:
            r1 = subprocess.run(
                ["bcdedit", "/set", "{fwbootmgr}", "bootsequence", self.entry.identifier],
                capture_output=True, text=True, timeout=15
            )
            if r1.returncode != 0:
                self.finished_err.emit(
                    r1.stderr.strip() or r1.stdout.strip() or "bcdedit bootsequence failed"
                )
                return

            r2 = subprocess.run(
                ["shutdown", "/r", "/t", "0"],
                capture_output=True, text=True, timeout=15
            )
            if r2.returncode != 0:
                self.finished_err.emit(r2.stderr.strip() or "shutdown /r failed")
                return

            self.finished_ok.emit()
        except Exception as e:
            self.finished_err.emit(str(e))


# --------------------------------------------------------------------------
# GUI (same dark theme as the Linux edition)
# --------------------------------------------------------------------------

DARK_STYLE = """
QWidget { background-color: #1e1f26; color: #e6e6e6; font-size: 13px; }
QGroupBox { border: 1px solid #3a3c4a; border-radius: 6px; margin-top: 10px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #8fd3ff; }
QListWidget { background-color: #262832; border: 1px solid #3a3c4a; border-radius: 4px; }
QListWidget::item:selected { background-color: #3a5a8c; }
QPushButton { background-color: #2d3040; border: 1px solid #454866; border-radius: 5px; padding: 8px 14px; }
QPushButton:hover { background-color: #3a3d52; }
QPushButton:disabled { color: #6a6d80; }
QPushButton#dangerBtn { background-color: #5a2d2d; border: 1px solid #7a3d3d; }
QPushButton#dangerBtn:hover { background-color: #6e3838; }
QPlainTextEdit { background-color: #14151a; border: 1px solid #3a3c4a; color: #9fef9f; font-family: monospace; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Teleport")
        self.resize(520, 480)
        self.entries = []

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        title = QLabel("Teleport")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        self.status_label = QLabel("Scanning UEFI firmware boot entries…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        group = QGroupBox("Boot entries")
        group_layout = QVBoxLayout(group)
        self.list_widget = QListWidget()
        group_layout.addWidget(self.list_widget)
        layout.addWidget(group)

        btn_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.detect_entries)
        btn_row.addWidget(self.refresh_btn)

        btn_row.addStretch()

        self.reboot_btn = QPushButton("Teleport to Selected Entry")
        self.reboot_btn.setObjectName("dangerBtn")
        self.reboot_btn.setEnabled(False)
        self.reboot_btn.clicked.connect(self.on_reboot_clicked)
        btn_row.addWidget(self.reboot_btn)
        layout.addLayout(btn_row)

        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        log_layout.addWidget(self.log)
        layout.addWidget(log_group)

        self.list_widget.itemSelectionChanged.connect(
            lambda: self.reboot_btn.setEnabled(bool(self.list_widget.selectedItems()))
        )

        self.detect_worker = None
        self.reboot_worker = None
        self.detect_entries()

    def append_log(self, msg: str):
        self.log.appendPlainText(msg)

    def detect_entries(self):
        self.list_widget.clear()
        self.reboot_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("Scanning UEFI firmware boot entries…")
        self.append_log("Running bcdedit /enum firmware…")

        self.detect_worker = DetectWorker()
        self.detect_worker.finished_ok.connect(self.on_detect_ok)
        self.detect_worker.finished_err.connect(self.on_detect_err)
        self.detect_worker.start()

    def on_detect_ok(self, entries):
        self.entries = entries
        self.refresh_btn.setEnabled(True)
        self.status_label.setText(
            f"Found {len(entries)} entr{'y' if len(entries)==1 else 'ies'}. "
            "Select one and click Teleport."
        )
        for entry in entries:
            item = QListWidgetItem(str(entry))
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.list_widget.addItem(item)
        self.append_log(f"Found {len(entries)} entries.")

    def on_detect_err(self, msg):
        self.refresh_btn.setEnabled(True)
        self.status_label.setText("Detection failed. See log below.")
        self.append_log(f"ERROR: {msg}")
        QMessageBox.critical(self, "Detection failed", msg)

    def on_reboot_clicked(self):
        items = self.list_widget.selectedItems()
        if not items:
            return
        entry = items[0].data(Qt.ItemDataRole.UserRole)

        dev_line = f"\n  {entry.device}\n" if entry.device else "\n"
        confirm = QMessageBox.question(
            self,
            "Confirm reboot",
            f"This will set:\n\n  {entry.description}{dev_line}\n"
            "as the ONE-TIME next boot target and restart your machine "
            "immediately.\n\nSave your work first. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.reboot_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.append_log(f"Setting next boot to '{entry.description}' and restarting…")

        self.reboot_worker = RebootWorker(entry)
        self.reboot_worker.finished_ok.connect(self.on_reboot_ok)
        self.reboot_worker.finished_err.connect(self.on_reboot_err)
        self.reboot_worker.start()

    def on_reboot_ok(self):
        self.append_log("Reboot command issued. System should restart now.")

    def on_reboot_err(self, msg):
        self.reboot_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.append_log(f"ERROR: {msg}")
        QMessageBox.critical(self, "Reboot failed", msg)


def main():
    if os.name != "nt":
        print("Teleport (Windows edition) only runs on Windows.")
        sys.exit(1)

    if not is_admin():
        # Relaunch elevated via UAC, then exit this non-elevated instance.
        relaunch_as_admin()
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
