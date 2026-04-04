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
from pathlib import Path
from typing import List

os.environ["QT_LOGGING_RULES"] = "qt.qpa.theme=false"

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton,
    QTextEdit, QVBoxLayout, QHBoxLayout,
    QWidget, QLabel, QSpinBox, QMessageBox, QFileDialog
)


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

    def __init__(self):
        if not self.driver_loaded():
            raise RuntimeError("Ryzen SMU driver not loaded")

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
        """Send SMU command, wait for completion, return 6-word response."""
        start = time.monotonic()

        # Wait for SMU to become ready (status == 1)
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

        # Write arguments and issue command
        if not self._write_file192(self.SMU_ARGS, arg1, arg2, arg3, arg4, arg5, arg6):
            raise RuntimeError("Failed to write SMU arguments")

        if not self._write_file32(self.MP1_CMD, op):
            raise RuntimeError("Failed to write SMU command")

        start = time.monotonic()

        # Wait for command completion (status returns to 1)
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
        # Core ID encoding for SMU: ( (core_id & 8) << 5 | (core_id & 7) ) << 20
        arg = ((core_id & 8) << 5 | (core_id & 7)) << 20

        try:
            result = self.smu_command(self.CMD_GET_OFFSET, arg)
        except RuntimeError:
            return None

        value = result[0]

        # Convert to signed 32-bit
        if value > 2**31 - 1:
            value -= 2**32

        return value

    def set_core_offset(self, core_id: int, offset: int):
        old_offset = self.get_core_offset(core_id)

        arg = (((core_id & 8) << 5 | (core_id & 7)) << 20) | (offset & 0xFFFF)

        try:
            self.smu_command(self.CMD_SET_OFFSET, arg)
        except Exception:
            # Rollback on failure
            if old_offset is not None:
                try:
                    rollback_arg = (((core_id & 8) << 5 | (core_id & 7)) << 20) | (old_offset & 0xFFFF)
                    self.smu_command(self.CMD_SET_OFFSET, rollback_arg)
                except Exception:
                    pass
            raise

    def reset_all_offsets(self):
        self.smu_command(self.CMD_RESET_ALL, 0)


def get_physical_core_ids() -> List[int]:
    """Read actual core IDs from sysfs topology."""
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

    if not core_ids:
        return list(range(8))  # fallback

    return sorted(core_ids)


SCRIPT_PATH = Path(__file__).resolve()


def run_privileged(args: List[str]) -> str:
    """Run a CLI command with pkexec (or directly if already root)."""
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
        raise RuntimeError(result.stderr.strip())

    return result.stdout


def cli_mode():
    parser = argparse.ArgumentParser(description="Ryzen SMU voltage control")

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

    args = parser.parse_args()

    if not RyzenSMU.driver_loaded():
        print("Error: Ryzen SMU driver not loaded.", file=sys.stderr)
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
            print(smu.get_core_offset(args.core))

        elif args.command == "set":
            smu.set_core_offset(args.core, args.offset)
            print(f"OK: Core {args.core} set to {args.offset}")

        elif args.command == "apply-list":
            for core in args.cores:
                smu.set_core_offset(core, args.offset)
            for core in args.cores:
                print(f"{core}: {smu.get_core_offset(core)}")

        elif args.command == "apply-file":
            with open(args.file) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("JSON must be an object with core:offset pairs")

            for core_str, offset in data.items():
                core = int(core_str)
                if core not in physical_cores:
                    print(f"Warning: Core {core} does not exist, skipping", file=sys.stderr)
                    continue
                smu.set_core_offset(core, offset)

            # Print new offsets after applying (GUI uses this to refresh)
            print("OK: Offsets applied from file. Current offsets:")
            for core in physical_cores:
                print(f"{core}: {smu.get_core_offset(core)}")

        elif args.command == "reset":
            smu.reset_all_offsets()
            print("OK: All offsets reset")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Linux Undervolt Tool")
        self.resize(600, 400)

        self.core_ids = get_physical_core_ids()
        if not self.core_ids:
            self.core_ids = list(range(8))

        self.max_cores = len(self.core_ids)

        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout()
        central.setLayout(main_layout)

        # Offset input and core count selector
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Offset (mV):"))

        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(-32768, 32767)
        self.offset_spin.setValue(0)
        controls_layout.addWidget(self.offset_spin)

        controls_layout.addWidget(QLabel("Number of cores:"))

        self.core_spin = QSpinBox()
        self.core_spin.setRange(1, self.max_cores)
        self.core_spin.setValue(self.max_cores)
        controls_layout.addWidget(self.core_spin)

        controls_layout.addStretch()  # pushes the button to the right

        self.btn_apply = QPushButton("Apply Offset")
        controls_layout.addWidget(self.btn_apply)
        main_layout.addLayout(controls_layout)

        # Action buttons
        button_row = QHBoxLayout()
        self.btn_list = QPushButton("Show Current Offsets")
        self.btn_reset = QPushButton("Reset All Offsets")
        button_row.addWidget(self.btn_list)
        button_row.addWidget(self.btn_reset)
        main_layout.addLayout(button_row)

        # File operations
        save_load_layout = QHBoxLayout()
        self.btn_save = QPushButton("Save Offsets to File")
        self.btn_load = QPushButton("Load Offsets from File")
        save_load_layout.addWidget(self.btn_save)
        save_load_layout.addWidget(self.btn_load)
        main_layout.addLayout(save_load_layout)

        # Output text area
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        main_layout.addWidget(self.output)

        # Signal connections
        self.btn_list.clicked.connect(self.list_offsets)
        self.btn_reset.clicked.connect(self.reset_offsets)
        self.btn_apply.clicked.connect(self.apply_offset)
        self.btn_save.clicked.connect(self.save_offsets)
        self.btn_load.clicked.connect(self.load_offsets)

    def list_offsets(self):
        try:
            output = run_privileged(["list"])
            self.output.setText(output)
        except Exception as e:
            self.output.setText(str(e))

    def reset_offsets(self):
        try:
            output = run_privileged(["reset"])
            self.output.setText(output)
        except Exception as e:
            self.output.setText(str(e))

    def apply_offset(self):
        offset = self.offset_spin.value()
        num_cores = self.core_spin.value()

        # Warn if offset is far outside typical range
        if offset < -30 or offset > 30:
            reply = QMessageBox.warning(
                self,
                "Offset Warning",
                f"Offset {offset} mV is outside the typical ±30 mV range.\n"
                "The ryzen_smu driver or SMU firmware may not support this value.\n"
                "Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Apply to first N physical cores
        selected_cores = self.core_ids[:num_cores]

        try:
            output = run_privileged(["apply-list"] + [str(c) for c in selected_cores] + [str(offset)])
            self.output.setText(output)
        except Exception as e:
            self.output.setText(f"Error applying: {str(e)}")

    def save_offsets(self):
        try:
            output = run_privileged(["list", "--json"])
            offsets = json.loads(output)

            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save Offsets", "", "JSON Files (*.json);;All Files (*)"
            )
            if file_path:
                with open(file_path, "w") as f:
                    json.dump(offsets, f, indent=2)
                self.output.setText(f"Saved offsets to {file_path}")
        except Exception as e:
            self.output.setText(f"Error saving offsets: {str(e)}")

    def load_offsets(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Load Offsets", "", "JSON Files (*.json);;All Files (*)"
        )
        if not file_path:
            return
        try:
            output = run_privileged(["apply-file", file_path])
            self.output.setText(output)  # Output already includes new offsets
        except Exception as e:
            self.output.setText(f"Error loading offsets: {str(e)}")


if __name__ == "__main__":
    # Prevent running GUI as root
    if len(sys.argv) == 1 and os.geteuid() == 0:
        print("ERROR: Do not run the GUI as root.", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) > 1:
        # Strip the '--' marker used by run_privileged
        if sys.argv[1] == "--" and len(sys.argv) > 2:
            sys.argv = [sys.argv[0]] + sys.argv[2:]
        cli_mode()
    else:
        app = QApplication(sys.argv)
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
