#!/usr/bin/env python3
"""
Linux Undervolt Tool for Ryzen CPUs using the ryzen_smu kernel driver.
Allows reading and setting voltage offsets per core.

WARNING: This tool writes to the SMU (System Management Unit) of your Ryzen CPU.
Incorrect offsets may cause system instability or damage. Use at your own risk.
"""

import sys
import os
import struct
import subprocess
import time
import argparse
import json
import shutil
import re
import logging
from pathlib import Path
from typing import List, Optional

os.environ["QT_LOGGING_RULES"] = "qt.qpa.theme=false"

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton,
    QTextEdit, QVBoxLayout, QHBoxLayout,
    QWidget, QLabel, QSpinBox, QMessageBox,
    QComboBox, QInputDialog, QListWidget,
    QListWidgetItem, QAbstractItemView, QSplitter
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon

# Setup logging
logger = logging.getLogger("ruv")
if os.environ.get("RUV_DEBUG"):
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
else:
    logging.basicConfig(level=logging.WARNING)


class RyzenSMU:
    """Low-level interface to the ryzen_smu kernel driver."""
    FS_PATH = Path("/sys/kernel/ryzen_smu_drv/")
    VER_PATH = FS_PATH / "version"
    SMU_ARGS = FS_PATH / "smu_args"
    MP1_CMD = FS_PATH / "mp1_smu_cmd"

    CMD_GET_OFFSET = 0x48
    CMD_SET_OFFSET = 0x35
    CMD_RESET_ALL = 0x36

    SMU_TIMEOUT = 5.0

    MIN_OFFSET = -100
    MAX_OFFSET = 100

    def __init__(self):
        if not self.driver_loaded():
            raise RuntimeError("Ryzen SMU driver not loaded. Load it with: sudo modprobe ryzen_smu")

    @classmethod
    def driver_loaded(cls):
        return cls.VER_PATH.is_file()

    @staticmethod
    def _read_file(file: Path, size: int):
        with open(file, "rb") as fp:
            return fp.read(size)

    @staticmethod
    def _write_file(file: Path, data: bytes):
        with open(file, "wb") as fp:
            return fp.write(data)

    @classmethod
    def _read_file32(cls, file: Path):
        data = cls._read_file(file, 4)
        if len(data) != 4:
            return None
        return struct.unpack("<I", data)[0]

    @classmethod
    def _write_file32(cls, file: Path, value: int):
        data = struct.pack("<I", value)
        return cls._write_file(file, data) == 4

    @classmethod
    def _read_file192(cls, file: Path):
        data = cls._read_file(file, 24)
        if len(data) != 24:
            return None
        return struct.unpack("<IIIIII", data)

    @classmethod
    def _write_file192(cls, file: Path, *values: int):
        if len(values) != 6:
            raise ValueError("Need exactly 6 values")
        data = struct.pack("<IIIIII", *values)
        return cls._write_file(file, data) == 24

    def smu_command(self, op: int, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0):
        start = time.monotonic()
        while True:
            status = self._read_file32(self.MP1_CMD)
            if status is None:
                raise RuntimeError("Failed to read SMU status")
            if status == 1:
                break
            if status != 0:
                raise RuntimeError(f"Unexpected SMU status: {status}")
            if time.monotonic() - start > self.SMU_TIMEOUT:
                raise RuntimeError("Timeout waiting for SMU ready")
            time.sleep(0.05)

        if not self._write_file192(self.SMU_ARGS, arg1, arg2, arg3, arg4, arg5, arg6):
            raise RuntimeError("Failed to write SMU arguments")
        if not self._write_file32(self.MP1_CMD, op):
            raise RuntimeError("Failed to write SMU command")

        start = time.monotonic()
        while True:
            status = self._read_file32(self.MP1_CMD)
            if status == 1:
                break
            if status is None:
                raise RuntimeError("Failed to read SMU status")
            if status != 0:
                raise RuntimeError(f"SMU command failed with status {status}")
            if time.monotonic() - start > self.SMU_TIMEOUT:
                raise RuntimeError("Timeout waiting for SMU command")
            time.sleep(0.05)

        response = self._read_file192(self.SMU_ARGS)
        if response is None:
            raise RuntimeError("Failed to read SMU response")
        return response

    def get_core_offset(self, core_id: int):
        arg = ((core_id & 8) << 5 | (core_id & 7)) << 20
        try:
            result = self.smu_command(self.CMD_GET_OFFSET, arg)
        except RuntimeError:
            return None
        value = result[0]
        if value > 2**31 - 1:
            value -= 2**32
        return value

    def set_core_offset(self, core_id: int, offset: int):
        if not (self.MIN_OFFSET <= offset <= self.MAX_OFFSET):
            raise ValueError(f"Offset {offset} mV is outside allowed range [{self.MIN_OFFSET}, {self.MAX_OFFSET}]")
        old_offset = self.get_core_offset(core_id)
        arg = (((core_id & 8) << 5 | (core_id & 7)) << 20) | (offset & 0xFFFF)
        try:
            self.smu_command(self.CMD_SET_OFFSET, arg)
        except Exception:
            if old_offset is not None:
                try:
                    rollback_arg = (((core_id & 8) << 5 | (core_id & 7)) << 20) | (old_offset & 0xFFFF)
                    self.smu_command(self.CMD_SET_OFFSET, rollback_arg)
                except Exception as e:
                    logger.error(f"Rollback failed for core {core_id}: {e}")
            raise

    def reset_all_offsets(self):
        self.smu_command(self.CMD_RESET_ALL, 0)


def get_physical_core_ids() -> List[int]:
    """Return sorted list of physical core IDs present on the system."""
    cpu_path = Path("/sys/devices/system/cpu")
    core_ids = set()
    for cpu_dir in cpu_path.glob("cpu[0-9]*"):
        core_file = cpu_dir / "topology" / "core_id"
        if core_file.exists():
            try:
                with open(core_file) as f:
                    core_ids.add(int(f.read().strip()))
            except Exception:
                pass
    if core_ids:
        return sorted(core_ids)

    # Fallback: try to use CPU affinity mask to guess number of cores
    try:
        affinity = os.sched_getaffinity(0)
        max_logical = max(affinity) + 1
        physical_cores = max_logical // 2
        logger.warning("Using fallback core count: %d", physical_cores)
        return list(range(physical_cores))
    except Exception:
        logger.warning("Could not read physical core IDs, falling back to 8 cores")
        return list(range(8))


SCRIPT_PATH = Path(__file__).resolve()


def run_privileged(args: List[str]) -> str:
    """Run the script with elevated privileges and return stdout."""
    if os.geteuid() != 0 and not shutil.which("pkexec"):
        raise RuntimeError("pkexec not found. Please install polkit or run this script as root.")
    if os.geteuid() == 0:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--"] + args,
            capture_output=True,
            text=True
        )
    else:
        result = subprocess.run(
            ["pkexec", sys.executable, str(SCRIPT_PATH), "--"] + args,
            capture_output=True,
            text=True
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def cli_mode(cli_args: List[str]):
    parser = argparse.ArgumentParser(description="Ryzen SMU voltage control")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true", help="Output offsets in JSON format")

    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("core", type=int)

    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("core", type=int)
    set_parser.add_argument("offset", type=int)

    apply_list_parser = subparsers.add_parser("apply-list")
    apply_list_parser.add_argument("cores", type=int, nargs="+", help="List of core IDs")
    apply_list_parser.add_argument("offset", type=int)

    apply_file_parser = subparsers.add_parser("apply-file")
    apply_file_parser.add_argument("file", type=str, help="JSON file with core:offset pairs")

    subparsers.add_parser("reset")

    args = parser.parse_args(cli_args)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not RyzenSMU.driver_loaded():
        print("Error: Ryzen SMU driver not loaded. Load it with: sudo modprobe ryzen_smu", file=sys.stderr)
        sys.exit(1)

    smu = RyzenSMU()
    physical_cores = get_physical_core_ids()

    try:
        if args.command == "list":
            if args.json:
                offsets = {core: smu.get_core_offset(core) for core in physical_cores}
                print(json.dumps(offsets))
            else:
                for core in physical_cores:
                    print(f"{core}: {smu.get_core_offset(core)}")
        elif args.command == "get":
            if args.core not in physical_cores:
                print(f"Error: Core {args.core} does not exist", file=sys.stderr)
                sys.exit(1)
            print(smu.get_core_offset(args.core))
        elif args.command == "set":
            if args.core not in physical_cores:
                print(f"Error: Core {args.core} does not exist", file=sys.stderr)
                sys.exit(1)
            smu.set_core_offset(args.core, args.offset)
            print(f"OK: Core {args.core} set to {args.offset}")
        elif args.command == "apply-list":
            invalid_cores = [c for c in args.cores if c not in physical_cores]
            if invalid_cores:
                print(f"Error: Cores {invalid_cores} do not exist", file=sys.stderr)
                sys.exit(1)
            for core in args.cores:
                smu.set_core_offset(core, args.offset)
            for core in args.cores:
                print(f"{core}: {smu.get_core_offset(core)}")
        elif args.command == "apply-file":
            file_path = Path(args.file).resolve()
            profiles_dir = Path("/etc/ruv/profiles").resolve()
            try:
                file_path.relative_to(profiles_dir)
            except ValueError:
                raise ValueError(f"Profile file {file_path} is not under {profiles_dir}")
            with open(file_path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("JSON must be an object with core:offset pairs")

            failed = []
            for core_str, offset in data.items():
                try:
                    core = int(core_str)
                except ValueError:
                    failed.append(f"{core_str}: invalid core ID")
                    continue
                if core not in physical_cores:
                    failed.append(f"{core}: core does not exist")
                    continue
                try:
                    smu.set_core_offset(core, offset)
                except Exception as e:
                    failed.append(f"{core}: {e}")

            if failed:
                print("Errors applying offsets:", file=sys.stderr)
                for err in failed:
                    print(f"  {err}", file=sys.stderr)
                sys.exit(1)

            print("OK: Offsets applied from file. Current offsets:")
            for core in physical_cores:
                print(f"{core}: {smu.get_core_offset(core)}")
        elif args.command == "reset":
            smu.reset_all_offsets()
            print("OK: All offsets reset")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


class CoreSelectionList(QListWidget):
    def __init__(self, core_ids: List[int], parent=None):
        super().__init__(parent)
        self.core_ids = core_ids
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for core in core_ids:
            item = QListWidgetItem(f"Core {core}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.addItem(item)
            item.setData(Qt.ItemDataRole.UserRole, core)

    def get_selected_cores(self) -> List[int]:
        cores = []
        for i in range(self.count()):
            item = self.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                cores.append(item.data(Qt.ItemDataRole.UserRole))
        return cores


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ryzen Undervolt Tool")
        self.resize(800, 600)
        self._set_window_icon()

        self.core_ids = get_physical_core_ids()
        if not self.core_ids:
            self.core_ids = list(range(8))

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)
        central.setLayout(main_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter, 1)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Select cores to undervolt:"))
        self.core_list = CoreSelectionList(self.core_ids)
        left_layout.addWidget(self.core_list)
        splitter.addWidget(left_widget)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(10, 0, 0, 0)
        right_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        right_layout.addWidget(QLabel("Offset (mV):"))
        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(RyzenSMU.MIN_OFFSET, RyzenSMU.MAX_OFFSET)
        self.offset_spin.setValue(0)
        self.offset_spin.setMinimumWidth(100)
        right_layout.addWidget(self.offset_spin)

        self.btn_apply = QPushButton("Apply to Selected Cores")
        self.btn_apply.setMinimumWidth(180)
        right_layout.addWidget(self.btn_apply)

        right_layout.addSpacing(20)

        btn_row = QHBoxLayout()
        self.btn_list = QPushButton("Show Current Offsets")
        self.btn_refresh = QPushButton("Refresh Offsets")
        btn_row.addWidget(self.btn_list)
        btn_row.addWidget(self.btn_refresh)
        right_layout.addLayout(btn_row)

        self.btn_reset = QPushButton("Reset All Offsets")
        right_layout.addWidget(self.btn_reset)

        right_layout.addStretch()
        splitter.addWidget(right_widget)

        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        left_widget.setMinimumWidth(200)
        right_widget.setMinimumWidth(180)
        splitter.setSizes([int(self.width() * 0.6), int(self.width() * 0.4)])

        profile_layout = QHBoxLayout()
        profile_layout.setSpacing(5)
        profile_layout.addWidget(QLabel("Profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(150)
        profile_layout.addWidget(self.profile_combo)
        self.btn_save_profile = QPushButton("Save Current as Profile")
        self.btn_delete_profile = QPushButton("Delete Profile")
        self.btn_apply_profile = QPushButton("Apply Profile")
        self.btn_update_profile = QPushButton("Update Selected Cores in Profile")
        profile_layout.addWidget(self.btn_save_profile)
        profile_layout.addWidget(self.btn_delete_profile)
        profile_layout.addWidget(self.btn_apply_profile)
        profile_layout.addWidget(self.btn_update_profile)
        profile_layout.addStretch()
        main_layout.addLayout(profile_layout)

        boot_layout = QHBoxLayout()
        boot_layout.setSpacing(10)
        self.btn_set_boot = QPushButton("Set as Boot Profile")
        self.btn_remove_boot = QPushButton("Remove Boot Service")
        boot_layout.addWidget(self.btn_set_boot)
        boot_layout.addWidget(self.btn_remove_boot)
        boot_layout.addStretch()
        main_layout.addLayout(boot_layout)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(150)
        main_layout.addWidget(self.output)

        # Connect signals
        self.btn_list.clicked.connect(self.list_offsets)
        self.btn_refresh.clicked.connect(self.refresh_offsets)
        self.btn_reset.clicked.connect(self.reset_offsets)
        self.btn_apply.clicked.connect(self.apply_offset)
        self.btn_save_profile.clicked.connect(self.save_current_as_profile)
        self.btn_delete_profile.clicked.connect(self.delete_profile)
        self.btn_apply_profile.clicked.connect(self.apply_profile)
        self.btn_update_profile.clicked.connect(self.update_profile)
        self.btn_set_boot.clicked.connect(self.set_as_boot_profile)
        self.btn_remove_boot.clicked.connect(self.remove_boot_service)

        self.refresh_profile_list()
        self.refresh_offsets()  # Show current offsets on startup

    def _set_window_icon(self):
        icon = QIcon.fromTheme("ruv-gui")
        if icon.isNull():
            fallback_path = "/usr/share/icons/hicolor/256x256/apps/ruv-gui.png"
            if os.path.exists(fallback_path):
                icon = QIcon(fallback_path)
        if not icon.isNull():
            self.setWindowIcon(icon)

    def list_offsets(self):
        try:
            output = run_privileged(["list"])
            self.output.setText(output)
        except Exception as e:
            self.output.setText(str(e))

    def refresh_offsets(self):
        self.list_offsets()  # Same operation

    def reset_offsets(self):
        try:
            output = run_privileged(["reset"])
            self.output.setText(output)
            # No need to refresh; reset output already indicates success
        except Exception as e:
            self.output.setText(str(e))

    def apply_offset(self):
        selected_cores = self.core_list.get_selected_cores()
        if not selected_cores:
            self.output.setText("No cores selected. Please tick at least one core.")
            return
        offset = self.offset_spin.value()
        if offset < -30 or offset > 30:
            reply = QMessageBox.warning(
                self, "Offset Warning",
                f"Offset {offset} mV is outside the typical ±30 mV range.\n"
                "The ryzen_smu driver or SMU firmware may not support this value.\n"
                "Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        try:
            output = run_privileged(["apply-list"] + [str(c) for c in selected_cores] + [str(offset)])
            self.output.setText(output)
            self.offset_spin.setValue(0)  # Reset after successful apply
        except Exception as e:
            self.output.setText(f"Error applying: {str(e)}")

    def refresh_profile_list(self):
        profiles_dir = Path("/etc/ruv/profiles")
        self.profile_combo.clear()
        try:
            if profiles_dir.exists():
                for f in profiles_dir.glob("*.json"):
                    self.profile_combo.addItem(f.stem)
        except Exception as e:
            self.output.append(f"Error scanning profiles: {e}")

    def save_current_as_profile(self):
        name, ok = QInputDialog.getText(self, "Save Profile", "Profile name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if not re.match(r'^[a-zA-Z0-9_.-]+$', name):
            self.output.setText("Invalid profile name. Use only letters, numbers, underscore, hyphen, and dot.")
            return

        profiles_dir = "/etc/ruv/profiles"
        json_path = f"{profiles_dir}/{name}.json"

        script = str(SCRIPT_PATH)
        python = sys.executable
        cmd = [
            "pkexec", "bash", "-c",
            f"mkdir -p {profiles_dir} && {python} {script} -- list --json > {json_path}"
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip())
            self.output.setText(f"Profile '{name}' saved successfully.")
            self.refresh_profile_list()
            index = self.profile_combo.findText(name)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        except Exception as e:
            self.output.setText(f"Error saving profile: {e}")

    def delete_profile(self):
        name = self.profile_combo.currentText()
        if not name:
            return
        reply = QMessageBox.question(
            self, "Delete Profile",
            f"Delete profile '{name}'? This will also reset all core offsets to 0.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            profiles_dir = "/etc/ruv/profiles"
            json_path = f"{profiles_dir}/{name}.json"
            script = str(SCRIPT_PATH)
            python = sys.executable
            cmd = [
                "pkexec", "bash", "-c",
                f"rm -f {json_path} && {python} {script} -- reset"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip())
            self.output.setText(f"Profile '{name}' deleted and all offsets reset to 0.\n{result.stdout}")
            self.refresh_profile_list()
            # No separate refresh needed
        except Exception as e:
            self.output.setText(f"Error deleting profile: {e}")

    def apply_profile(self):
        name = self.profile_combo.currentText()
        if not name:
            self.output.setText("No profile selected.")
            return
        try:
            profiles_dir = Path("/etc/ruv/profiles").resolve()
            json_path = profiles_dir / f"{name}.json"
            if not json_path.exists():
                self.output.setText(f"Profile file {json_path} not found.")
                return
            output = run_privileged(["apply-file", str(json_path)])
            self.output.setText(output)
            # The output already contains the current offsets; no extra refresh needed.
        except Exception as e:
            self.output.setText(f"Error applying profile: {e}")

    def update_profile(self):
        """
        Update the selected profile by changing the offset of only the selected cores
        to the current spinbox value. Other cores retain their existing values.
        """
        name = self.profile_combo.currentText()
        if not name:
            self.output.setText("No profile selected.")
            return

        selected_cores = self.core_list.get_selected_cores()
        if not selected_cores:
            self.output.setText("No cores selected. Please tick at least one core to update.")
            return

        new_offset = self.offset_spin.value()
        if new_offset < -30 or new_offset > 30:
            reply = QMessageBox.warning(
                self, "Offset Warning",
                f"Offset {new_offset} mV is outside the typical ±30 mV range.\n"
                "Continue updating profile?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        profiles_dir = Path("/etc/ruv/profiles").resolve()
        json_path = profiles_dir / f"{name}.json"

        if not json_path.exists():
            self.output.setText(f"Profile '{name}' does not exist (file missing).")
            return

        try:
            with open(json_path, "r") as f:
                profile_data = json.load(f)

            if not isinstance(profile_data, dict):
                raise ValueError("Profile JSON is not a dictionary")

            # Update selected cores
            for core in selected_cores:
                profile_data[str(core)] = new_offset

            json_text = json.dumps(profile_data, indent=2)
            cmd = ["pkexec", "tee", str(json_path)]
            result = subprocess.run(cmd, input=json_text, text=True, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip())

            apply_reply = QMessageBox.question(
                self, "Apply Updated Profile",
                f"Profile '{name}' updated.\nDo you want to apply it to the CPU now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if apply_reply == QMessageBox.StandardButton.Yes:
                apply_output = run_privileged(["apply-file", str(json_path)])
                self.output.setText(f"Profile updated and applied.\n{apply_output}")
                # apply_output already contains final offsets; no extra refresh
            else:
                self.output.setText(f"Profile '{name}' updated (not applied live).")
            self.offset_spin.setValue(0)

        except Exception as e:
            self.output.setText(f"Error updating profile: {e}")

    def set_as_boot_profile(self):
        name = self.profile_combo.currentText()
        if not name:
            self.output.setText("No profile selected.")
            return
        profiles_dir = Path("/etc/ruv/profiles").resolve()
        json_path = profiles_dir / f"{name}.json"
        if not json_path.exists():
            self.output.setText(f"Profile '{name}' does not exist (file missing).")
            return

        service_content = f"""[Unit]
Description=Apply Ryzen undervolt profile '{name}'
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 {SCRIPT_PATH} -- apply-file {json_path}
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
"""
        service_path = "/etc/systemd/system/ruv-boot.service"
        try:
            cmd = [
                "pkexec", "bash", "-c",
                f"cat > {service_path} <<'EOF'\n{service_content}\nEOF\n"
                f"systemctl daemon-reload && systemctl enable ruv-boot.service"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip())

            self.output.setText(
                f"Boot service installed with profile '{name}'.\n"
                f"Profile will be applied automatically at next boot.\n"
                f"To remove the service, use 'Remove Boot Service' button."
            )
        except Exception as e:
            self.output.setText(f"Error installing boot service: {e}")

    def remove_boot_service(self):
        reply = QMessageBox.question(
            self, "Remove Boot Service",
            "Remove the boot service? This will stop automatic offset loading at startup.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            cmd = [
                "pkexec", "bash", "-c",
                "systemctl disable ruv-boot.service && rm -f /etc/systemd/system/ruv-boot.service && systemctl daemon-reload"
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            self.output.setText("Boot service removed successfully.")
        except subprocess.CalledProcessError as e:
            self.output.setText(f"Error removing boot service: {e.stderr if e.stderr else str(e)}")
        except Exception as e:
            self.output.setText(f"Error removing boot service: {e}")


if __name__ == "__main__":
    if len(sys.argv) == 1 and os.geteuid() == 0:
        print("ERROR: Do not run the GUI as root.", file=sys.stderr)
        sys.exit(1)
    if len(sys.argv) > 1:
        if sys.argv[1] == "--" and len(sys.argv) > 2:
            cli_args = sys.argv[2:]
        else:
            cli_args = sys.argv[1:]
        cli_mode(cli_args)
    else:
        QApplication.setApplicationName("Ryzen Undervolt Tool")
        QApplication.setApplicationDisplayName("Ryzen Undervolt Tool")
        QApplication.setDesktopFileName("ruv-gui")
        app = QApplication(sys.argv)
        app_icon = QIcon.fromTheme("ruv-gui")
        if app_icon.isNull():
            fallback_path = "/usr/share/icons/hicolor/256x256/apps/ruv-gui.png"
            if os.path.exists(fallback_path):
                app_icon = QIcon(fallback_path)
        if not app_icon.isNull():
            app.setWindowIcon(app_icon)
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
