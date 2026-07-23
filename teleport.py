#!/usr/bin/env python3
"""
Teleport
--------
A small PyQt6 GUI that lets you reboot straight into a dual-booted (or
separate-drive) Windows install, without having to catch the GRUB menu
or mash F-keys for the boot device selector.

How it works:
  - UEFI systems: uses `efibootmgr` to list NVRAM boot entries, finds the
    "Windows Boot Manager" entry (or lets you pick one), sets it as the
    ONE-TIME next boot target with `--bootnext`, then reboots. Firmware
    reverts to the normal boot order automatically after that one boot.
  - Legacy BIOS / GRUB systems: parses grub.cfg for menuentry titles,
    and uses `grub-reboot <title>` to set a one-time GRUB default,
    then reboots.

Privileged operations (reading NVRAM, setting boot-next, rebooting) are
run through `pkexec` so the GUI itself doesn't need to run as root.

Requires: PyQt6, efibootmgr (UEFI) or grub2-common/grub-common (BIOS).
Install PyQt6 with:  pip install PyQt6 --break-system-packages
"""

import os
import re
import sys
import subprocess

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QGroupBox, QSizePolicy
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont


# --------------------------------------------------------------------------
# Boot backend detection / parsing
# --------------------------------------------------------------------------

def is_uefi() -> bool:
    return os.path.exists("/sys/firmware/efi")


def find_grub_cfg():
    for path in ("/boot/grub/grub.cfg", "/boot/grub2/grub.cfg",
                 "/boot/efi/EFI/grub.cfg"):
        if os.path.exists(path):
            return path
    return None


class BootEntry:
    """Represents one selectable boot target, UEFI or GRUB."""
    def __init__(self, backend, ident, label, is_windows=False, drive_desc=None):
        self.backend = backend      # "uefi" or "grub"
        self.ident = ident          # UEFI: "0001" style id. GRUB: menu title.
        self.label = label          # Human readable label
        self.is_windows = is_windows
        self.drive_desc = drive_desc  # e.g. "/dev/sda2 · Samsung SSD 970 EVO · 465.8G"

    def __str__(self):
        tag = "  [likely Windows]" if self.is_windows else ""
        drive = f"\n    ↳ {self.drive_desc}" if self.drive_desc else ""
        return f"{self.label}{tag}{drive}"


def get_lsblk_map():
    """Build partuuid/uuid -> drive-info lookup tables via lsblk. This reads
    udev's cached block device database, so it does NOT require root."""
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,PARTUUID,UUID,LABEL,MODEL,SIZE,PKNAME,TYPE"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return {}
        import json
        data = json.loads(result.stdout)
    except Exception:
        return {}

    flat = []

    def walk(devices):
        for d in devices:
            flat.append(d)
            if d.get("children"):
                walk(d["children"])

    walk(data.get("blockdevices", []))

    disks = {d["name"]: d for d in flat if d.get("type") == "disk"}
    by_partuuid, by_uuid = {}, {}
    for d in flat:
        if d.get("type") != "part":
            continue
        parent = disks.get(d.get("pkname") or "")
        info = {
            "part_name": d.get("name"),
            "label": d.get("label"),
            "part_size": d.get("size"),
            "disk_name": d.get("pkname"),
            "disk_model": parent.get("model") if parent else None,
            "disk_size": parent.get("size") if parent else None,
        }
        if d.get("partuuid"):
            by_partuuid[d["partuuid"].lower()] = info
        if d.get("uuid"):
            by_uuid[d["uuid"].lower()] = info
    return {"by_partuuid": by_partuuid, "by_uuid": by_uuid}


def describe_drive(info):
    """Turn an lsblk info dict into a readable one-liner."""
    if not info:
        return None
    bits = []
    if info.get("part_name"):
        bits.append(f"/dev/{info['part_name']}")
    if info.get("disk_model"):
        bits.append(info["disk_model"])
    elif info.get("disk_name"):
        bits.append(f"/dev/{info['disk_name']}")
    if info.get("disk_size"):
        bits.append(info["disk_size"])
    if info.get("label"):
        bits.append(f"label: {info['label']}")
    return " · ".join(bits) if bits else None


def parse_efibootmgr(output: str, lsblk_map=None):
    """Parse `efibootmgr -v` output into a list of BootEntry."""
    entries = []
    line_re = re.compile(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+(.+)$")
    guid_re = re.compile(r"HD\(\d+,GPT,([0-9A-Fa-f-]{36})")
    for raw_line in output.splitlines():
        parts = raw_line.split("\t")
        first_part = parts[0]
        m = line_re.match(first_part)
        if not m:
            continue
        ident, _active, label = m.groups()
        label = label.strip()
        is_win = "windows" in label.lower()

        drive_desc = None
        if lsblk_map:
            gm = guid_re.search("\t".join(parts[1:]))
            if gm:
                info = lsblk_map.get("by_partuuid", {}).get(gm.group(1).lower())
                drive_desc = describe_drive(info)

        entries.append(BootEntry("uefi", ident, label, is_win, drive_desc))
    return entries


def parse_grub_titles(cfg_path: str, lsblk_map=None):
    """Pull top-level menuentry titles out of grub.cfg, and try to resolve
    the underlying drive each one boots from."""
    entries = []
    try:
        with open(cfg_path, "r", errors="ignore") as f:
            text = f.read()
    except PermissionError:
        return entries

    title_re = re.compile(r"^\s*menuentry\s+['\"]([^'\"]+)['\"]", re.MULTILINE)
    uuid_re = re.compile(r"--fs-uuid[^\n]*--set=root\s+([0-9A-Za-z-]+)")
    hd_re = re.compile(r"set root='?(hd\d+,\S+?)'?\s")

    matches = list(title_re.finditer(text))
    for i, m in enumerate(matches):
        title = m.group(1)
        is_win = "windows" in title.lower()

        # Body = text from this menuentry up to the next one, so we only
        # look at device lines belonging to THIS entry.
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]

        drive_desc = None
        if lsblk_map:
            um = uuid_re.search(body)
            if um:
                info = lsblk_map.get("by_uuid", {}).get(um.group(1).lower())
                drive_desc = describe_drive(info)
            if not drive_desc:
                hm = hd_re.search(body)
                if hm:
                    drive_desc = f"GRUB device: {hm.group(1)}"

        entries.append(BootEntry("grub", title, title, is_win, drive_desc))
    return entries


# --------------------------------------------------------------------------
# Background workers (so the GUI doesn't freeze on pkexec prompts)
# --------------------------------------------------------------------------

class DetectWorker(QThread):
    finished_ok = pyqtSignal(list, str)   # entries, backend name
    finished_err = pyqtSignal(str)

    def run(self):
        try:
            lsblk_map = get_lsblk_map()
            if is_uefi():
                result = subprocess.run(
                    ["pkexec", "efibootmgr", "-v"],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode != 0:
                    self.finished_err.emit(
                        f"efibootmgr failed:\n{result.stderr.strip()}"
                    )
                    return
                entries = parse_efibootmgr(result.stdout, lsblk_map)
                if not entries:
                    self.finished_err.emit("No UEFI boot entries found.")
                    return
                self.finished_ok.emit(entries, "uefi")
            else:
                cfg = find_grub_cfg()
                if not cfg:
                    self.finished_err.emit(
                        "Could not find grub.cfg (checked /boot/grub, "
                        "/boot/grub2, /boot/efi/EFI)."
                    )
                    return
                entries = parse_grub_titles(cfg, lsblk_map)
                if not entries:
                    self.finished_err.emit(f"No menu entries parsed from {cfg}")
                    return
                self.finished_ok.emit(entries, "grub")
        except FileNotFoundError as e:
            self.finished_err.emit(f"Required tool missing: {e}")
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
            if self.entry.backend == "uefi":
                # One pkexec call, one password prompt: set bootnext, then reboot.
                cmd = (
                    f"efibootmgr --bootnext {self.entry.ident} && "
                    f"/sbin/reboot"
                )
            else:
                # grub-reboot accepts the exact menu title.
                safe_title = self.entry.ident.replace('"', '\\"')
                cmd = (
                    f'grub-reboot "{safe_title}" && '
                    f"/sbin/reboot"
                )

            result = subprocess.run(
                ["pkexec", "sh", "-c", cmd],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                self.finished_err.emit(result.stderr.strip() or "Unknown error")
                return
            self.finished_ok.emit()
        except Exception as e:
            self.finished_err.emit(str(e))


# --------------------------------------------------------------------------
# GUI
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
        self.backend = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        title = QLabel("Teleport")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        self.status_label = QLabel("Detecting boot configuration…")
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

        self.reboot_btn = QPushButton("Reboot to Selected Entry")
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
        mode = "UEFI" if is_uefi() else "Legacy BIOS / GRUB"
        self.status_label.setText(f"Detected boot mode: {mode}. Scanning entries "
                                   f"(you may be asked for your password)…")
        self.append_log(f"Scanning boot entries via {mode} backend…")

        self.detect_worker = DetectWorker()
        self.detect_worker.finished_ok.connect(self.on_detect_ok)
        self.detect_worker.finished_err.connect(self.on_detect_err)
        self.detect_worker.start()

    def on_detect_ok(self, entries, backend):
        self.entries = entries
        self.backend = backend
        self.refresh_btn.setEnabled(True)
        self.status_label.setText(
            f"Found {len(entries)} entr{'y' if len(entries)==1 else 'ies'} "
            f"via {backend.upper()}. Select one and click Reboot."
        )
        for entry in entries:
            item = QListWidgetItem(str(entry))
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.list_widget.addItem(item)
            if entry.is_windows:
                item.setSelected(True)
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

        drive_line = f"\n  {entry.drive_desc}\n" if entry.drive_desc else "\n"
        confirm = QMessageBox.question(
            self,
            "Confirm reboot",
            f"This will set:\n\n  {entry.label}{drive_line}\n"
            "as the ONE-TIME next boot target and reboot your machine "
            "immediately.\n\nSave your work first. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.reboot_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.append_log(f"Setting next boot to '{entry.label}' and rebooting…")

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
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
