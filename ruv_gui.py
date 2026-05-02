#!/usr/bin/env python3
"""
Ryzen Undervolt Tool (ruv-gui / ruv)
GUI and CLI to control Ryzen CPU core voltage offsets via the ryzen_smu driver.

Supports:
  - Vermeer (Ryzen 5000)         – full read/write (opcodes 0x48, 0x35, 0x36)
  - Granite Ridge (Ryzen 9000)   – write‑only (opcodes 0x50+i) with local cache
  - Raphael (Ryzen 7000)         – unsupported (SMU commands unknown)
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
import tempfile
import fcntl
import atexit
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from enum import Enum

# ----------------------------------------------------------------------------
# Logging (debug mode: RUV_DEBUG=1)
# ----------------------------------------------------------------------------
log_level = logging.DEBUG if os.environ.get("RUV_DEBUG") == "1" else logging.INFO
logging.basicConfig(level=log_level, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ruv")

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
SCRIPT_PATH = Path(__file__).resolve()
INSTALLED_BIN_PATH = "/usr/local/bin/ruv-gui"          # CLI tool path after installation
PROFILES_DIR = Path("/etc/ruv/profiles")
ICON_FALLBACK_PATH = "/usr/share/icons/hicolor/256x256/apps/ruv-gui.png"
LOCK_FILE = "/var/run/ruv.lock"                        # concurrency guard for privileged ops
CACHE_DIR = Path("/var/cache/ruv")                     # better than /etc/ruv for non‑config cache
CO_CACHE_FILE = CACHE_DIR / "co_cache.json"

# PyQt6 – only needed for the GUI
try:
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QPushButton,
        QTextEdit, QVBoxLayout, QHBoxLayout,
        QWidget, QLabel, QSpinBox, QMessageBox,
        QComboBox, QInputDialog, QListWidget,
        QListWidgetItem, QAbstractItemView, QSplitter
    )
    from PyQt6.QtCore import Qt, QThread, pyqtSignal
    from PyQt6.QtGui import QIcon, QCursor
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False
    if len(sys.argv) == 1:                  # no arguments → user tried to launch GUI
        print("PyQt6 is required for the GUI. Please install it or use the CLI.", file=sys.stderr)
        sys.exit(1)

# ----------------------------------------------------------------------------
# Concurrency guard (privileged operations only)
# ----------------------------------------------------------------------------
_lock_fd: Optional[int] = None

def acquire_lock() -> None:
    """
    Acquire an exclusive lock to prevent concurrent SMU access.
    The lock file is created and kept until the process exits; it will be
    cleaned up by the OS (tmpfs) on reboot if still present.
    """
    global _lock_fd
    if os.geteuid() != 0:
        return                         # only root processes compete
    try:
        _lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        # IMPORTANT: Do NOT unlink the file – that would break mutual exclusion.
        atexit.register(release_lock)
    except (IOError, OSError):
        print("Error: Another instance of ruv is already running (SMU access locked). "
              "Please wait for it to finish.", file=sys.stderr)
        sys.exit(1)

def release_lock() -> None:
    """Release the SMU access lock. Called automatically at exit."""
    global _lock_fd
    if _lock_fd:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except Exception:
            pass
        finally:
            _lock_fd = None
        # Remove the lock file only after releasing (good practice, but not required)
        try:
            os.unlink(LOCK_FILE)
        except Exception:
            pass

# ----------------------------------------------------------------------------
# Privileged execution helper (pkexec / direct root)
# ----------------------------------------------------------------------------
class PrivilegedRunner:
    """Run commands with elevated privileges using pkexec or directly as root."""

    @staticmethod
    def run(args: List[str], input_text: Optional[str] = None) -> str:
        if os.geteuid() != 0 and not shutil.which("pkexec"):
            raise RuntimeError("pkexec not found. Please install polkit or run as root.")
        try:
            if os.geteuid() == 0:
                cmd = [sys.executable, str(SCRIPT_PATH), "--"] + args
            else:
                cmd = ["pkexec", sys.executable, str(SCRIPT_PATH), "--"] + args
            result = subprocess.run(
                cmd, input=input_text, capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Operation timed out. The driver may be unresponsive.")
        except Exception as e:
            raise RuntimeError(f"Unexpected error: {e}")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        logger.debug("Privileged command succeeded: %s", args)
        return result.stdout

# ----------------------------------------------------------------------------
# CPU model detection fallback (when codename file is ambiguous)
# ----------------------------------------------------------------------------
def detect_generation_from_cpuinfo() -> Optional['RyzenSMU.Generation']:
    """
    Try to determine the Ryzen generation from /proc/cpuinfo model name.
    Returns a Generation or None if detection fails.
    """
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    name = line.split(":", 1)[1].strip().lower()
                    if "ryzen" not in name:
                        continue
                    # Example: "ryzen 7 5700x3d" → series=7, model=5700
                    match = re.search(r'ryzen\s+(\d)\s*(\d{3,4})?', name)
                    if not match or match.group(2) is None:
                        continue
                    model_str = match.group(2)
                    model = int(model_str)
                    prefix = model // 1000   # first digit of model number
                    # Mapping: 5xxx -> Vermeer, 7xxx -> Raphael, 9xxx -> Granite Ridge
                    if prefix == 5:
                        return RyzenSMU.Generation.VERMEER
                    if prefix == 7:
                        return RyzenSMU.Generation.RAPHAEL
                    if prefix == 9:
                        return RyzenSMU.Generation.GRANITE_RIDGE
        # nothing matched
        logger.debug("No known Ryzen model found in /proc/cpuinfo")
    except Exception as e:
        logger.debug("Failed to read /proc/cpuinfo: %s", e)
    return None

# ----------------------------------------------------------------------------
# Ryzen SMU driver interface
# ----------------------------------------------------------------------------
class RyzenSMU:
    """
    Interface to the ryzen_smu sysfs driver.
    Auto‑detects CPU generation and adapts SMU commands.

    !!! Not thread/process‑safe. The provided lock must be held.
    """

    FS_PATH = Path("/sys/kernel/ryzen_smu_drv/")
    VER_PATH = FS_PATH / "version"
    CODENAME_PATH = FS_PATH / "codename"
    SMU_ARGS = FS_PATH / "smu_args"
    MP1_CMD = FS_PATH / "mp1_smu_cmd"

    class Generation(Enum):
        VERMEER = "vermeer"               # Ryzen 5000
        GRANITE_RIDGE = "granite_ridge"   # Ryzen 9000
        RAPHAEL = "raphael"               # Ryzen 7000 (unsupported)
        UNSUPPORTED = "unsupported"

    # Codename numbers from the driver codename file
    CODENAME_MAP = {
        25: Generation.VERMEER,
        12: Generation.VERMEER,
        19: Generation.VERMEER,
        24: Generation.GRANITE_RIDGE,
        23: Generation.RAPHAEL,
        17: Generation.RAPHAEL,
    }

    # SMU opcodes
    V_GET_OFFSET    = 0x48
    V_SET_OFFSET    = 0x35
    V_RESET_ALL     = 0x36
    GR_SET_OFFSET_BASE = 0x50              # 0x50 + core index

    # Tuning via environment
    SMU_TIMEOUT = float(os.environ.get("RUV_SMU_TIMEOUT", "5.0"))
    SMU_RETRY_ATTEMPTS = max(1, int(os.environ.get("RUV_SMU_RETRY_ATTEMPTS", "3")))
    SMU_RETRY_DELAY = float(os.environ.get("RUV_SMU_RETRY_DELAY", "0.15"))

    MIN_OFFSET = -100
    MAX_OFFSET = 100

    def __init__(self):
        if not self.driver_loaded():
            raise RuntimeError("Ryzen SMU driver not loaded. Load with: sudo modprobe ryzen_smu")
        self.generation = self._detect_generation()
        # Now core_id_list holds APIC IDs (sorted) for linear core mapping
        self.core_id_list = get_physical_apic_ids_sorted()
        self.core_count = len(self.core_id_list)
        self.co_cache: Dict[int, int] = {}
        if self.generation == self.Generation.GRANITE_RIDGE:
            self._load_co_cache()
        logger.debug("RyzenSMU initialized. Gen: %s, cores: %d", self.generation.value, self.core_count)

    # ------------------------------------------------------------------
    # Sysfs helpers
    # ------------------------------------------------------------------
    @classmethod
    def driver_loaded(cls) -> bool:
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
    def _read_file192(cls, file: Path) -> Optional[Tuple[int, ...]]:
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

    # ------------------------------------------------------------------
    # SMU command execution
    # ------------------------------------------------------------------
    def smu_command(self, op: int, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0) -> Tuple[int, ...]:
        """Execute a raw SMU command. Blocks until completion or timeout."""
        # Wait for ready
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

        # Write arguments and opcode
        if not self._write_file192(self.SMU_ARGS, arg1, arg2, arg3, arg4, arg5, arg6):
            raise RuntimeError("Failed to write SMU arguments")
        if not self._write_file32(self.MP1_CMD, op):
            raise RuntimeError("Failed to write SMU command")

        # Wait for completion
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

    def _smu_command_with_retry(self, op: int, *args: int) -> Tuple[int, ...]:
        """SMU command with automatic retries on failure."""
        last_error: Optional[Exception] = None
        for attempt in range(1, self.SMU_RETRY_ATTEMPTS + 1):
            try:
                return self.smu_command(op, *args)
            except RuntimeError as e:
                last_error = e
                if attempt >= self.SMU_RETRY_ATTEMPTS:
                    break
                logger.warning(
                    "SMU command 0x%x failed (attempt %d/%d): %s; retrying in %.2fs",
                    op, attempt, self.SMU_RETRY_ATTEMPTS, e, self.SMU_RETRY_DELAY,
                )
                time.sleep(self.SMU_RETRY_DELAY)
        raise RuntimeError(
            f"SMU command failed after {self.SMU_RETRY_ATTEMPTS} attempts: {last_error}"
        )

    # ------------------------------------------------------------------
    # Per‑core offset operations
    # ------------------------------------------------------------------
    def get_core_offset(self, core_index: int) -> Optional[int]:
        """Read the current voltage offset (mV) for the given linear core index."""
        if core_index < 0 or core_index >= self.core_count:
            raise ValueError(f"Core index {core_index} out of range (0-{self.core_count-1})")

        if self.generation == self.Generation.VERMEER:
            apic_id = self.core_id_list[core_index]
            arg = ((apic_id & 8) << 5 | (apic_id & 7)) << 20
            try:
                result = self._smu_command_with_retry(self.V_GET_OFFSET, arg)
            except RuntimeError:
                return None
            value = result[0]
            if value > 2**31 - 1:
                value -= 2**32
            return value
        elif self.generation == self.Generation.GRANITE_RIDGE:
            # Write-only hardware; return whatever we have in cache
            return self.co_cache.get(core_index, 0)
        else:
            raise RuntimeError(f"Getting offsets is not supported on {self.generation.value}")

    def set_core_offset(self, core_index: int, offset: int) -> None:
        """Set the voltage offset for a single linear core index."""
        if not (self.MIN_OFFSET <= offset <= self.MAX_OFFSET):
            raise ValueError(f"Offset {offset} mV out of range [{self.MIN_OFFSET}, {self.MAX_OFFSET}]")
        if core_index < 0 or core_index >= self.core_count:
            raise ValueError(f"Core index {core_index} out of range (0-{self.core_count-1})")

        if self.generation == self.Generation.VERMEER:
            apic_id = self.core_id_list[core_index]
            # Save old offset for rollback
            old_offset = self.get_core_offset(core_index)
            arg = (((apic_id & 8) << 5 | (apic_id & 7)) << 20) | (offset & 0xFFFF)
            try:
                self._smu_command_with_retry(self.V_SET_OFFSET, arg)
                logger.debug("Set core %d (APIC %d) → %d mV", core_index, apic_id, offset)
            except Exception:
                # Try to restore previous value
                if old_offset is not None:
                    try:
                        rollback_arg = (((apic_id & 8) << 5 | (apic_id & 7)) << 20) | (old_offset & 0xFFFF)
                        self._smu_command_with_retry(self.V_SET_OFFSET, rollback_arg)
                        logger.warning("Rollback succeeded for core %d to %d mV", core_index, old_offset)
                    except Exception as e:
                        logger.error("Rollback failed for core %d: %s", core_index, e)
                raise
        elif self.generation == self.Generation.GRANITE_RIDGE:
            op = self.GR_SET_OFFSET_BASE + core_index
            encoded = offset & 0xFFFFFFFF if offset >= 0 else ((offset + 2**32) & 0xFFFFFFFF)
            try:
                self._smu_command_with_retry(op, encoded)
                self.co_cache[core_index] = offset
                self._save_co_cache()
                logger.debug("Set core %d → %d mV (cached)", core_index, offset)
            except Exception:
                raise
        else:
            raise RuntimeError(f"Setting offsets is not supported on {self.generation.value}")

    def reset_all_offsets(self) -> None:
        """Reset every core's offset to 0 mV."""
        if self.generation == self.Generation.VERMEER:
            self._smu_command_with_retry(self.V_RESET_ALL, 0)
        elif self.generation == self.Generation.GRANITE_RIDGE:
            for i in range(self.core_count):
                self.set_core_offset(i, 0)
        else:
            raise RuntimeError(f"Resetting offsets is not supported on {self.generation.value}")
        logger.debug("Reset all offsets")

    # ------------------------------------------------------------------
    # Granite Ridge cache (write‑only hardware)
    # ------------------------------------------------------------------
    def _load_co_cache(self) -> None:
        """Load the Curve Optimizer offset cache from disk."""
        if CO_CACHE_FILE.is_file():
            try:
                with open(CO_CACHE_FILE) as f:
                    data = json.load(f)
                for idx_str, off in data.items():
                    self.co_cache[int(idx_str)] = ensure_valid_offset(off, f"cache core {idx_str}")
            except Exception as e:
                logger.warning("Failed to load CO cache, starting fresh: %s", e)
                self.co_cache = {}
        else:
            self.co_cache = {}

    def _save_co_cache(self) -> None:
        """Persist the current CO cache to disk."""
        data = {str(i): self.co_cache.get(i, 0) for i in range(self.core_count)}
        write_json_atomic(CO_CACHE_FILE, data)

    # ------------------------------------------------------------------
    # Generation detection
    # ------------------------------------------------------------------
    def _detect_generation(self) -> Generation:
        """Detect the Ryzen generation from sysfs or fallback to /proc/cpuinfo."""
        if self.CODENAME_PATH.is_file():
            try:
                codenum = int(self.CODENAME_PATH.read_text().strip())
                gen = self.CODENAME_MAP.get(codenum)
                if gen is not None:
                    logger.info("Detected generation from codename %d: %s", codenum, gen.value)
                    return gen
                logger.debug("Codename %d not in mapping, trying fallback...", codenum)
            except (ValueError, OSError) as e:
                logger.debug("Failed to parse codename file: %s, trying fallback...", e)

        gen = detect_generation_from_cpuinfo()
        if gen is not None:
            logger.info("Detected generation from CPU model: %s", gen.value)
            return gen

        logger.warning("Could not determine CPU generation – treating as unsupported")
        return self.Generation.UNSUPPORTED

# ----------------------------------------------------------------------------
# Core detection (linear indices using APIC IDs)
# ----------------------------------------------------------------------------
def get_physical_apic_ids_sorted() -> List[int]:
    """
    Return a sorted list of APIC IDs of physical cores (one per core).
    Falls back to logical core count if sysfs is missing (assumes identity mapping).
    """
    cpu_path = Path("/sys/devices/system/cpu")
    core_apic = {}   # core_id -> APIC ID of first logical CPU seen
    for cpu_dir in sorted(cpu_path.glob("cpu[0-9]*")):
        try:
            core_id_file = cpu_dir / "topology" / "core_id"
            apic_id_file = cpu_dir / "topology" / "apic_id"
            if not core_id_file.exists() or not apic_id_file.exists():
                continue
            core_id = int(core_id_file.read_text().strip())
            # Take the APIC ID of the first logical processor for each physical core
            if core_id not in core_apic:
                apic_id = int(apic_id_file.read_text().strip())
                core_apic[core_id] = apic_id
        except (ValueError, OSError):
            pass
    if core_apic:
        # Return sorted by core_id for a stable linear ordering
        return [apic for core_id, apic in sorted(core_apic.items())]

    # Fallback: /proc/cpuinfo core count
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("cpu cores"):
                    cores = int(line.split(":")[1].strip())
                    # assume identity mapping 0..N-1
                    return list(range(cores))
    except Exception:
        pass

    # Last resort: half logical cores
    total_logical = os.cpu_count() or 8
    try:
        with open("/proc/cpuinfo") as f:
            ht_enabled = any(" ht " in line for line in f if line.startswith("flags"))
    except Exception:
        ht_enabled = False
    physical = total_logical // 2 if ht_enabled else total_logical
    logger.warning("Fallback core count: %d (identity APIC mapping)", physical)
    return list(range(physical))

def parse_core_range(spec: str, core_count: Optional[int] = None) -> List[int]:
    """
    Parse a core specification string like '0,2-5,7' into a sorted list
    of linear core indices, validated against the given `core_count`.
    """
    if core_count is None:
        core_count = len(get_physical_apic_ids_sorted())
    cores = set()
    for part in spec.replace(" ", "").split(","):
        if "-" in part:
            try:
                start, end = map(int, part.split("-"))
                if start > end:
                    raise ValueError
                cores.update(range(start, end + 1))
            except ValueError:
                raise ValueError(f"Invalid range: {part}")
        else:
            try:
                cores.add(int(part))
            except ValueError:
                raise ValueError(f"Invalid core number: {part}")
    invalid = cores - set(range(core_count))
    if invalid:
        raise ValueError(f"Core index(es) {sorted(invalid)} do not exist. Available: 0-{core_count-1}")
    return sorted(cores)

# ----------------------------------------------------------------------------
# Profile and offset validation
# ----------------------------------------------------------------------------
def validate_profile_name(name: str) -> bool:
    """Profile names may contain letters, digits, underscores, hyphens, and dots."""
    return bool(re.match(r'^[a-zA-Z0-9_.-]+$', name))

def ensure_valid_offset(value: Any, context: str = "offset") -> int:
    """Raise if `value` is not an integer in [MIN_OFFSET, MAX_OFFSET]."""
    if not isinstance(value, int):
        raise ValueError(f"{context}: offset must be an integer")
    if not (RyzenSMU.MIN_OFFSET <= value <= RyzenSMU.MAX_OFFSET):
        raise ValueError(
            f"{context}: offset {value} mV is outside allowed range "
            f"[{RyzenSMU.MIN_OFFSET}, {RyzenSMU.MAX_OFFSET}]"
        )
    return value

def validate_profile_data(data: Any) -> Dict[int, int]:
    """Validate a raw JSON‑decoded profile dict and return a clean {core_index: offset}."""
    if not isinstance(data, dict):
        raise ValueError("Invalid profile JSON: expected an object mapping core indices to offsets")
    validated: Dict[int, int] = {}
    for idx_str, raw_offset in data.items():
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            raise ValueError(f"{idx_str}: invalid core index")
        validated[idx] = ensure_valid_offset(raw_offset, f"core {idx}")
    return validated

def load_and_validate_profile_data(profile_path: Path) -> Dict[int, int]:
    """Load a profile JSON file and return validated data."""
    with open(profile_path) as f:
        data = json.load(f)
    return validate_profile_data(data)

# ----------------------------------------------------------------------------
# Atomic file writes
# ----------------------------------------------------------------------------
def write_json_atomic(path: Path, data: Any, mode: int = 0o644) -> None:
    """Atomically write JSON data to `path` using a temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

def write_text_atomic(path: Path, content: str, mode: int = 0o644) -> None:
    """Atomically write text content to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

# ----------------------------------------------------------------------------
# Profile operations
# ----------------------------------------------------------------------------
def save_current_offsets_as_profile(name: str) -> None:
    """Save the current live offsets to a named profile."""
    if not RyzenSMU.driver_loaded():
        raise RuntimeError("Ryzen SMU driver not loaded.")
    smu = RyzenSMU()
    offsets = {str(i): smu.get_core_offset(i) for i in range(smu.core_count)}
    write_json_atomic(PROFILES_DIR / f"{name}.json", offsets)
    logger.info("Profile '%s' saved.", name)

def apply_profile_file(profile_path: Path) -> None:
    """Apply a profile JSON file to the CPU."""
    if not RyzenSMU.driver_loaded():
        raise RuntimeError("Ryzen SMU driver not loaded.")
    smu = RyzenSMU()
    data = load_and_validate_profile_data(profile_path)
    failed = []
    for idx, offset in data.items():
        if idx >= smu.core_count:
            failed.append(f"Core {idx}: index out of range (max {smu.core_count-1})")
            continue
        try:
            smu.set_core_offset(idx, offset)
        except Exception as e:
            failed.append(f"Core {idx}: {e}")
    if failed:
        raise RuntimeError("Errors applying offsets:\n" + "\n".join(failed))
    logger.info("Profile '%s' applied.", profile_path.stem)

# ----------------------------------------------------------------------------
# CLI handlers (core operations)
# ----------------------------------------------------------------------------
def _set_cores(smu: RyzenSMU, cores: List[int], offset: int) -> None:
    """
    Set the same offset on a list of cores; attempts rollback on failure.
    Prints applied cores or raises.
    """
    # Save original offsets for rollback
    original = {}
    for idx in cores:
        try:
            orig = smu.get_core_offset(idx)
            original[idx] = orig
        except Exception as e:
            print(f"Warning: cannot read current offset for core {idx}: {e}", file=sys.stderr)
            original[idx] = None

    success = []
    try:
        for idx in cores:
            smu.set_core_offset(idx, offset)
            success.append(idx)
        for idx in success:
            print(f"Core {idx}: {offset} mV")
    except Exception as e:
        # Rollback any successfully set cores
        print(f"Error setting core {idx}: {e}", file=sys.stderr)
        print("Attempting rollback...", file=sys.stderr)
        rollback_fail = []
        for r_idx in success:
            if original[r_idx] is not None:
                try:
                    smu.set_core_offset(r_idx, original[r_idx])
                except Exception as re:
                    rollback_fail.append(r_idx)
        if rollback_fail:
            print(f"Rollback failed for cores: {rollback_fail}", file=sys.stderr)
        else:
            print("Rollback successful.", file=sys.stderr)
        raise RuntimeError(f"Failed to set offset on core {idx} ({e})")

def cli_status(args: argparse.Namespace) -> None:
    smu = RyzenSMU()
    if getattr(args, 'json', False):
        offsets = {str(i): smu.get_core_offset(i) for i in range(smu.core_count)}
        print(json.dumps(offsets))
    else:
        if smu.generation == RyzenSMU.Generation.GRANITE_RIDGE:
            print("Warning: Offsets for Granite Ridge are cached (write‑only hardware).")
        for i in range(smu.core_count):
            off = smu.get_core_offset(i)
            print(f"Core {i}: {off if off is not None else 'error'} mV")

def cli_get(args: argparse.Namespace) -> None:
    smu = RyzenSMU()
    try:
        cores = parse_core_range(args.core, smu.core_count)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    for idx in cores:
        off = smu.get_core_offset(idx)
        print(f"Core {idx}: {off if off is not None else 'error'} mV")

def cli_set(args: argparse.Namespace) -> None:
    smu = RyzenSMU()
    try:
        cores = parse_core_range(args.core, smu.core_count)
        _set_cores(smu, cores, args.offset)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

def cli_apply_list(args: argparse.Namespace) -> None:
    """Apply a fixed offset to multiple cores (CLI: apply-list)."""
    smu = RyzenSMU()
    try:
        cores = parse_core_range(args.cores, smu.core_count)
        _set_cores(smu, cores, args.offset)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

def cli_apply_profile(args: argparse.Namespace) -> None:
    name = args.name.strip()
    profile_path = PROFILES_DIR / f"{name}.json"
    if not profile_path.is_file():
        print(f"Error: Profile '{name}' not found", file=sys.stderr)
        sys.exit(1)
    try:
        apply_profile_file(profile_path)
        print(f"Profile '{name}' applied.")
        smu = RyzenSMU()
        for i in range(smu.core_count):
            print(f"Core {i}: {smu.get_core_offset(i)} mV")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

def cli_reset(args: argparse.Namespace) -> None:
    smu = RyzenSMU()
    smu.reset_all_offsets()
    print("All offsets reset to 0 mV")

# ----------------------------------------------------------------------------
# Profile management CLI
# ----------------------------------------------------------------------------
def cli_profile_list(args: argparse.Namespace) -> None:
    if PROFILES_DIR.exists():
        profiles = sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
        print("\n".join(profiles) if profiles else "No profiles found.")
    else:
        print("No profiles directory exists.")

def cli_profile_save(args: argparse.Namespace) -> None:
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
        RyzenSMU().reset_all_offsets()
        print(f"Profile '{name}' deleted and offsets reset.")
    else:
        print(f"Profile '{name}' deleted.")

def cli_profile_apply(args: argparse.Namespace) -> None:
    """Alias: profile apply → same as apply command."""
    args.name = args.profile_name
    cli_apply_profile(args)

def cli_profile_read(args: argparse.Namespace) -> None:
    name = args.name.strip()
    profile_path = PROFILES_DIR / f"{name}.json"
    if not profile_path.is_file():
        print(f"Error: Profile '{name}' not found.", file=sys.stderr)
        sys.exit(1)
    try:
        print(profile_path.read_text(), end="")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

def cli_profile_update(args: argparse.Namespace) -> None:
    name = args.name.strip()
    profile_path = PROFILES_DIR / f"{name}.json"
    if not profile_path.is_file():
        print(f"Profile '{name}' does not exist.", file=sys.stderr)
        sys.exit(1)
    smu = RyzenSMU()
    try:
        cores = parse_core_range(args.cores, smu.core_count)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        data = {str(idx): offset for idx, offset in load_and_validate_profile_data(profile_path).items()}
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    new_offset = ensure_valid_offset(args.offset, "offset")
    for idx in cores:
        data[str(idx)] = new_offset
    write_json_atomic(profile_path, data)
    print(f"Profile '{name}' updated for cores {cores}.")
    if args.apply:
        if not RyzenSMU.driver_loaded():
            print("Warning: Driver not loaded, cannot apply.", file=sys.stderr)
        else:
            try:
                _set_cores(smu, cores, new_offset)
            except RuntimeError as e:
                print(f"Error during apply: {e}", file=sys.stderr)
                sys.exit(1)
            print("Applied to CPU.")

# ----------------------------------------------------------------------------
# Boot service CLI (improved with driver dependency)
# ----------------------------------------------------------------------------
def cli_boot_enable(args: argparse.Namespace) -> None:
    name = args.name.strip()
    profile_path = PROFILES_DIR / f"{name}.json"
    if not profile_path.is_file():
        print(f"Error: Profile '{name}' does not exist.", file=sys.stderr)
        sys.exit(1)
    service = f"""[Unit]
Description=Apply Ryzen undervolt profile '{name}'
Wants=systemd-modules-load.service
After=multi-user.target systemd-modules-load.service
ConditionPathExists=/sys/kernel/ryzen_smu_drv/version

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 {INSTALLED_BIN_PATH} -- apply {name}
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
"""
    try:
        write_text_atomic(Path("/etc/systemd/system/ruv-boot.service"), service)
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
    try:
        subprocess.run(["systemctl", "disable", "ruv-boot.service"], check=True, capture_output=True)
        Path("/etc/systemd/system/ruv-boot.service").unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        print("Boot service disabled.")
    except PermissionError:
        print("Permission denied. Run with sudo.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

def cli_boot_status(args: argparse.Namespace) -> None:
    try:
        result = subprocess.run(["systemctl", "is-enabled", "ruv-boot.service"],
                                capture_output=True, text=True)
        enabled = result.stdout.strip()
        if enabled == "enabled":
            try:
                content = Path("/etc/systemd/system/ruv-boot.service").read_text()
                match = re.search(r"apply\s+(\S+)", content)
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

# ----------------------------------------------------------------------------
# CLI entry point (unchanged logic, but relies on fixed functions)
# ----------------------------------------------------------------------------
def cli_mode(cli_args: List[str]) -> None:
    acquire_lock()

    parser = argparse.ArgumentParser(
        prog="ruv-gui",
        description="Ryzen SMU voltage control – CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Core specifications use linear indices:
  single core   : 0
  comma list    : 0,2,4
  range         : 0-7
  combination   : 0,2-5,7

For negative offsets with 'apply-list', use '--' before the offset:
  sudo ruv-gui apply-list 0,1 -- -30
"""
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- core commands ---
    status_parser = subparsers.add_parser("status", help="Show current core voltage offsets")
    status_parser.add_argument("--json", action="store_true", help="Output in JSON format")

    get_parser = subparsers.add_parser("get", help="Get offset for one or more cores")
    get_parser.add_argument("core", help="Core specification (e.g., 0, 0-3, 0,2,4)")

    set_parser = subparsers.add_parser("set", help="Set offset for one or more cores")
    set_parser.add_argument("core", help="Core specification")
    set_parser.add_argument("offset", type=int, help="Offset in mV (use -- -30 for negative)")

    apply_list_parser = subparsers.add_parser("apply-list", help="Apply same offset to multiple cores without saving")
    apply_list_parser.add_argument("cores", help="Core specification")
    apply_list_parser.add_argument("offset", type=int, help="Offset in mV")

    apply_parser = subparsers.add_parser("apply", help="Apply a saved profile by name")
    apply_parser.add_argument("name", help="Profile name (without .json)")

    subparsers.add_parser("reset", help="Reset all offsets to 0 mV")

    # --- profile commands ---
    profile_parser = subparsers.add_parser("profile", help="Manage profiles")
    profile_sub = profile_parser.add_subparsers(dest="profile_cmd", required=True)
    profile_sub.add_parser("list", help="List saved profiles")
    profile_save = profile_sub.add_parser("save", help="Save current offsets as a profile")
    profile_save.add_argument("name", help="Profile name (alphanumeric, underscore, hyphen, dot)")
    profile_delete = profile_sub.add_parser("delete", help="Delete a profile")
    profile_delete.add_argument("name", help="Profile name")
    profile_apply = profile_sub.add_parser("apply", help="Apply a saved profile (alias for apply)")
    profile_apply.add_argument("profile_name", help="Profile name")
    profile_read = profile_sub.add_parser("read", help="Display the JSON content of a saved profile")
    profile_read.add_argument("name", help="Profile name")
    profile_update = profile_sub.add_parser("update", help="Update specific cores in a profile")
    profile_update.add_argument("name", help="Profile name")
    profile_update.add_argument("--cores", required=True, help="Core specification")
    profile_update.add_argument("--offset", type=int, required=True, help="New offset in mV")
    profile_update.add_argument("--apply", action="store_true", help="Apply updated offsets to CPU immediately")

    # --- boot commands ---
    boot_parser = subparsers.add_parser("boot", help="Manage boot-time application")
    boot_sub = boot_parser.add_subparsers(dest="boot_cmd", required=True)
    boot_enable = boot_sub.add_parser("enable", help="Enable automatic profile application at boot")
    boot_enable.add_argument("name", help="Profile name to apply")
    boot_sub.add_parser("disable", help="Disable boot-time application")
    boot_sub.add_parser("status", help="Show boot service status")

    # --- internal privileged commands (hidden) ---
    subparsers.add_parser("list", help=argparse.SUPPRESS)          # alias for status
    apply_file_parser = subparsers.add_parser("apply-file", help=argparse.SUPPRESS)
    apply_file_parser.add_argument("file", type=str)
    read_profile_parser = subparsers.add_parser("read-profile", help=argparse.SUPPRESS)
    read_profile_parser.add_argument("file", type=str)
    write_profile_parser = subparsers.add_parser("write-profile", help=argparse.SUPPRESS)
    write_profile_parser.add_argument("file", type=str)
    delete_profile_file_parser = subparsers.add_parser("delete-profile-file", help=argparse.SUPPRESS)
    delete_profile_file_parser.add_argument("file", type=str)
    delete_and_reset_parser = subparsers.add_parser("delete-profile-and-reset", help=argparse.SUPPRESS)
    delete_and_reset_parser.add_argument("file", type=str)
    save_profile_combined = subparsers.add_parser("save-profile-combined", help=argparse.SUPPRESS)
    save_profile_combined.add_argument("name", type=str)
    install_boot_service_parser = subparsers.add_parser("install-boot-service", help=argparse.SUPPRESS)
    install_boot_service_parser.add_argument("service_path", type=str)
    subparsers.add_parser("remove-boot-service", help=argparse.SUPPRESS)

    args = parser.parse_args(cli_args)

    # ------------------------------------------------------------------
    # Internal privileged command implementations
    # ------------------------------------------------------------------
    def _resolve_profile_path(raw_path: str) -> Path:
        path = Path(raw_path).resolve()
        try:
            path.relative_to(PROFILES_DIR)
        except ValueError:
            print(f"Error: Profile file must be inside {PROFILES_DIR}", file=sys.stderr)
            sys.exit(1)
        return path

    if args.command == "read-profile":
        path = _resolve_profile_path(args.file)
        try:
            print(path.read_text(), end="")
        except Exception as e:
            print(f"Error reading profile: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "write-profile":
        path = _resolve_profile_path(args.file)
        raw = sys.stdin.read()
        try:
            parsed = json.loads(raw)
            validate_profile_data(parsed)
            write_text_atomic(path, raw)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "delete-profile-file":
        path = _resolve_profile_path(args.file)
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "delete-profile-and-reset":
        path = _resolve_profile_path(args.file)
        try:
            RyzenSMU().reset_all_offsets()
            path.unlink(missing_ok=True)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
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
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "install-boot-service":
        service_path = Path(args.service_path)
        try:
            write_text_atomic(service_path, sys.stdin.read())
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "enable", "ruv-boot.service"], check=True)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "remove-boot-service":
        try:
            subprocess.run(["systemctl", "disable", "ruv-boot.service"],
                           check=True, capture_output=True)
            Path("/etc/systemd/system/ruv-boot.service").unlink(missing_ok=True)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # ------------------------------------------------------------------
    # Public aliases
    # ------------------------------------------------------------------
    if args.command == "list":
        args.command = "status"
    elif args.command == "apply-file":
        args.command = "apply"
        args.name = Path(args.file).stem

    # All following commands require the driver
    if not RyzenSMU.driver_loaded():
        print("Error: Ryzen SMU driver not loaded.", file=sys.stderr)
        sys.exit(1)

    handlers = {
        "status": cli_status,
        "get": cli_get,
        "set": cli_set,
        "apply-list": cli_apply_list,
        "apply": cli_apply_profile,
        "reset": cli_reset,
    }
    if args.command in handlers:
        handlers[args.command](args)
    elif args.command == "profile":
        profile_handlers = {
            "list": cli_profile_list,
            "save": cli_profile_save,
            "delete": cli_profile_delete,
            "apply": cli_profile_apply,
            "read": cli_profile_read,
            "update": cli_profile_update,
        }
        if args.profile_cmd in profile_handlers:
            profile_handlers[args.profile_cmd](args)
        else:
            parser.print_help()
    elif args.command == "boot":
        boot_handlers = {
            "enable": cli_boot_enable,
            "disable": cli_boot_disable,
            "status": cli_boot_status,
        }
        if args.boot_cmd in boot_handlers:
            boot_handlers[args.boot_cmd](args)
        else:
            parser.print_help()
    else:
        parser.print_help()

# ----------------------------------------------------------------------------
# GUI components (only if PyQt6 is available)
# ----------------------------------------------------------------------------
if GUI_AVAILABLE:

    class WorkerThread(QThread):
        finished = pyqtSignal(str)
        error = pyqtSignal(str)

        def __init__(self, args: List[str], input_text: Optional[str] = None):
            super().__init__()
            self.args = args
            self.input_text = input_text

        def run(self) -> None:
            try:
                output = PrivilegedRunner.run(self.args, self.input_text)
                self.finished.emit(output)
            except Exception as e:
                self.error.emit(str(e))

    class CoreSelectionList(QListWidget):
        def __init__(self, core_count: int, parent=None):
            super().__init__(parent)
            self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            for i in range(core_count):
                item = QListWidgetItem(f"Core {i}")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                self.addItem(item)
                item.setData(Qt.ItemDataRole.UserRole, i)

        def get_selected_cores(self) -> List[int]:
            return [self.item(idx).data(Qt.ItemDataRole.UserRole)
                    for idx in range(self.count())
                    if self.item(idx).checkState() == Qt.CheckState.Checked]

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Ryzen Undervolt Tool")
            self.resize(800, 600)
            self._set_window_icon()

            # Quick SMU probe to get generation & core count (driver must be loaded)
            try:
                smu = RyzenSMU()
                self.generation = smu.generation
                self.core_count = smu.core_count
            except Exception as e:
                QMessageBox.critical(None, "Error", f"Failed to initialise SMU:\n{e}")
                sys.exit(1)

            self.workers: List[QThread] = []
            self._busy = False
            self._setup_ui()
            self.refresh_profile_list()
            self.list_offsets()

        # ------------------------------------------------------------------
        # UI helpers
        # ------------------------------------------------------------------
        def _set_window_icon(self) -> None:
            icon = QIcon.fromTheme("ruv-gui")
            if icon.isNull() and os.path.exists(ICON_FALLBACK_PATH):
                icon = QIcon(ICON_FALLBACK_PATH)
            if not icon.isNull():
                self.setWindowIcon(icon)

        def _setup_ui(self) -> None:
            central = QWidget()
            self.setCentralWidget(central)
            main_layout = QVBoxLayout()
            main_layout.setContentsMargins(5, 5, 5, 5)
            main_layout.setSpacing(5)
            central.setLayout(main_layout)

            splitter = QSplitter(Qt.Orientation.Horizontal)
            main_layout.addWidget(splitter, 1)

            # Left: core list
            left_widget = QWidget()
            left_layout = QVBoxLayout(left_widget)
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.addWidget(QLabel("Select cores to undervolt:"))
            self.core_list = CoreSelectionList(self.core_count)
            left_layout.addWidget(self.core_list)
            splitter.addWidget(left_widget)

            # Right: controls
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

            # Profile management
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

            # Boot service
            boot_layout = QHBoxLayout()
            boot_layout.setSpacing(10)
            self.btn_set_boot = QPushButton("Set as Boot Profile")
            self.btn_remove_boot = QPushButton("Remove Boot Service")
            boot_layout.addWidget(self.btn_set_boot)
            boot_layout.addWidget(self.btn_remove_boot)
            boot_layout.addStretch()
            main_layout.addLayout(boot_layout)

            # Output log
            self.output = QTextEdit()
            self.output.setReadOnly(True)
            self.output.setMaximumHeight(150)
            main_layout.addWidget(self.output)

            # Connections
            self.btn_list.clicked.connect(self.list_offsets)
            self.btn_reset.clicked.connect(self.reset_offsets)
            self.btn_apply.clicked.connect(self.apply_offset)
            self.btn_save_profile.clicked.connect(self.save_current_as_profile)
            self.btn_delete_profile.clicked.connect(self.delete_profile)
            self.btn_apply_profile.clicked.connect(self.apply_profile)
            self.btn_update_profile.clicked.connect(self.update_profile)
            self.btn_set_boot.clicked.connect(self.set_as_boot_profile)
            self.btn_remove_boot.clicked.connect(self.remove_boot_service)

        def _set_busy(self, busy: bool) -> None:
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

        def _worker_cleanup(self, worker: QThread) -> None:
            if worker in self.workers:
                self.workers.remove(worker)
            if not self.workers:
                self._set_busy(False)

        def _run_privileged_async(self, args: List[str], on_finish, on_error=None,
                                  input_text: Optional[str] = None) -> None:
            if not self.workers:
                self._set_busy(True)
            worker = WorkerThread(args, input_text)
            def handle_finish(output: str) -> None:
                try:
                    on_finish(output)
                except Exception as e:
                    logger.exception("Error in finished callback")
                    self.output.setText(f"Error in callback: {e}")
                finally:
                    self._worker_cleanup(worker)
            def handle_error(err: str) -> None:
                try:
                    if on_error:
                        on_error(err)
                    else:
                        self.output.setText(f"Error: {err}")
                except Exception as e:
                    logger.exception("Error in error callback")
                    self.output.setText(f"Error in error handler: {e}")
                finally:
                    self._worker_cleanup(worker)
            worker.finished.connect(handle_finish)
            worker.error.connect(handle_error)
            self.workers.append(worker)
            worker.start()

        # ------------------------------------------------------------------
        # GUI actions
        # ------------------------------------------------------------------
        def list_offsets(self) -> None:
            def on_finish(output: str) -> None:
                self.output.setText(output)
            self._run_privileged_async(["list"], on_finish)

        def reset_offsets(self) -> None:
            def on_finish(output: str) -> None:
                self.output.setText(output)
                self.offset_spin.setValue(0)
            self._run_privileged_async(["reset"], on_finish)

        def apply_offset(self) -> None:
            selected = self.core_list.get_selected_cores()
            if not selected:
                self.output.setText("No cores selected.")
                return
            offset = self.offset_spin.value()
            args = ["apply-list", ",".join(map(str, selected)), str(offset)]
            def on_finish(output: str) -> None:
                self.output.setText(output)
            self._run_privileged_async(args, on_finish)

        def refresh_profile_list(self) -> None:
            self.profile_combo.clear()
            if PROFILES_DIR.exists():
                for f in PROFILES_DIR.glob("*.json"):
                    self.profile_combo.addItem(f.stem)

        def save_current_as_profile(self) -> None:
            name, ok = QInputDialog.getText(self, "Save Profile", "Profile name:")
            if not ok or not name.strip():
                return
            name = name.strip()
            if not validate_profile_name(name):
                self.output.setText("Invalid profile name.")
                return
            def on_finish(output: str) -> None:
                self.output.setText(output)
                self.refresh_profile_list()
                idx = self.profile_combo.findText(name)
                if idx >= 0:
                    self.profile_combo.setCurrentIndex(idx)
            self._run_privileged_async(["save-profile-combined", name], on_finish)

        def delete_profile(self) -> None:
            name = self.profile_combo.currentText()
            if not name:
                return
            reply = QMessageBox.question(self, "Delete Profile",
                                         f"Delete profile '{name}' and reset offsets?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            json_path = PROFILES_DIR / f"{name}.json"
            def on_done(_output: str) -> None:
                self.output.setText(f"Profile '{name}' deleted and offsets reset.")
                self.refresh_profile_list()
            self._run_privileged_async(["delete-profile-and-reset", str(json_path)], on_done)

        def apply_profile(self) -> None:
            name = self.profile_combo.currentText()
            if not name:
                self.output.setText("No profile selected.")
                return
            json_path = PROFILES_DIR / f"{name}.json"
            if not json_path.exists():
                self.output.setText("Profile file not found.")
                return
            def on_finish(output: str) -> None:
                self.output.setText(output)
            self._run_privileged_async(["apply-file", str(json_path)], on_finish)

        def update_profile(self) -> None:
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

            def on_read_profile(raw_json: str) -> None:
                try:
                    data = {str(idx): offset for idx, offset in validate_profile_data(json.loads(raw_json)).items()}
                    for core in selected:
                        data[str(core)] = new_offset
                    json_text = json.dumps(data, indent=2)

                    def on_write_done(_msg: str) -> None:
                        reply = QMessageBox.question(
                            self, "Apply Updated Profile",
                            f"Profile '{name}' updated. Apply updated cores to CPU now?",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.Yes
                        )
                        if reply == QMessageBox.StandardButton.Yes:
                            def on_apply_done(output: str) -> None:
                                self.output.setText(f"Profile updated and applied to selected cores.\n{output}")
                                # Do not reset spinbox; user may want to keep the value
                            apply_args = ["apply-list", ",".join(map(str, selected)), str(new_offset)]
                            self._run_privileged_async(apply_args, on_apply_done)
                        else:
                            self.output.setText(f"Profile '{name}' updated (not applied).")

                    self._run_privileged_async(["write-profile", str(json_path)], on_write_done, input_text=json_text)

                except Exception as e:
                    self.output.setText(f"Error: {e}")

            self._run_privileged_async(["read-profile", str(json_path)], on_read_profile)

        def set_as_boot_profile(self) -> None:
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
Wants=systemd-modules-load.service
After=multi-user.target systemd-modules-load.service
ConditionPathExists=/sys/kernel/ryzen_smu_drv/version

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 {INSTALLED_BIN_PATH} -- apply {name}
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
"""
            def on_done(_msg: str) -> None:
                self.output.setText(f"Boot service installed with profile '{name}'.")
            self._run_privileged_async(
                ["install-boot-service", "/etc/systemd/system/ruv-boot.service"],
                on_done, input_text=service_content
            )

        def remove_boot_service(self) -> None:
            reply = QMessageBox.question(self, "Remove Boot Service",
                                         "Remove the boot service?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            def on_done(_msg: str) -> None:
                self.output.setText("Boot service removed.")
            self._run_privileged_async(["remove-boot-service"], on_done)

# ----------------------------------------------------------------------------
# Main guard
# ----------------------------------------------------------------------------
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
        if not GUI_AVAILABLE:
            print("PyQt6 is required for the GUI.", file=sys.stderr)
            sys.exit(1)
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
