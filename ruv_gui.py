#!/usr/bin/env python3
"""
Linux Undervolt Tool for Ryzen CPUs using the ryzen_smu kernel driver.
Allows reading and setting voltage offsets per core.
"""

import sys
import os
import struct
import subprocess
import time
import argparse
from pathlib import Path
from typing import Optional, Tuple, List

os.environ["QT_LOGGING_RULES"] = "qt.qpa.theme=false"

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton,
    QTextEdit, QVBoxLayout, QHBoxLayout,
    QWidget, QLabel, QSpinBox, QMessageBox
)


class RyzenSMU:
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
        start = time.monotonic()
        while True:
            status = self._read_file32(self.MP1_CMD)
            if status is None:
                raise RuntimeError("Failed to read SMU status")
            if status != 0:
                break
            if time.monotonic() - start > self.SMU_TIMEOUT:
                raise RuntimeError("Timeout waiting for SMU")
            time.sleep(0.1)

        if not self._write_file192(self.SMU_ARGS, arg1, arg2, arg3, arg4, arg5, arg6):
            raise RuntimeError("Failed to write SMU arguments")
        if not self._write_file32(self.MP1_CMD, op):
            raise RuntimeError("Failed to write SMU command")

        start = time.monotonic()
        while True:
            status = self._read_file32(self.MP1_CMD)
            if status is None:
                raise RuntimeError("Failed to read SMU status")
            if status != 0:
                break
            if time.monotonic() - start > self.SMU_TIMEOUT:
                raise RuntimeError("Timeout waiting for SMU command")
            time.sleep(0.1)

        if status != 1:
            raise RuntimeError(f"SMU command failed with status {status}")

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
        arg = (((core_id & 8) << 5 | (core_id & 7)) << 20) | (offset & 0xFFFF)
        self.smu_command(self.CMD_SET_OFFSET, arg)

    def reset_all_offsets(self):
        self.smu_command(self.CMD_RESET_ALL, 0)


def get_physical_core_ids():
    cpu_path = Path("/sys/devices/system/cpu")
    core_ids = set()
    for cpu_dir in cpu_path.glob("cpu[0-9]*"):
        core_file = cpu_dir / "topology" / "core_id"
        if core_file.exists():
            try:
                with open(core_file) as f:
                    core_ids.add(int(f.read().strip()))
            except:
                pass
    return sorted(core_ids)


def cli_mode():
    parser = argparse.ArgumentParser(description="Ryzen SMU voltage control")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list")
    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("core", type=int)

    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("core", type=int)
    set_parser.add_argument("offset", type=int)

    range_parser = subparsers.add_parser("apply-range")
    range_parser.add_argument("start", type=int)
    range_parser.add_argument("end", type=int)
    range_parser.add_argument("offset", type=int)

    subparsers.add_parser("reset")

    args = parser.parse_args()
    if not RyzenSMU.driver_loaded():
        print("Error: Ryzen SMU driver not loaded.", file=sys.stderr)
        sys.exit(1)

    smu = RyzenSMU()
    try:
        if args.command == "list":
            for core in get_physical_core_ids():
                print(f"{core}: {smu.get_core_offset(core)}")
        elif args.command == "get":
            print(smu.get_core_offset(args.core))
        elif args.command == "set":
            smu.set_core_offset(args.core, args.offset)
            print(f"OK: Core {args.core} set to {args.offset}")
        elif args.command == "apply-range":
            for core in range(args.start, args.end):
                smu.set_core_offset(core, args.offset)
            for core in range(args.start, args.end):
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

        self.core_ids = get_physical_core_ids() or list(range(8))
        self.max_cores = len(self.core_ids)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout()
        central.setLayout(main_layout)

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Offset:"))
        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(-32768, 32767)
        self.offset_spin.setValue(0)
        controls_layout.addWidget(self.offset_spin)

        controls_layout.addWidget(QLabel("Cores:"))
        self.core_spin = QSpinBox()
        self.core_spin.setRange(1, self.max_cores)
        self.core_spin.setValue(self.max_cores)
        controls_layout.addWidget(self.core_spin)
        controls_layout.addWidget(QLabel(f"(max {self.max_cores})"))

        self.btn_apply = QPushButton("Apply Offset")
        controls_layout.addWidget(self.btn_apply)
        main_layout.addLayout(controls_layout)

        button_row = QHBoxLayout()
        self.btn_list = QPushButton("Show Current Offsets")
        self.btn_reset = QPushButton("Reset All Offsets (0)")
        button_row.addWidget(self.btn_list)
        button_row.addWidget(self.btn_reset)
        main_layout.addLayout(button_row)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        main_layout.addWidget(self.output)

        self.btn_list.clicked.connect(self.list_offsets)
        self.btn_reset.clicked.connect(self.reset_offsets)
        self.btn_apply.clicked.connect(self.apply_offset)

    def run_privileged(self, args):
        cmd = ["pkexec", sys.executable, __file__, "--"] + args
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        return result.stdout

    def list_offsets(self):
        try:
            self.output.setText(self.run_privileged(["list"]))
        except Exception as e:
            self.output.setText(str(e))

    def reset_offsets(self):
        try:
            self.output.setText(self.run_privileged(["reset"]))
        except Exception as e:
            self.output.setText(str(e))

    def apply_offset(self):
        offset = self.offset_spin.value()
        cores = self.core_spin.value()

        if offset < -30 or offset > 30:
            reply = QMessageBox.warning(
                self,
                "Offset Warning",
                (
                    f"Offset {offset} is outside the typical ±30 range.\n"
                    "The ryzen_smu driver or SMU firmware may not support this value.\n"
                    "Continue?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            output = self.run_privileged([
                "apply-range",
                "0",
                str(cores),
                str(offset)
            ])
            self.output.setText(output)
        except Exception as e:
            self.output.setText(f"Error applying: {str(e)}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--" and len(sys.argv) > 2:
            sys.argv = [sys.argv[0]] + sys.argv[2:]
        cli_mode()
    else:
        app = QApplication(sys.argv)
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
