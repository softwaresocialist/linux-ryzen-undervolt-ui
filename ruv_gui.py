#!/usr/bin/env python3
"""
Ryzen Undervolt Tool (ruv-gui / ruv)

Provides a GUI and CLI to control Ryzen CPU core voltage offsets
via the ryzen_smu kernel driver.
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
from typing import List, Optional, Dict, Any

# ----------------------------------------------------------------------
# Logging setup (debug mode can be enabled with RUV_DEBUG=1)
# ----------------------------------------------------------------------
log_level = logging.DEBUG if os.environ.get("RUV_DEBUG") == "1" else logging.INFO
logging.basicConfig(level=log_level, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ruv")

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
SCRIPT_PATH = Path(__file__).resolve()
INSTALLED_BIN_PATH = "/usr/local/bin/ruv-gui"
PROFILES_DIR = Path("/etc/ruv/profiles")
ICON_FALLBACK_PATH = "/usr/share/icons/hicolor/256x256/apps/ruv-gui.png"

# PyQt6 imports (only needed for GUI mode)
try:
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QPushButton,
        QTextEdit, QVBoxLayout, QHBoxLayout,
        QWidget, QLabel, QSpinBox, QMessageBox,
        QComboBox, QInputDialog, QListWidget,
        QListWidgetItem, QAbstractItemView, QSplitter
    )
    from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
    from PyQt6.QtGui import QIcon, QCursor
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False
    # We still want the CLI to work even if PyQt6 is missing
    if len(sys.argv) == 1:
        print("PyQt6 is required for the GUI. Please install it or use the CLI.", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------
# Privileged execution helper
# ----------------------------------------------------------------------
class PrivilegedRunner:
    """Run commands with elevated privileges (pkexec or root)."""

    @staticmethod
    def run(args: List[str], input_text: Optional[str] = None) -> str:
        """Execute a privileged command and return its stdout."""
        if os.geteuid() != 0 and not shutil.which("pkexec"):
            raise RuntimeError("pkexec not found. Please install polkit or run as root.")

        try:
            if os.geteuid() == 0:
                result = subprocess.run(
                    [sys.executable, str(SCRIPT_PATH), "--"] + args,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
            else:
                cmd = ["pkexec", sys.executable, str(SCRIPT_PATH), "--"] + args
                result = subprocess.run(
                    cmd,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Operation timed out. The driver may be unresponsive.")
        except subprocess.CalledProcessError as e:
            if e.returncode == 126:
                raise RuntimeError("Authentication cancelled.")
            raise RuntimeError(f"Privileged command failed: {e.stderr.strip() or e.stdout.strip()}")
        except Exception as e:
            raise RuntimeError(f"Unexpected error: {e}")

        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        logger.debug("Privileged command succeeded: %s", args)
        return result.stdout


# ----------------------------------------------------------------------
# Ryzen SMU driver interface
# ----------------------------------------------------------------------
class RyzenSMU:
    """Interface to the ryzen_smu sysfs driver."""

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
        logger.debug("RyzenSMU initialized")

    @classmethod
    def driver_loaded(cls) -> bool:
        """Return True if the driver sysfs interface is present."""
        return cls.VER_PATH.is_file()

    @staticmethod
    def _read_file(file: Path, size: int) -> bytes:
        with open(file, "rb") as fp:
            return fp.read(size)

    @staticmethod
    def _write_file(file: Path, data: bytes) -> int:
        with open(file, "wb") as fp:
            return fp.write(data)

    @classmethod
    def _read_file32(cls, file: Path) -> Optional[int]:
        data = cls._read_file(file, 4)
        if len(data) != 4:
            return None
        return struct.unpack("<I", data)[0]

    @classmethod
    def _write_file32(cls, file: Path, value: int) -> bool:
        data = struct.pack("<I", value)
        return cls._write_file(file, data) == 4

    @classmethod
    def _read_file192(cls, file: Path) -> Optional[tuple]:
        data = cls._read_file(file, 24)
        if len(data) != 24:
            return None
        return struct.unpack("<IIIIII", data)

    @classmethod
    def _write_file192(cls, file: Path, *values: int) -> bool:
        if len(values) != 6:
            raise ValueError("Need exactly 6 values")
        data = struct.pack("<IIIIII", *values)
        return cls._write_file(file, data) == 24

    def smu_command(self, op: int, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0) -> tuple:
        """Send a command to the SMU and return the response tuple."""
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
                raise RuntimeError("Timeout waiting for SMU ready. Driver may be busy.")
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
                raise RuntimeError("Timeout waiting for SMU command to complete.")
            time.sleep(0.05)

        response = self._read_file192(self.SMU_ARGS)
        if response is None:
            raise RuntimeError("Failed to read SMU response")
        return response

    def get_core_offset(self, core_id: int) -> Optional[int]:
        """Return the current voltage offset for a core (in mV)."""
        arg = ((core_id & 8) << 5 | (core_id & 7)) << 20
        try:
            result = self.smu_command(self.CMD_GET_OFFSET, arg)
        except RuntimeError:
            return None
        value = result[0]
        if value > 2**31 - 1:
            value -= 2**32
        return value

    def set_core_offset(self, core_id: int, offset: int) -> None:
        """Set the voltage offset for a core."""
        if not (self.MIN_OFFSET <= offset <= self.MAX_OFFSET):
            raise ValueError(f"Offset {offset} mV is outside allowed range [{self.MIN_OFFSET}, {self.MAX_OFFSET}]")

        old_offset = self.get_core_offset(core_id)
        arg = (((core_id & 8) << 5 | (core_id & 7)) << 20) | (offset & 0xFFFF)
        try:
            self.smu_command(self.CMD_SET_OFFSET, arg)
            logger.debug("Set core %d offset to %d", core_id, offset)
        except Exception:
            # Attempt rollback
            if old_offset is not None:
                try:
                    rollback_arg = (((core_id & 8) << 5 | (core_id & 7)) << 20) | (old_offset & 0xFFFF)
                    self.smu_command(self.CMD_SET_OFFSET, rollback_arg)
                    logger.debug("Rolled back core %d to %d", core_id, old_offset)
                except Exception as e:
                    logger.error("Rollback failed for core %d: %s", core_id, e)
            raise

    def reset_all_offsets(self) -> None:
        """Reset all core offsets to 0."""
        self.smu_command(self.CMD_RESET_ALL, 0)
        logger.debug("Reset all offsets")


# ----------------------------------------------------------------------
# Core detection
# ----------------------------------------------------------------------
def get_physical_core_ids() -> List[int]:
    """
    Return a sorted list of physical core IDs available on the system.
    Uses sysfs topology; falls back to /proc/cpuinfo or a safe default.
    """
    cpu_path = Path("/sys/devices/system/cpu")
    core_ids = set()

    # Try to read core_id from topology
    for cpu_dir in cpu_path.glob("cpu[0-9]*"):
        core_file = cpu_dir / "topology" / "core_id"
        if core_file.exists():
            try:
                core_ids.add(int(core_file.read_text().strip()))
            except (ValueError, OSError):
                pass

    if core_ids:
        logger.debug("Detected cores via sysfs: %s", sorted(core_ids))
        return sorted(core_ids)

    # Fallback: parse /proc/cpuinfo for "cpu cores"
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("cpu cores"):
                    cores = int(line.split(":")[1].strip())
                    logger.debug("Fallback /proc/cpuinfo: %d cores", cores)
                    return list(range(cores))
    except Exception:
        pass

    # Last resort: logical CPU count and HT detection
    total_logical = os.cpu_count() or 8
    try:
        with open("/proc/cpuinfo") as f:
            ht_enabled = any(" ht " in line for line in f if line.startswith("flags"))
    except Exception:
        ht_enabled = False
    physical = total_logical // 2 if ht_enabled else total_logical
    logger.warning("Fallback core count: %d", physical)
    return list(range(physical))


# ----------------------------------------------------------------------
# Profile management helpers (CLI and internal use)
# ----------------------------------------------------------------------
def validate_profile_name(name: str) -> bool:
    """Return True if profile name contains only allowed characters."""
    return bool(re.match(r'^[a-zA-Z0-9_.-]+$', name))


def save_current_offsets_as_profile(name: str) -> None:
    """Save current core offsets to a profile file."""
    if not RyzenSMU.driver_loaded():
        raise RuntimeError("Ryzen SMU driver not loaded.")
    smu = RyzenSMU()
    cores = get_physical_core_ids()
    offsets = {str(core): smu.get_core_offset(core) for core in cores}
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profile_path = PROFILES_DIR / f"{name}.json"
    with open(profile_path, "w") as f:
        json.dump(offsets, f, indent=2)
    logger.info("Profile '%s' saved.", name)


def apply_profile_file(profile_path: Path) -> None:
    """Apply all offsets from a profile JSON file."""
    if not RyzenSMU.driver_loaded():
        raise RuntimeError("Ryzen SMU driver not loaded.")
    smu = RyzenSMU()
    cores = get_physical_core_ids()
    with open(profile_path) as f:
        data = json.load(f)
    failed = []
    for core_str, offset in data.items():
        try:
            core = int(core_str)
        except ValueError:
            failed.append(f"{core_str}: invalid core ID")
            continue
        if core not in cores:
            failed.append(f"{core}: core does not exist")
            continue
        try:
            smu.set_core_offset(core, offset)
        except Exception as e:
            failed.append(f"{core}: {e}")
    if failed:
        raise RuntimeError("Errors applying offsets:\n" + "\n".join(failed))
    logger.info("Profile '%s' applied.", profile_path.stem)


# ----------------------------------------------------------------------
# CLI handling (split into sub-functions)
# ----------------------------------------------------------------------
def cli_status(args: argparse.Namespace) -> None:
    """Handle 'status' command."""
    smu = RyzenSMU()
    cores = get_physical_core_ids()
    if getattr(args, 'json', False):
        offsets = {str(core): smu.get_core_offset(core) for core in cores}
        print(json.dumps(offsets))
    else:
        for core in cores:
            print(f"Core {core}: {smu.get_core_offset(core)} mV")


def cli_get(args: argparse.Namespace) -> None:
    """Handle 'get' command."""
    smu = RyzenSMU()
    cores = get_physical_core_ids()
    if args.core not in cores:
        print(f"Error: Core {args.core} does not exist", file=sys.stderr)
        sys.exit(1)
    print(f"{smu.get_core_offset(args.core)} mV")


def cli_set(args: argparse.Namespace) -> None:
    """Handle 'set' command."""
    smu = RyzenSMU()
    cores = get_physical_core_ids()
    if args.core not in cores:
        print(f"Error: Core {args.core} does not exist", file=sys.stderr)
        sys.exit(1)
    smu.set_core_offset(args.core, args.offset)
    print(f"Core {args.core} set to {args.offset} mV")


def cli_apply(args: argparse.Namespace) -> None:
    """Handle 'apply' command."""
    profile_path = PROFILES_DIR / f"{args.name}.json"
    if not profile_path.is_file():
        print(f"Error: Profile '{args.name}' not found", file=sys.stderr)
        sys.exit(1)
    try:
        apply_profile_file(profile_path)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    # Show current offsets after apply
    smu = RyzenSMU()
    cores = get_physical_core_ids()
    for core in cores:
        print(f"Core {core}: {smu.get_core_offset(core)} mV")


def cli_reset(args: argparse.Namespace) -> None:
    """Handle 'reset' command."""
    smu = RyzenSMU()
    smu.reset_all_offsets()
    print("All offsets reset to 0 mV")


def cli_profile_list(args: argparse.Namespace) -> None:
    """Handle 'profile list' command."""
    if PROFILES_DIR.exists():
        profiles = [p.stem for p in PROFILES_DIR.glob("*.json")]
        if profiles:
            print("\n".join(sorted(profiles)))
        else:
            print("No profiles found.")
    else:
        print("No profiles directory exists.")


def cli_profile_save(args: argparse.Namespace) -> None:
    """Handle 'profile save' command."""
    name = args.name.strip()
    if not validate_profile_name(name):
        print("Invalid profile name.", file=sys.stderr)
        sys.exit(1)
    try:
        save_current_offsets_as_profile(name)
        print(f"Profile '{name}' saved.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cli_profile_delete(args: argparse.Namespace) -> None:
    """Handle 'profile delete' command."""
    name = args.name.strip()
    profile_path = PROFILES_DIR / f"{name}.json"
    if not profile_path.is_file():
        print(f"Profile '{name}' does not exist.", file=sys.stderr)
        sys.exit(1)
    response = input(f"Delete profile '{name}' and reset offsets? [y/N] ")
    if response.lower() != 'y':
        print("Cancelled.")
        return
    profile_path.unlink()
    if RyzenSMU.driver_loaded():
        smu = RyzenSMU()
        smu.reset_all_offsets()
        print(f"Profile '{name}' deleted and offsets reset.")
    else:
        print(f"Profile '{name}' deleted.")


def cli_profile_update(args: argparse.Namespace) -> None:
    """Handle 'profile update' command."""
    name = args.name.strip()
    profile_path = PROFILES_DIR / f"{name}.json"
    if not profile_path.is_file():
        print(f"Profile '{name}' does not exist.", file=sys.stderr)
        sys.exit(1)
    cores_available = get_physical_core_ids()
    for core in args.cores:
        if core not in cores_available:
            print(f"Error: Core {core} does not exist.", file=sys.stderr)
            sys.exit(1)

    with open(profile_path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        print("Invalid profile JSON.", file=sys.stderr)
        sys.exit(1)

    for core in args.cores:
        data[str(core)] = args.offset

    with open(profile_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Profile '{name}' updated.")

    if args.apply:
        if not RyzenSMU.driver_loaded():
            print("Warning: Driver not loaded.", file=sys.stderr)
        else:
            smu = RyzenSMU()
            for core in args.cores:
                smu.set_core_offset(core, args.offset)
            print("Applied to CPU.")


def cli_boot_enable(args: argparse.Namespace) -> None:
    """Handle 'boot enable' command."""
    name = args.name.strip()
    profile_path = PROFILES_DIR / f"{name}.json"
    if not profile_path.is_file():
        print(f"Error: Profile '{name}' does not exist.", file=sys.stderr)
        sys.exit(1)

    service_content = f"""[Unit]
Description=Apply Ryzen undervolt profile '{name}'
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 {INSTALLED_BIN_PATH} -- apply-file {profile_path}
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
"""
    service_path = "/etc/systemd/system/ruv-boot.service"
    try:
        with open(service_path, "w") as f:
            f.write(service_content)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "ruv-boot.service"], check=True)
        print(f"Boot service enabled with profile '{name}'.")
    except PermissionError:
        print("Permission denied. Run with sudo.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cli_boot_disable(args: argparse.Namespace) -> None:
    """Handle 'boot disable' command."""
    try:
        subprocess.run(["systemctl", "disable", "ruv-boot.service"], check=True, capture_output=True)
        service_path = "/etc/systemd/system/ruv-boot.service"
        if os.path.exists(service_path):
            os.remove(service_path)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        print("Boot service disabled.")
    except PermissionError:
        print("Permission denied. Run with sudo.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cli_boot_status(args: argparse.Namespace) -> None:
    """Handle 'boot status' command."""
    try:
        result = subprocess.run(["systemctl", "is-enabled", "ruv-boot.service"],
                                capture_output=True, text=True)
        enabled = result.stdout.strip()
        if enabled == "enabled":
            try:
                with open("/etc/systemd/system/ruv-boot.service") as f:
                    content = f.read()
                    match = re.search(r"apply-file\s+(\S+)", content)
                    profile = match.group(1) if match else "unknown"
                print(f"Boot service ENABLED (profile: {profile})")
            except Exception:
                print("Boot service ENABLED")
        elif enabled == "disabled":
            print("Boot service disabled")
        else:
            print("Boot service not installed")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------
def cli_mode(cli_args: List[str]) -> None:
    """Parse CLI arguments and execute the requested command."""
    parser = argparse.ArgumentParser(
        description="Ryzen SMU voltage control – CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo ruv status                 Show current offsets
  sudo ruv get 2                  Get offset of core 2
  sudo ruv set 2 -30              Set core 2 offset to -30 mV
  sudo ruv apply gaming           Apply profile 'gaming'
  sudo ruv profile list           List saved profiles
  sudo ruv profile save myprofile Save current offsets as 'myprofile'
  sudo ruv profile update myprofile --cores 0 2 4 --offset -40
  sudo ruv boot enable myprofile  Apply 'myprofile' at every boot
  sudo ruv boot disable           Disable boot-time application
  sudo ruv reset                  Reset all offsets to 0
        """
    )
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    status_parser = subparsers.add_parser("status", help="Show current core voltage offsets")
    status_parser.add_argument("--json", action="store_true", help="Output in JSON format")

    # get
    get_parser = subparsers.add_parser("get", help="Get offset for a specific core")
    get_parser.add_argument("core", type=int, help="Core ID")

    # set
    set_parser = subparsers.add_parser("set", help="Set offset for a specific core")
    set_parser.add_argument("core", type=int, help="Core ID")
    set_parser.add_argument("offset", type=int, help="Offset in mV")

    # apply
    apply_parser = subparsers.add_parser("apply", help="Apply a saved profile by name")
    apply_parser.add_argument("name", help="Profile name (without .json)")

    # profile
    profile_parser = subparsers.add_parser("profile", help="Manage profiles")
    profile_sub = profile_parser.add_subparsers(dest="profile_cmd", required=True)

    profile_list = profile_sub.add_parser("list", help="List saved profiles")
    profile_save = profile_sub.add_parser("save", help="Save current offsets as a profile")
    profile_save.add_argument("name", help="Profile name (alphanumeric, underscore, hyphen, dot)")
    profile_delete = profile_sub.add_parser("delete", help="Delete a profile")
    profile_delete.add_argument("name", help="Profile name")
    profile_update = profile_sub.add_parser("update", help="Update specific cores in a profile")
    profile_update.add_argument("name", help="Profile name")
    profile_update.add_argument("--cores", type=int, nargs="+", required=True)
    profile_update.add_argument("--offset", type=int, required=True)
    profile_update.add_argument("--apply", action="store_true")

    # boot
    boot_parser = subparsers.add_parser("boot", help="Manage boot-time application")
    boot_sub = boot_parser.add_subparsers(dest="boot_cmd", required=True)
    boot_enable = boot_sub.add_parser("enable", help="Enable automatic profile application at boot")
    boot_enable.add_argument("name", help="Profile name to apply at boot")
    boot_disable = boot_sub.add_parser("disable", help="Disable boot-time application")
    boot_status = boot_sub.add_parser("status", help="Show boot service status")

    # reset
    subparsers.add_parser("reset", help="Reset all offsets to 0")

    # Internal commands (used via pkexec)
    subparsers.add_parser("list", help=argparse.SUPPRESS)  # alias for status
    apply_file_parser = subparsers.add_parser("apply-file", help=argparse.SUPPRESS)
    apply_file_parser.add_argument("file", type=str)
    apply_list_parser = subparsers.add_parser("apply-list", help=argparse.SUPPRESS)
    apply_list_parser.add_argument("cores", type=int, nargs="+")
    apply_list_parser.add_argument("offset", type=int)
    read_profile_parser = subparsers.add_parser("read-profile", help=argparse.SUPPRESS)
    read_profile_parser.add_argument("file", type=str)
    write_profile_parser = subparsers.add_parser("write-profile", help=argparse.SUPPRESS)
    write_profile_parser.add_argument("file", type=str)
    delete_profile_file_parser = subparsers.add_parser("delete-profile-file", help=argparse.SUPPRESS)
    delete_profile_file_parser.add_argument("file", type=str)
    save_profile_combined = subparsers.add_parser("save-profile-combined", help=argparse.SUPPRESS)
    save_profile_combined.add_argument("name", help="Profile name")
    install_boot_service_parser = subparsers.add_parser("install-boot-service", help=argparse.SUPPRESS)
    install_boot_service_parser.add_argument("service_path", type=str)
    subparsers.add_parser("remove-boot-service", help=argparse.SUPPRESS)

    args = parser.parse_args(cli_args)

    # ------------------------------------------------------------------
    # Internal privileged commands (called via pkexec)
    # ------------------------------------------------------------------
    if args.command == "read-profile":
        path = Path(args.file).resolve()
        try:
            path.relative_to(PROFILES_DIR)
        except ValueError:
            print(f"Error: Profile file {path} is not under {PROFILES_DIR}", file=sys.stderr)
            sys.exit(1)
        try:
            with open(path) as f:
                print(f.read(), end="")
        except Exception as e:
            print(f"Error reading profile: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "write-profile":
        path = Path(args.file).resolve()
        try:
            path.relative_to(PROFILES_DIR)
        except ValueError:
            print(f"Error: Profile file {path} is not under {PROFILES_DIR}", file=sys.stderr)
            sys.exit(1)
        try:
            data = sys.stdin.read()
            json.loads(data)  # validate
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                f.write(data)
        except Exception as e:
            print(f"Error writing profile: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "delete-profile-file":
        path = Path(args.file).resolve()
        try:
            path.relative_to(PROFILES_DIR)
        except ValueError:
            print(f"Error: Profile file {path} is not under {PROFILES_DIR}", file=sys.stderr)
            sys.exit(1)
        try:
            if path.exists():
                path.unlink()
        except Exception as e:
            print(f"Error deleting profile: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "save-profile-combined":
        name = args.name.strip()
        if not validate_profile_name(name):
            print("Invalid profile name.", file=sys.stderr)
            sys.exit(1)
        try:
            save_current_offsets_as_profile(name)
            print(f"Profile '{name}' saved.")
        except Exception as e:
            print(f"Error saving profile: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "install-boot-service":
        service_path = args.service_path
        service_content = sys.stdin.read()
        try:
            with open(service_path, "w") as f:
                f.write(service_content)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "enable", "ruv-boot.service"], check=True)
        except Exception as e:
            print(f"Error installing boot service: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "remove-boot-service":
        try:
            subprocess.run(["systemctl", "disable", "ruv-boot.service"], check=True, capture_output=True)
            service_path = "/etc/systemd/system/ruv-boot.service"
            if os.path.exists(service_path):
                os.remove(service_path)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
        except Exception as e:
            print(f"Error removing boot service: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # Aliases and internal conversions
    if args.command == "list":
        args.command = "status"
    elif args.command == "apply-file":
        args.command = "apply"
        args.name = Path(args.file).stem
    elif args.command == "apply-list":
        # Direct application of offsets to specified cores
        if not RyzenSMU.driver_loaded():
            print("Error: Ryzen SMU driver not loaded.", file=sys.stderr)
            sys.exit(1)
        smu = RyzenSMU()
        cores_available = get_physical_core_ids()
        invalid = [c for c in args.cores if c not in cores_available]
        if invalid:
            print(f"Error: Cores {invalid} do not exist", file=sys.stderr)
            sys.exit(1)
        try:
            for core in args.cores:
                smu.set_core_offset(core, args.offset)
            for core in args.cores:
                print(f"{core}: {smu.get_core_offset(core)}")
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # ------------------------------------------------------------------
    # Regular user commands
    # ------------------------------------------------------------------
    if not RyzenSMU.driver_loaded():
        print("Error: Ryzen SMU driver not loaded.", file=sys.stderr)
        sys.exit(1)

    if args.command == "status":
        cli_status(args)
    elif args.command == "get":
        cli_get(args)
    elif args.command == "set":
        cli_set(args)
    elif args.command == "apply":
        cli_apply(args)
    elif args.command == "reset":
        cli_reset(args)
    elif args.command == "profile":
        if args.profile_cmd == "list":
            cli_profile_list(args)
        elif args.profile_cmd == "save":
            cli_profile_save(args)
        elif args.profile_cmd == "delete":
            cli_profile_delete(args)
        elif args.profile_cmd == "update":
            cli_profile_update(args)
    elif args.command == "boot":
        if args.boot_cmd == "enable":
            cli_boot_enable(args)
        elif args.boot_cmd == "disable":
            cli_boot_disable(args)
        elif args.boot_cmd == "status":
            cli_boot_status(args)
    else:
        parser.print_help()


# ----------------------------------------------------------------------
# GUI components (only loaded if needed)
# ----------------------------------------------------------------------
if GUI_AVAILABLE:

    class WorkerThread(QThread):
        """Worker thread for executing privileged commands without freezing the GUI."""
        finished = pyqtSignal(str)
        error = pyqtSignal(str)

        def __init__(self, args: List[str], input_text: Optional[str] = None):
            super().__init__()
            self.args = args
            self.input_text = input_text

        def run(self):
            logger.debug("WorkerThread started")
            try:
                output = PrivilegedRunner.run(self.args, self.input_text)
                self.finished.emit(output)
            except Exception as e:
                self.error.emit(str(e))
            logger.debug("WorkerThread finished")


    class CoreSelectionList(QListWidget):
        """List widget with checkboxes for selecting CPU cores."""

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
            """Return list of core IDs that are currently checked."""
            cores = []
            for i in range(self.count()):
                item = self.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    cores.append(item.data(Qt.ItemDataRole.UserRole))
            return cores


    class MainWindow(QMainWindow):
        """Main GUI window for the Ryzen Undervolt Tool."""

        def __init__(self):
            super().__init__()
            self.setWindowTitle("Ryzen Undervolt Tool")
            self.resize(800, 600)
            self._set_window_icon()

            self.core_ids = get_physical_core_ids()
            self.workers: List[QThread] = []
            self._busy = False

            self._setup_ui()
            self.refresh_profile_list()
            self.list_offsets()

        def _set_window_icon(self):
            icon = QIcon.fromTheme("ruv-gui")
            if icon.isNull() and os.path.exists(ICON_FALLBACK_PATH):
                icon = QIcon(ICON_FALLBACK_PATH)
            if not icon.isNull():
                self.setWindowIcon(icon)

        def _setup_ui(self):
            central = QWidget()
            self.setCentralWidget(central)
            main_layout = QVBoxLayout()
            main_layout.setContentsMargins(5, 5, 5, 5)
            main_layout.setSpacing(5)
            central.setLayout(main_layout)

            splitter = QSplitter(Qt.Orientation.Horizontal)
            main_layout.addWidget(splitter, 1)

            # Left panel: core selection
            left_widget = QWidget()
            left_layout = QVBoxLayout(left_widget)
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.addWidget(QLabel("Select cores to undervolt:"))
            self.core_list = CoreSelectionList(self.core_ids)
            left_layout.addWidget(self.core_list)
            splitter.addWidget(left_widget)

            # Right panel: controls
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

            self.btn_list = QPushButton("Show Current Offsets")
            right_layout.addWidget(self.btn_list)

            self.btn_reset = QPushButton("Reset All Offsets")
            right_layout.addWidget(self.btn_reset)

            right_layout.addStretch()
            splitter.addWidget(right_widget)

            splitter.setCollapsible(0, False)
            splitter.setCollapsible(1, False)
            left_widget.setMinimumWidth(200)
            right_widget.setMinimumWidth(180)
            splitter.setSizes([int(self.width() * 0.6), int(self.width() * 0.4)])

            # Profile management row
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

            # Boot service row
            boot_layout = QHBoxLayout()
            boot_layout.setSpacing(10)
            self.btn_set_boot = QPushButton("Set as Boot Profile")
            self.btn_remove_boot = QPushButton("Remove Boot Service")
            boot_layout.addWidget(self.btn_set_boot)
            boot_layout.addWidget(self.btn_remove_boot)
            boot_layout.addStretch()
            main_layout.addLayout(boot_layout)

            # Output area
            self.output = QTextEdit()
            self.output.setReadOnly(True)
            self.output.setMaximumHeight(150)
            main_layout.addWidget(self.output)

            # Connect signals
            self.btn_list.clicked.connect(self.list_offsets)
            self.btn_reset.clicked.connect(self.reset_offsets)
            self.btn_apply.clicked.connect(self.apply_offset)
            self.btn_save_profile.clicked.connect(self.save_current_as_profile)
            self.btn_delete_profile.clicked.connect(self.delete_profile)
            self.btn_apply_profile.clicked.connect(self.apply_profile)
            self.btn_update_profile.clicked.connect(self.update_profile)
            self.btn_set_boot.clicked.connect(self.set_as_boot_profile)
            self.btn_remove_boot.clicked.connect(self.remove_boot_service)

        def _set_busy(self, busy: bool):
            """Enable/disable UI elements during background operations."""
            if self._busy == busy:
                return
            self._busy = busy
            widgets = [
                self.btn_apply, self.btn_list, self.btn_reset,
                self.btn_save_profile, self.btn_delete_profile,
                self.btn_apply_profile, self.btn_update_profile,
                self.btn_set_boot, self.btn_remove_boot,
                self.profile_combo, self.offset_spin, self.core_list
            ]
            for w in widgets:
                w.setEnabled(not busy)
            if busy:
                QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
            else:
                QApplication.restoreOverrideCursor()

        def _worker_cleanup(self, worker: QThread):
            """Remove finished worker and restore UI if no more workers are running."""
            if worker in self.workers:
                self.workers.remove(worker)
            if not self.workers:
                self._set_busy(False)

        def _run_privileged_async(self, args: List[str], on_finish,
                                  on_error=None, input_text: Optional[str] = None):
            """Run a privileged command in a background thread."""
            if not self.workers:
                self._set_busy(True)
            worker = WorkerThread(args, input_text)

            def handle_finish(output):
                try:
                    on_finish(output)
                except Exception as e:
                    logger.error("Error in finished callback: %s", e)
                    self.output.setText(f"Error in callback: {e}")
                finally:
                    self._worker_cleanup(worker)

            def handle_error(err):
                try:
                    if on_error:
                        on_error(err)
                    else:
                        self.output.setText(f"Error: {err}")
                except Exception as e:
                    logger.error("Error in error callback: %s", e)
                    self.output.setText(f"Error in error handler: {e}")
                finally:
                    self._worker_cleanup(worker)

            worker.finished.connect(handle_finish)
            worker.error.connect(handle_error)
            self.workers.append(worker)
            worker.start()

        def list_offsets(self):
            def on_finish(output):
                self.output.setText(output)
            self._run_privileged_async(["list"], on_finish)

        def reset_offsets(self):
            def on_finish(output):
                self.output.setText(output)
                self.offset_spin.setValue(0)
            self._run_privileged_async(["reset"], on_finish)

        def apply_offset(self):
            selected = self.core_list.get_selected_cores()
            if not selected:
                self.output.setText("No cores selected.")
                return
            offset = self.offset_spin.value()
            args = ["apply-list"] + [str(c) for c in selected] + [str(offset)]

            def on_finish(output):
                self.output.setText(output)
                self.offset_spin.setValue(0)
            self._run_privileged_async(args, on_finish)

        def refresh_profile_list(self):
            self.profile_combo.clear()
            try:
                if PROFILES_DIR.exists():
                    for f in PROFILES_DIR.glob("*.json"):
                        self.profile_combo.addItem(f.stem)
            except Exception as e:
                self.output.append(f"Error scanning profiles: {e}")

        def save_current_as_profile(self):
            name, ok = QInputDialog.getText(self, "Save Profile", "Profile name:")
            if not ok or not name.strip():
                return
            name = name.strip()
            if not validate_profile_name(name):
                self.output.setText("Invalid profile name.")
                return

            def on_finish(output):
                self.output.setText(output)
                self.refresh_profile_list()
                index = self.profile_combo.findText(name)
                if index >= 0:
                    self.profile_combo.setCurrentIndex(index)
            self._run_privileged_async(["save-profile-combined", name], on_finish)

        def delete_profile(self):
            name = self.profile_combo.currentText()
            if not name:
                return
            reply = QMessageBox.question(
                self, "Delete Profile",
                f"Delete profile '{name}' and reset offsets?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            json_path = PROFILES_DIR / f"{name}.json"

            # Combined operation: reset offsets and delete profile file
            class CombinedWorker(QThread):
                finished = pyqtSignal(str)
                error = pyqtSignal(str)

                def run(self):
                    try:
                        PrivilegedRunner.run(["reset"])
                        PrivilegedRunner.run(["delete-profile-file", str(json_path)])
                        self.finished.emit(f"Profile '{name}' deleted and offsets reset.")
                    except Exception as e:
                        self.error.emit(str(e))

            self._set_busy(True)
            worker = CombinedWorker()

            def on_done(msg):
                self.output.setText(msg)
                self.refresh_profile_list()
                self._set_busy(False)

            def on_err(err):
                self.output.setText(f"Error: {err}")
                self._set_busy(False)

            worker.finished.connect(on_done)
            worker.error.connect(on_err)
            worker.start()
            self.workers.append(worker)

        def apply_profile(self):
            name = self.profile_combo.currentText()
            if not name:
                self.output.setText("No profile selected.")
                return
            json_path = PROFILES_DIR / f"{name}.json"
            if not json_path.exists():
                self.output.setText("Profile file not found.")
                return

            def on_finish(output):
                self.output.setText(output)
            self._run_privileged_async(["apply-file", str(json_path)], on_finish)

        def update_profile(self):
            name = self.profile_combo.currentText()
            if not name:
                self.output.setText("No profile selected.")
                return
            selected = self.core_list.get_selected_cores()
            if not selected:
                self.output.setText("No cores selected.")
                return
            new_offset = self.offset_spin.value()
            json_path = PROFILES_DIR / f"{name}.json"
            if not json_path.exists():
                self.output.setText("Profile does not exist.")
                return

            def on_read_profile(raw_json):
                try:
                    data = json.loads(raw_json)
                    if not isinstance(data, dict):
                        raise ValueError("Invalid JSON")
                    for core in selected:
                        data[str(core)] = new_offset
                    json_text = json.dumps(data, indent=2)

                    def on_write_done(msg):
                        reply = QMessageBox.question(
                            self, "Apply Updated Profile",
                            f"Profile '{name}' updated. Apply to CPU now?",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.Yes
                        )
                        if reply == QMessageBox.StandardButton.Yes:
                            def on_apply_done(output):
                                self.output.setText(f"Profile updated and applied.\n{output}")
                                self.offset_spin.setValue(0)
                            self._run_privileged_async(["apply-file", str(json_path)], on_apply_done)
                        else:
                            self.output.setText(f"Profile '{name}' updated (not applied).")
                            self.offset_spin.setValue(0)

                    self._run_privileged_async(
                        ["write-profile", str(json_path)],
                        on_write_done,
                        input_text=json_text
                    )
                except Exception as e:
                    self.output.setText(f"Error: {e}")

            self._run_privileged_async(["read-profile", str(json_path)], on_read_profile)

        def set_as_boot_profile(self):
            name = self.profile_combo.currentText()
            if not name:
                self.output.setText("No profile selected.")
                return
            json_path = PROFILES_DIR / f"{name}.json"
            if not json_path.exists():
                self.output.setText("Profile does not exist.")
                return

            service_content = f"""[Unit]
Description=Apply Ryzen undervolt profile '{name}'
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 {INSTALLED_BIN_PATH} -- apply-file {json_path}
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
"""
            def on_done(msg):
                self.output.setText(f"Boot service installed with profile '{name}'.")
            self._run_privileged_async(
                ["install-boot-service", "/etc/systemd/system/ruv-boot.service"],
                on_done,
                input_text=service_content
            )

        def remove_boot_service(self):
            reply = QMessageBox.question(
                self, "Remove Boot Service",
                "Remove the boot service?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

            def on_done(msg):
                self.output.setText("Boot service removed.")
            self._run_privileged_async(["remove-boot-service"], on_done)


# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Prevent running GUI as root
    if len(sys.argv) == 1 and os.geteuid() == 0:
        print("ERROR: Do not run the GUI as root.", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) > 1:
        # CLI mode
        if sys.argv[1] == "--" and len(sys.argv) > 2:
            cli_args = sys.argv[2:]
        else:
            cli_args = sys.argv[1:]
        cli_mode(cli_args)
    else:
        # GUI mode
        if not GUI_AVAILABLE:
            print("PyQt6 is required for the GUI.", file=sys.stderr)
            sys.exit(1)

        # Suppress Qt theme warnings
        os.environ["QT_LOGGING_RULES"] = "qt.qpa.theme=false"

        QApplication.setApplicationName("Ryzen Undervolt Tool")
        QApplication.setApplicationDisplayName("Ryzen Undervolt Tool")
        QApplication.setDesktopFileName("ruv-gui")
        app = QApplication(sys.argv)

        app_icon = QIcon.fromTheme("ruv-gui")
        if app_icon.isNull() and os.path.exists(ICON_FALLBACK_PATH):
            app_icon = QIcon(ICON_FALLBACK_PATH)
        if not app_icon.isNull():
            app.setWindowIcon(app_icon)

        window = MainWindow()
        window.show()
        sys.exit(app.exec())
