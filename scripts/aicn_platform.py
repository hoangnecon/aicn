#!/usr/bin/env python3
"""
aicn_platform.py - Cross-platform runtime, hardware, locking, and FFmpeg abstraction for AICN.

Stdlib-only module supporting macOS (Apple Silicon / Metal / VideoToolbox) and
Windows 10/11 + Linux (NVIDIA CUDA / NVENC / CPU fallback).
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

# ----------------------------------------------------------------- Canonical v3.0.0 pipeline versions
ASR_PIPELINE_VERSION = "asr-4.0"
WAVEFORM_CACHE_VERSION = "wf-4.0"
STEMS_VERSION = "aicn-audio-1.0"
AUDIO_VERSION = "aicn-audio-3.0"
TTS_ENGINE_TAG = "vieneu-v3-turbo-48k"
RB_STRETCH_VERSION = "rb3-f-3.0"
FF_RENDERER_VERSION = "v3-dub-wm-3.0"
SLOT_UNIT_MB = 512

# ----------------------------------------------------------------- OS detection
def is_windows() -> bool:
    """Return True when running on Windows."""
    return sys.platform == "win32" or platform.system() == "Windows"


def is_macos() -> bool:
    """Return True when running on macOS."""
    return sys.platform == "darwin" or platform.system() == "Darwin"


def is_linux() -> bool:
    """Return True when running on Linux."""
    return sys.platform.startswith("linux")


# ----------------------------------------------------------------- Hardware info
def get_cpu_brand() -> str:
    """Return human-readable CPU brand string across macOS, Windows, and Linux."""
    if is_macos():
        try:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if out:
                return out
        except Exception:
            pass
    elif is_windows():
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            )
            val, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            winreg.CloseKey(key)
            if val and str(val).strip():
                return str(val).strip()
        except Exception:
            pass
    elif is_linux():
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
    return platform.processor() or platform.machine() or "unknown"


def get_ram_gb() -> float:
    """Return total physical system RAM in GiB (rounded to 1 decimal)."""
    if is_macos():
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if out:
                return round(int(out) / (1024 ** 3), 1)
        except Exception:
            pass
    elif is_windows():
        try:
            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return round(stat.ullTotalPhys / (1024 ** 3), 1)
        except Exception:
            pass
    elif is_linux():
        try:
            with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024 ** 2), 1)
        except Exception:
            pass
    return 0.0


def _probe_python_cuda(py: str) -> dict[str, Any]:
    """Query CUDA availability and GPU details using the target Python interpreter."""
    code = (
        "import json\n"
        "info = {'cuda': False, 'name': None, 'vram_gb': None, 'ct2_cuda': False}\n"
        "try:\n"
        "    import torch\n"
        "    info['cuda'] = bool(torch.cuda.is_available())\n"
        "    if info['cuda']:\n"
        "        props = torch.cuda.get_device_properties(0)\n"
        "        info['name'] = props.name\n"
        "        info['vram_gb'] = round(props.total_memory / (1024 ** 3), 1)\n"
        "except Exception:\n"
        "    pass\n"
        "try:\n"
        "    import ctranslate2\n"
        "    info['ct2_cuda'] = ctranslate2.get_cuda_device_count() > 0\n"
        "except Exception:\n"
        "    pass\n"
        "print(json.dumps(info))\n"
    )
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        pass
    return {"cuda": False, "name": None, "vram_gb": None, "ct2_cuda": False}


def _probe_nvidia_smi() -> dict[str, Any]:
    """Fallback GPU query via nvidia-smi when torch is unavailable."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return {"cuda": False, "name": None, "vram_gb": None}
    try:
        r = subprocess.run(
            [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            line = r.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            name = parts[0]
            vram_gb = round(float(parts[1]) / 1024.0, 1) if len(parts) > 1 and parts[1].isdigit() else None
            return {"cuda": True, "name": name, "vram_gb": vram_gb}
    except Exception:
        pass
    return {"cuda": False, "name": None, "vram_gb": None}


def get_gpu_info(py: str | None = None) -> dict[str, Any]:
    """Return normalized GPU backend info: {'backend': 'metal'|'cuda'|'cpu', ...}."""
    if is_macos() and platform.machine() in ("arm64", "aarch64"):
        return {
            "backend": "metal",
            "name": get_cpu_brand(),
            "vram_gb": get_ram_gb(),
            "cuda": False,
            "ct2_cuda": False,
        }
    target_py = py or find_python()
    cuda_info = _probe_python_cuda(target_py)
    if not cuda_info.get("cuda"):
        smi = _probe_nvidia_smi()
        if smi.get("cuda"):
            cuda_info["cuda"] = True
            cuda_info["name"] = cuda_info.get("name") or smi.get("name")
            cuda_info["vram_gb"] = cuda_info.get("vram_gb") or smi.get("vram_gb")
    if cuda_info.get("cuda"):
        return {
            "backend": "cuda",
            "name": cuda_info.get("name") or "NVIDIA CUDA GPU",
            "vram_gb": cuda_info.get("vram_gb"),
            "cuda": True,
            "ct2_cuda": bool(cuda_info.get("ct2_cuda")),
        }
    return {
        "backend": "cpu",
        "name": get_cpu_brand(),
        "vram_gb": None,
        "cuda": False,
        "ct2_cuda": False,
    }


# ----------------------------------------------------------------- Tool & Python discovery
def _user_env_var(name: str) -> str:
    """Read an environment variable from process env, falling back to Windows HKCU User env."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    if is_windows():
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                v, _ = winreg.QueryValueEx(k, name)
                if v:
                    return str(v).strip()
        except Exception:
            pass
    return ""


def _candidate_tool_paths(name: str) -> list[Path]:
    """Return platform-specific candidate executable paths for a CLI tool."""
    home = Path.home()
    cands: list[Path] = []
    if is_windows():
        exe = name if name.lower().endswith(".exe") else f"{name}.exe"
        cands.extend([
            home / ".local" / "bin" / exe,
            home / ".local" / "node" / exe,
            Path(r"C:\Program Files\nodejs") / exe,
            Path(r"C:\Program Files\Git\cmd") / exe,
            Path(r"C:\ffmpeg\bin") / exe,
        ])
    elif is_macos():
        cands.extend([
            Path("/opt/homebrew/bin") / name,
            Path("/usr/local/bin") / name,
            home / ".local" / "bin" / name,
        ])
    else:
        cands.extend([
            home / ".local" / "bin" / name,
            Path("/usr/local/bin") / name,
            Path("/usr/bin") / name,
        ])
    return cands


@lru_cache(maxsize=32)
def find_tool(name: str) -> str:
    """Resolve an external binary path cleanly across macOS, Windows, and Linux."""
    env_override = os.environ.get(f"AICN_{name.upper()}", "").strip()
    if env_override and Path(env_override).exists():
        return env_override

    found = shutil.which(name)
    if found:
        return found

    for cand in _candidate_tool_paths(name):
        if cand.exists():
            return str(cand)

    if name == "ffmpeg":
        try:
            import imageio_ffmpeg
            ff = imageio_ffmpeg.get_ffmpeg_exe()
            if ff and Path(ff).exists():
                return ff
        except Exception:
            pass

    return name


def tool_exists(name: str) -> bool:
    """Return True if a CLI tool resolves to an executable on PATH or known locations."""
    resolved = find_tool(name)
    return bool(shutil.which(resolved) or (os.path.sep in resolved and Path(resolved).exists()) or Path(resolved).is_file())


def find_python() -> str:
    """Resolve the VieNeu / ML Python interpreter (AICN_PY -> platform venv -> sys.executable)."""
    env_py = _user_env_var("AICN_PY")
    if env_py and Path(env_py).exists():
        return env_py

    home = Path.home()
    if is_windows():
        cands = [
            Path(r"C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe"),
            home / "Tools" / "VieNeu-TTS" / ".venv" / "Scripts" / "python.exe",
        ]
    else:
        cands = [
            Path("/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python"),
            home / "Tools" / "VieNeu-TTS" / ".venv" / "bin" / "python",
        ]
    for c in cands:
        if c.exists():
            return str(c)
    return sys.executable


def find_dy_python(project_root: str | Path | None = None) -> str:
    """Resolve the Douyin scraper venv python if present, else fall back to find_python()."""
    env_py = _user_env_var("AICN_DY_PY")
    if env_py and Path(env_py).exists():
        return env_py
    if project_root:
        root = Path(project_root)
        rel = Path("tools/.venv-dy/Scripts/python.exe") if is_windows() else Path("tools/.venv-dy/bin/python")
        cand = root / rel
        if cand.exists():
            return str(cand)
    return find_python()


def find_chrome() -> str | None:
    """Locate Chrome/Edge executable for headless cookie minting if available."""
    env_chrome = os.environ.get("AICN_CHROME", "").strip()
    if env_chrome and Path(env_chrome).exists():
        return env_chrome
    if is_macos():
        c = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        return str(c) if c.exists() else None
    if is_windows():
        local_app = Path(os.environ.get("LOCALAPPDATA", ""))
        cands = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            local_app / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
        for c in cands:
            if c.exists():
                return str(c)
        return None
    for name in ("google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


# ----------------------------------------------------------------- Cross-platform file locking, VRAM pool & global SQLite-WAL cache
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCK_DEPTH = threading.local()

_POOL_STATE_LOCK = threading.Lock()
_PROCESS_HELD_SLOTS: dict[str, set[int]] = {}
_LOCAL_GPU_HELD = threading.local()

_GPU_INFO_CACHE_LOCK = threading.Lock()
_GPU_INFO_CACHE: dict[str, dict[str, Any]] = {}
_VRAM_FREE_CACHE_LOCK = threading.Lock()
_VRAM_FREE_CACHE: tuple[float, int | None] = (0.0, None)


def _get_thread_lock(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lk = _THREAD_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _THREAD_LOCKS[key] = lk
        return lk


def find_project_root(start_path: str | Path | None = None) -> Path | None:
    """Walk up from start_path (or cwd) to find the AICN project root containing config/machine.json or workspace/."""
    cur = Path(os.path.abspath(str(start_path or os.getcwd())))
    if cur.is_file():
        cur = cur.parent
    for p in [cur, *cur.parents]:
        if (p / "config" / "machine.json").is_file() or (p / "workspace").is_dir():
            return p
        if p.name == "workspace" and p.parent.exists():
            return p.parent
    return None


def global_cache_dir(hint_path: str | Path | None = None) -> str:
    """Return persistent cross-series cache root (<project_root>/.cache or AICN_CACHE_DIR or ~/.cache/aicn).
    Survives pruning individual workspace/<series>/ folders."""
    env_dir = os.environ.get("AICN_CACHE_DIR", "").strip()
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return os.path.abspath(env_dir)
    proj = find_project_root(hint_path)
    base = (proj / ".cache") if proj is not None else (Path.home() / ".cache" / "aicn")
    os.makedirs(base, exist_ok=True)
    return str(base)


def global_cache_path(namespace: str, key: str, ext: str = "", hint_path: str | Path | None = None) -> str:
    """Return <global_cache_dir>/<namespace>/<key><ext>, ensuring the namespace directory exists."""
    base = os.path.join(global_cache_dir(hint_path), namespace)
    os.makedirs(base, exist_ok=True)
    suffix = f".{ext}" if (ext and not ext.startswith(".")) else ext
    return os.path.join(base, f"{key}{suffix}")


def atomic_write_json(path: str | Path, obj: Any, indent: int = 2) -> None:
    """Atomically write JSON via a PID+thread unique temp file with Windows replace retry."""
    target = os.path.abspath(str(path))
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
        for attempt in range(10):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def hash_obj(obj: Any) -> str:
    """Canonical SHA-256 digest of a JSON-serializable object (key-order independent)."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ----------------------------------------------------------------- Single-source v3.0.0 cache key builders
def stage1_asr_key(
    src_sha: str,
    voc_sha: str,
    model: str,
    source_lang: str,
    asr_version: str = ASR_PIPELINE_VERSION,
) -> str:
    """Canonical content-addressed Stage 1 ASR cache key (zero path dependence)."""
    return hash_obj({
        "v": asr_version,
        "src": src_sha,
        "voc": voc_sha,
        "model": model,
        "lang": source_lang,
    })[:24]


def stems_key(
    src_sha: str,
    demucs_model: str = "htdemucs",
    stems_version: str = STEMS_VERSION,
) -> str:
    """Canonical content-addressed Demucs stem separation cache key."""
    return hash_obj({
        "v": stems_version,
        "src": src_sha,
        "model": demucs_model,
    })[:24]


def tts_sent_key(
    text: str,
    voice: str,
    engine_tag: str = TTS_ENGINE_TAG,
    temperature: float = 0.8,
    top_k: int = 25,
) -> str:
    """Canonical content-addressed VieNeu sentence cache key (compatible with existing .cache/tts_sent)."""
    return hashlib.sha256(
        json.dumps(
            {"t": text, "v": voice, "e": engine_tag, "temp": temperature, "k": top_k},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def rb_stretch_key(
    raw_wav_sha: str,
    tempo: float,
    rb_version: str = RB_STRETCH_VERSION,
) -> str:
    """Canonical content-addressed rubberband time-stretch cache key."""
    return hash_obj({
        "v": rb_version,
        "wav": raw_wav_sha,
        "tempo": round(float(tempo), 3),
    })[:24]


def wm_overlay_key(
    width: int,
    height: int,
    tl_sha: str,
    tr_sha: str,
    scale: float,
    pos: list[int] | tuple[int, ...],
) -> str:
    """Canonical content-addressed pre-composited dual-corner watermark strip key."""
    return hash_obj({
        "w": int(width),
        "h": int(height),
        "tl": str(tl_sha)[:16],
        "tr": str(tr_sha)[:16],
        "scale": round(float(scale), 3),
        "pos": [int(x) for x in pos],
    })[:24]


def render_key(
    src_sha: str,
    audio_sha: str | None,
    wm_key: str | None,
    enc: dict[str, Any],
    renderer_version: str = FF_RENDERER_VERSION,
) -> str:
    """Canonical content-addressed Stage 4 render cache key."""
    return hash_obj({
        "v": renderer_version,
        "src": src_sha,
        "audio": audio_sha,
        "wm": wm_key,
        "enc": enc,
    })


def machine_lock_dir() -> str:
    """Return machine-wide lock directory (~/.cache/aicn/locks or AICN_LOCK_DIR) shared across all series/agents."""
    env_dir = os.environ.get("AICN_LOCK_DIR", "").strip()
    base = Path(env_dir) if env_dir else (Path.home() / ".cache" / "aicn" / "locks")
    os.makedirs(base, exist_ok=True)
    return str(base)


def _env_held_locks() -> set[str]:
    raw = os.environ.get("AICN_HELD_LOCKS", "")
    if not raw:
        return set()
    return {os.path.normcase(os.path.abspath(p)) for p in raw.split(os.pathsep) if p.strip()}


def _env_held_gpu_leases() -> list[dict[str, Any]]:
    raw = os.environ.get("AICN_HELD_GPU_SLOTS", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and isinstance(d.get("slots"), list)]
    except Exception:
        pass
    return []


def child_env_with_held_locks(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a subprocess env propagating file locks AND GPU slot leases currently held by THIS thread
    so child scripts inherit ownership without self-deadlocking."""
    env = dict(os.environ if base_env is None else base_env)
    held = _env_held_locks()
    depths = getattr(_LOCAL_LOCK_DEPTH, "depths", None) or {}
    for k, v in depths.items():
        if v > 0:
            held.add(k)
    if held:
        env["AICN_HELD_LOCKS"] = os.pathsep.join(sorted(held))

    gpu_leases = list(_env_held_gpu_leases())
    local_leases = getattr(_LOCAL_GPU_HELD, "leases", None) or []
    for lease in local_leases:
        gpu_leases.append(dict(lease))
    if gpu_leases:
        env["AICN_HELD_GPU_SLOTS"] = json.dumps(gpu_leases, separators=(",", ":"))
    return env


@contextlib.contextmanager
def file_lock(path: str | Path, reentrant: bool = True, label: str = "") -> Iterator[None]:
    """Cross-platform advisory + process file lock using fcntl (POSIX) or msvcrt (Windows).
    Re-entrant within the same thread AND across child subprocesses spawned with
    `child_env_with_held_locks()` when `reentrant=True`."""
    lp = os.path.abspath(str(path))
    norm_key = os.path.normcase(lp)
    depths = getattr(_LOCAL_LOCK_DEPTH, "depths", None)
    if depths is None:
        depths = {}
        _LOCAL_LOCK_DEPTH.depths = depths
    if reentrant and (depths.get(norm_key, 0) > 0 or norm_key in _env_held_locks()):
        depths[norm_key] = depths.get(norm_key, 0) + 1
        try:
            yield
        finally:
            depths[norm_key] = max(0, depths.get(norm_key, 1) - 1)
        return

    os.makedirs(os.path.dirname(lp), exist_ok=True)
    tlk = _get_thread_lock(norm_key)
    t_wait0 = time.time()
    logged_wait = False
    with tlk:
        if is_windows():
            import msvcrt
            with open(lp, "a+b") as lf:
                lf.seek(0, os.SEEK_END)
                if lf.tell() == 0:
                    lf.write(b"\0")
                    lf.flush()
                lf.seek(0)
                while True:
                    try:
                        msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if label and not logged_wait and (time.time() - t_wait0) >= 1.0:
                            print(f"[{label}] Waiting for exclusive lock ({lp})...", flush=True)
                            logged_wait = True
                        time.sleep(0.05)
                waited = time.time() - t_wait0
                if label and logged_wait:
                    print(f"[{label}] Acquired lock after {waited:.1f}s.", flush=True)
                depths[norm_key] = depths.get(norm_key, 0) + 1
                try:
                    yield
                finally:
                    depths[norm_key] = max(0, depths.get(norm_key, 1) - 1)
                    lf.seek(0)
                    try:
                        msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
        else:
            import fcntl
            with open(lp, "a+") as lf:
                while True:
                    try:
                        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if label and not logged_wait and (time.time() - t_wait0) >= 1.0:
                            print(f"[{label}] Waiting for exclusive lock ({lp})...", flush=True)
                            logged_wait = True
                        time.sleep(0.05)
                waited = time.time() - t_wait0
                if label and logged_wait:
                    print(f"[{label}] Acquired lock after {waited:.1f}s.", flush=True)
                depths[norm_key] = depths.get(norm_key, 0) + 1
                try:
                    yield
                finally:
                    depths[norm_key] = max(0, depths.get(norm_key, 1) - 1)
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _cached_gpu_info(py: str | None = None) -> dict[str, Any]:
    key = py or ""
    with _GPU_INFO_CACHE_LOCK:
        if key in _GPU_INFO_CACHE:
            return _GPU_INFO_CACHE[key]
    info = get_gpu_info(py)
    with _GPU_INFO_CACHE_LOCK:
        _GPU_INFO_CACHE[key] = info
    return info


def _probe_nvidia_smi_free_mb(max_age_s: float = 1.0) -> int | None:
    """Query free CUDA VRAM in MiB via nvidia-smi (cached up to 1s)."""
    global _VRAM_FREE_CACHE
    now_m = time.monotonic()
    with _VRAM_FREE_CACHE_LOCK:
        ts, val = _VRAM_FREE_CACHE
        if (now_m - ts) < max_age_s and ts > 0.0:
            return val
    smi = shutil.which("nvidia-smi")
    free_mb: int | None = None
    if smi:
        try:
            r = subprocess.run(
                [smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                free_mb = int(float(r.stdout.strip().splitlines()[0].strip()))
        except Exception:
            free_mb = None
    with _VRAM_FREE_CACHE_LOCK:
        _VRAM_FREE_CACHE = (time.monotonic(), free_mb)
    return free_mb


def gpu_vram_budget_mb(gpu_info: dict[str, Any] | None = None) -> int:
    """Return usable GPU VRAM budget in MiB for VRAM-aware multi-slot scheduling."""
    env_budget = os.environ.get("AICN_GPU_BUDGET_MB", "").strip()
    if env_budget:
        try:
            val = int(env_budget)
            if val > 0:
                return max(SLOT_UNIT_MB, val)
        except ValueError:
            pass
    env_slots = os.environ.get("AICN_GPU_SLOTS", "").strip()
    if env_slots:
        try:
            slots = int(env_slots)
            if slots > 0:
                return slots * SLOT_UNIT_MB
        except ValueError:
            pass
    info = gpu_info if gpu_info is not None else get_gpu_info()
    backend = info.get("backend")
    if backend == "cuda":
        vram_gb = float(info.get("vram_gb") or 6.0)
        return max(SLOT_UNIT_MB, int(vram_gb * 1024.0 * 0.85))
    if backend == "metal":
        ram_gb = float(info.get("vram_gb") or get_ram_gb() or 8.0)
        return max(SLOT_UNIT_MB, int(ram_gb * 1024.0 * 0.60))
    return 2048


def _try_lock_slot_fd(slot_path: str) -> Any | None:
    if is_windows():
        import msvcrt
        lf = open(slot_path, "a+b")
        lf.seek(0, os.SEEK_END)
        if lf.tell() == 0:
            lf.write(b"\0")
            lf.flush()
        lf.seek(0)
        try:
            msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
            return lf
        except OSError:
            lf.close()
            return None
    import fcntl
    lf = open(slot_path, "a+")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lf
    except BlockingIOError:
        lf.close()
        return None


def _unlock_slot_fd(lf: Any) -> None:
    try:
        if is_windows():
            import msvcrt
            lf.seek(0)
            try:
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            lf.close()
        except OSError:
            pass


@contextlib.contextmanager
def gpu_slot(task: str = "gpu", vram_mb: int = 1536, label: str = "") -> Iterator[dict[str, Any]]:
    """VRAM-aware multi-slot GPU lease (512 MiB slots, crash-safe OS locks, deadlock-free).
    Allows multiple concurrent GPU workloads (e.g. Whisper + Demucs + VieNeu) up to GPU VRAM capacity."""
    env_override = bool(os.environ.get("AICN_GPU_BUDGET_MB", "").strip() or os.environ.get("AICN_GPU_SLOTS", "").strip())
    g_info = None if env_override else _cached_gpu_info()
    budget_mb = gpu_vram_budget_mb(g_info)
    total_slots = max(1, budget_mb // SLOT_UNIT_MB)
    k = max(1, min(total_slots, math.ceil(max(1, int(vram_mb)) / SLOT_UNIT_MB)))

    ldir = os.path.abspath(machine_lock_dir())
    norm_dir = os.path.normcase(ldir)
    os.makedirs(ldir, exist_ok=True)

    leases = getattr(_LOCAL_GPU_HELD, "leases", None)
    if leases is None:
        leases = []
        _LOCAL_GPU_HELD.leases = leases

    inherited_slots: set[int] = set()
    for lease in _env_held_gpu_leases():
        if os.path.normcase(os.path.abspath(str(lease.get("dir", "")))) == norm_dir:
            inherited_slots.update(int(s) for s in lease.get("slots", []))
    for lease in leases:
        if lease.get("dir") == norm_dir:
            inherited_slots.update(int(s) for s in lease.get("slots", []))

    if len(inherited_slots) >= k:
        yield {
            "task": task,
            "slots": sorted(inherited_slots)[:k],
            "vram_mb": k * SLOT_UNIT_MB,
            "total_slots": total_slots,
            "inherited": True,
        }
        return

    need = k - len(inherited_slots)
    alloc_lock = os.path.join(ldir, "gpu_pool_alloc.lock")
    use_vram_guard = (
        g_info is not None
        and g_info.get("backend") == "cuda"
        and os.environ.get("AICN_SKIP_VRAM_PROBE", "").strip() != "1"
    )
    tag = label or f"GPU-POOL:{task}"
    t0 = time.time()
    logged_wait = False
    grabbed: list[tuple[int, Any]] = []

    while True:
        with file_lock(alloc_lock, reentrant=False):
            with _POOL_STATE_LOCK:
                proc_held = _PROCESS_HELD_SLOTS.setdefault(norm_dir, set())
                candidate_grabbed: list[tuple[int, Any]] = []
                other_busy = False
                for idx in range(total_slots):
                    if idx in inherited_slots:
                        continue
                    if idx in proc_held:
                        other_busy = True
                        continue
                    spath = os.path.join(ldir, f"gpu_vram_slot_{idx}.lock")
                    fd = _try_lock_slot_fd(spath)
                    if fd is None:
                        other_busy = True
                        continue
                    candidate_grabbed.append((idx, fd))
                    if len(candidate_grabbed) >= need:
                        break

                if len(candidate_grabbed) == need:
                    vram_blocked = False
                    if use_vram_guard:
                        free_mb = _probe_nvidia_smi_free_mb()
                        if free_mb is not None and free_mb < (int(vram_mb) + 384):
                            if not other_busy:
                                grabbed_idxs_tmp = {i for i, _ in candidate_grabbed}
                                for idx in range(total_slots):
                                    if idx in grabbed_idxs_tmp or idx in inherited_slots:
                                        continue
                                    if idx in proc_held:
                                        other_busy = True
                                        break
                                    probe_fd = _try_lock_slot_fd(os.path.join(ldir, f"gpu_vram_slot_{idx}.lock"))
                                    if probe_fd is None:
                                        other_busy = True
                                        break
                                    _unlock_slot_fd(probe_fd)
                            if other_busy:
                                vram_blocked = True
                    if not vram_blocked:
                        grabbed = candidate_grabbed
                        for idx, _ in grabbed:
                            proc_held.add(idx)
                        break

                for _, fd in candidate_grabbed:
                    _unlock_slot_fd(fd)

        if not logged_wait and (time.time() - t0) >= 1.0:
            print(f"[{tag}] Waiting for {need}x{SLOT_UNIT_MB}MB GPU slots ({vram_mb}MB)...", flush=True)
            logged_wait = True
        time.sleep(0.05)

    if logged_wait:
        print(f"[{tag}] Acquired {len(grabbed)} GPU slot(s) after {time.time() - t0:.1f}s.", flush=True)

    held_now = sorted(inherited_slots | {idx for idx, _ in grabbed})
    lease_rec = {"dir": norm_dir, "vram_mb": k * SLOT_UNIT_MB, "slots": [idx for idx, _ in grabbed]}
    leases.append(lease_rec)
    try:
        yield {
            "task": task,
            "slots": held_now,
            "vram_mb": k * SLOT_UNIT_MB,
            "total_slots": total_slots,
            "inherited": bool(inherited_slots),
        }
    finally:
        if lease_rec in leases:
            leases.remove(lease_rec)
        with _POOL_STATE_LOCK:
            proc_held = _PROCESS_HELD_SLOTS.get(norm_dir)
            for idx, fd in grabbed:
                _unlock_slot_fd(fd)
                if proc_held is not None:
                    proc_held.discard(idx)


def _open_hash_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    try:
        for sql in (
            "PRAGMA busy_timeout=15000",
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            """
            CREATE TABLE IF NOT EXISTS stat_hash (
                norm_path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
        ):
            cur = conn.execute(sql)
            try:
                cur.fetchall()
            finally:
                cur.close()
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


def content_sha256(path: str | Path | None, cache_dir: str | Path | None = None) -> str:
    """Process-safe SQLite-WAL stat-accelerated SHA-256 content hash.
    Moving or recreating identical media yields the exact same content SHA-256."""
    if not path or not os.path.isfile(path):
        return "missing"
    ap_str = os.path.abspath(str(path))
    norm_p = os.path.normcase(ap_str)
    st = os.stat(ap_str)
    idx_dir = str(cache_dir) if cache_dir else global_cache_dir(ap_str)
    os.makedirs(idx_dir, exist_ok=True)
    db_path = os.path.join(idx_dir, "content_hash.db")

    for attempt in range(8):
        conn = None
        try:
            conn = _open_hash_db(db_path)
            cur = conn.execute(
                "SELECT size, mtime_ns, sha256 FROM stat_hash WHERE norm_path = ?",
                (norm_p,),
            )
            try:
                row = cur.fetchone()
            finally:
                cur.close()
            if row and int(row[0]) == st.st_size and int(row[1]) == st.st_mtime_ns and row[2]:
                return str(row[2])
            break
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                time.sleep(0.03 * (attempt + 1))
                continue
            break
        finally:
            if conn is not None:
                conn.close()

    h = hashlib.sha256()
    with open(ap_str, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()

    for attempt in range(8):
        conn = None
        try:
            conn = _open_hash_db(db_path)
            cur = conn.execute(
                "INSERT OR REPLACE INTO stat_hash (norm_path, size, mtime_ns, sha256, updated_at) VALUES (?, ?, ?, ?, ?)",
                (norm_p, int(st.st_size), int(st.st_mtime_ns), digest, time.time()),
            )
            cur.close()
            break
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                time.sleep(0.03 * (attempt + 1))
                continue
            break
        finally:
            if conn is not None:
                conn.close()

    return digest


# ----------------------------------------------------------------- Path normalization & concat
def ffmpeg_concat_line(p: str | Path) -> str:
    """Format an FFmpeg concat demuxer line with POSIX slashes and escaped single quotes."""
    posix = Path(os.path.abspath(str(p))).as_posix()
    escaped = posix.replace("'", r"'\''")
    return f"file '{escaped}'\n"


def normalize_rel_path(p: str | Path, base: str | Path) -> str:
    """Return a POSIX-slash relative path safe across Windows drive casing."""
    ap = os.path.abspath(str(p))
    ab = os.path.abspath(str(base))
    try:
        rel = os.path.relpath(ap, ab)
    except ValueError:
        rel = ap
    return rel.replace("\\", "/")


def same_path(a: str | Path, b: str | Path) -> bool:
    """Case-aware path equality check."""
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def resolve_unit_path(ws: str | Path, p: str | Path | None, default_rel: str = "") -> str:
    """Resolve a workspace-relative or fallback path safely even after unit/hub relocation."""
    ws_abs = os.path.abspath(str(ws))
    cand = str(p or "").strip()
    if cand:
        if os.path.isabs(cand):
            if os.path.exists(cand):
                return cand
        else:
            joined = os.path.abspath(os.path.join(ws_abs, cand))
            if os.path.exists(joined):
                return joined
    if default_rel:
        def_p = os.path.abspath(os.path.join(ws_abs, default_rel))
        if os.path.exists(def_p):
            return def_p
        return def_p
    return os.path.abspath(os.path.join(ws_abs, cand)) if (cand and not os.path.isabs(cand)) else cand


# ----------------------------------------------------------------- Video encoder abstraction
_ENCODER_NAMES = (
    "h264_videotoolbox",
    "hevc_videotoolbox",
    "h264_nvenc",
    "hevc_nvenc",
    "libx264",
    "libx265",
)

_FILTER_NAMES = ("overlay", "scale_vt", "overlay_cuda", "hwupload_cuda", "scale_cuda")


@lru_cache(maxsize=4)
def probe_ffmpeg_encoders(ffmpeg_bin: str | None = None) -> dict[str, bool]:
    """Probe available FFmpeg video encoders."""
    ff = ffmpeg_bin or find_tool("ffmpeg")
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-encoders"],
            capture_output=True, text=True, errors="replace", timeout=10,
        ).stdout
    except Exception:
        out = ""
    return {name: (name in out) for name in _ENCODER_NAMES}


@lru_cache(maxsize=4)
def probe_ffmpeg_filters(ffmpeg_bin: str | None = None) -> dict[str, bool]:
    """Probe available FFmpeg video filters (CPU + CUDA hardware overlay filters)."""
    ff = ffmpeg_bin or find_tool("ffmpeg")
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-filters"],
            capture_output=True, text=True, errors="replace", timeout=10,
        ).stdout
    except Exception:
        out = ""
    return {name: bool(re.search(rf"(?<!\S){re.escape(name)}(?!\S)", out)) for name in _FILTER_NAMES}


def get_ffmpeg_version(ffmpeg_bin: str | None = None) -> str:
    """Return the first line of `ffmpeg -version`."""
    ff = ffmpeg_bin or find_tool("ffmpeg")
    try:
        out = subprocess.run(
            [ff, "-version"],
            capture_output=True, text=True, errors="replace", timeout=5,
        ).stdout
        return (out.splitlines() or [""])[0]
    except Exception:
        return ""


@lru_cache(maxsize=4)
def _nvenc_functional(ffmpeg_bin: str | None = None) -> bool:
    """Run a 1-frame 256x256 null encode to confirm NVENC initializes on the local GPU."""
    ff = ffmpeg_bin or find_tool("ffmpeg")
    try:
        r = subprocess.run(
            [
                ff, "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.04",
                "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def detect_video_backend(encoders: dict[str, bool] | None = None, ffmpeg_bin: str | None = None) -> str:
    """Select video encoding backend: 'videotoolbox' | 'nvenc' | 'cpu'."""
    override = os.environ.get("AICN_VIDEO_BACKEND", "").strip().lower()
    if override in ("videotoolbox", "nvenc", "cpu"):
        return override

    encs = encoders if encoders is not None else probe_ffmpeg_encoders(ffmpeg_bin)
    if is_macos():
        return "videotoolbox" if encs.get("h264_videotoolbox") else "cpu"
    if encs.get("h264_nvenc") and _nvenc_functional(ffmpeg_bin):
        return "nvenc"
    return "cpu"


_BACKEND_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "videotoolbox": {
        "h264": {"backend": "videotoolbox", "vcodec": "h264_videotoolbox", "q": 58, "pix_fmt": "yuv420p", "hwdec": True},
        "hevc": {"backend": "videotoolbox", "vcodec": "hevc_videotoolbox", "q": 60, "tag": "hvc1", "pix_fmt": "yuv420p", "hwdec": True},
        "h264_high": {"backend": "videotoolbox", "vcodec": "h264_videotoolbox", "bitrate": "5000k", "pix_fmt": "yuv420p", "hwdec": True},
    },
    "nvenc": {
        "h264": {"backend": "nvenc", "vcodec": "h264_nvenc", "cq": 22, "preset": "p4", "pix_fmt": "yuv420p", "hwdec": False},
        "hevc": {"backend": "nvenc", "vcodec": "hevc_nvenc", "cq": 24, "preset": "p4", "tag": "hvc1", "pix_fmt": "yuv420p", "hwdec": False},
        "h264_high": {"backend": "nvenc", "vcodec": "h264_nvenc", "bitrate": "5000k", "preset": "p4", "pix_fmt": "yuv420p", "hwdec": False},
    },
    "cpu": {
        "h264": {"backend": "cpu", "vcodec": "libx264", "crf": 20, "preset": "medium", "pix_fmt": "yuv420p", "hwdec": False},
        "hevc": {"backend": "cpu", "vcodec": "libx265", "crf": 22, "preset": "medium", "tag": "hvc1", "pix_fmt": "yuv420p", "hwdec": False},
        "h264_high": {"backend": "cpu", "vcodec": "libx264", "bitrate": "5000k", "preset": "medium", "pix_fmt": "yuv420p", "hwdec": False},
    },
}


def resolve_encode_preset(
    cfg_or_style: dict[str, Any] | None = None,
    codec: str | None = None,
    quality: int | None = None,
    backend: str | None = None,
) -> dict[str, Any]:
    """Resolve an encoding preset dict for the active hardware backend."""
    bk = backend or detect_video_backend()
    table = _BACKEND_PRESETS.get(bk, _BACKEND_PRESETS["cpu"])
    preset_name = codec or (cfg_or_style or {}).get("codec", "h264")
    enc = dict(table.get(preset_name, table["h264"]))
    q = quality if quality is not None else (cfg_or_style or {}).get("quality")
    if q is not None:
        q_int = int(q)
        enc.pop("bitrate", None)
        if bk == "videotoolbox":
            enc["q"] = q_int
        elif bk == "nvenc":
            enc["cq"] = q_int
        else:
            enc["crf"] = q_int
    return enc


def build_hwaccel_input_args(enc: dict[str, Any]) -> list[str]:
    """Return input hwaccel flags. Never injects -hwaccel cuda before CPU overlay filters."""
    if enc.get("hwdec") and str(enc.get("vcodec", "")).endswith("_videotoolbox"):
        return ["-hwaccel", "videotoolbox"]
    return []


def build_vcodec_args(enc: dict[str, Any], include_pix_fmt: bool = False) -> list[str]:
    """Convert a resolved encode preset dict into FFmpeg CLI arguments."""
    vcodec = enc["vcodec"]
    args = ["-c:v", vcodec]
    if vcodec.endswith("_videotoolbox"):
        if "q" in enc:
            args += ["-q:v", str(enc["q"])]
        elif "bitrate" in enc:
            args += ["-b:v", str(enc["bitrate"])]
    elif vcodec.endswith("_nvenc"):
        if enc.get("preset"):
            args += ["-preset", str(enc["preset"])]
        if "cq" in enc:
            args += ["-rc", "vbr", "-cq", str(enc["cq"]), "-b:v", "0"]
        elif "bitrate" in enc:
            args += ["-b:v", str(enc["bitrate"])]
    else:
        if enc.get("preset"):
            args += ["-preset", str(enc["preset"])]
        if "crf" in enc:
            args += ["-crf", str(enc["crf"])]
        elif "bitrate" in enc:
            args += ["-b:v", str(enc["bitrate"])]

    if enc.get("tag"):
        args += ["-tag:v", str(enc["tag"])]
    if include_pix_fmt and enc.get("pix_fmt"):
        args += ["-pix_fmt", str(enc["pix_fmt"])]
    return args


def vendor_h264_encode_args(bitrate: str = "5000k", backend: str | None = None) -> list[str]:
    """Return H.264 bitrate-targeted encode flags for vendor scripts across all backends."""
    bk = backend or detect_video_backend()
    if bk == "videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", bitrate, "-pix_fmt", "yuv420p"]
    if bk == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-b:v", bitrate, "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-b:v", bitrate, "-pix_fmt", "yuv420p"]


# ----------------------------------------------------------------- ASR & Demucs backend selection
def register_cuda_dll_dirs() -> list[str]:
    """On Windows, register PyTorch/nvidia/ctranslate2 CUDA DLL directories before CTranslate2 loads cuBLAS/cuDNN."""
    if not is_windows():
        return []
    import glob as _glob
    import sysconfig as _sysconfig

    added: list[str] = []
    roots = [
        _sysconfig.get_paths().get("purelib", ""),
        os.path.join(sys.prefix, "Lib", "site-packages"),
    ]
    cands: list[str] = []
    for sp in roots:
        if not sp or not os.path.isdir(sp):
            continue
        cands.append(os.path.join(sp, "torch", "lib"))
        cands.append(os.path.join(sp, "ctranslate2"))
        cands.extend(_glob.glob(os.path.join(sp, "nvidia", "*", "bin")))
    cur_path = os.environ.get("PATH", "")
    path_parts = cur_path.split(os.pathsep) if cur_path else []
    for d in cands:
        nd = os.path.normpath(d)
        if not os.path.isdir(nd) or nd in added:
            continue
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(nd)
            except OSError:
                pass
        if nd not in path_parts:
            path_parts.insert(0, nd)
        added.append(nd)
    if added:
        os.environ["PATH"] = os.pathsep.join(path_parts)
    return added


def _probe_module(py: str, mod: str) -> bool:
    try:
        return subprocess.run(
            [py, "-c", f"import {mod}"],
            capture_output=True, timeout=10,
        ).returncode == 0
    except Exception:
        return False


def detect_asr_backend(py: str | None = None, gpu_info: dict[str, Any] | None = None) -> dict[str, str]:
    """Choose ASR backend: 'mlx' | 'faster-cuda' | 'faster-cpu'."""
    override = os.environ.get("AICN_ASR", "").strip().lower()
    target_py = py or find_python()
    gpu = gpu_info or get_gpu_info(target_py)

    if override == "mlx":
        return {"engine": "mlx", "device": "mps", "compute_type": "float16", "label": "mlx-whisper (Metal)"}
    if override == "faster-cuda":
        return {"engine": "faster-cuda", "device": "cuda", "compute_type": "float16", "label": "faster-whisper (CUDA float16)"}
    if override in ("faster-cpu", "cpu"):
        return {"engine": "faster-cpu", "device": "cpu", "compute_type": "int8", "label": "faster-whisper (CPU int8)"}

    if override != "faster" and is_macos() and _probe_module(target_py, "mlx_whisper"):
        return {"engine": "mlx", "device": "mps", "compute_type": "float16", "label": "mlx-whisper (Metal)"}

    if gpu.get("cuda") or gpu.get("ct2_cuda"):
        return {"engine": "faster-cuda", "device": "cuda", "compute_type": "float16", "label": "faster-whisper (CUDA float16)"}

    return {"engine": "faster-cpu", "device": "cpu", "compute_type": "int8", "label": "faster-whisper (CPU int8)"}


def detect_demucs_backend(py: str | None = None, gpu_info: dict[str, Any] | None = None) -> dict[str, str]:
    """Choose Demucs stem separation backend: 'mlx' | 'torch-cuda' | 'torch-cpu'."""
    override = os.environ.get("AICN_DEMUCS", "").strip().lower()
    target_py = py or find_python()
    gpu = gpu_info or get_gpu_info(target_py)

    if override == "mlx":
        return {"engine": "mlx", "device": "mps", "label": "demucs-mlx via uvx"}
    if override == "torch-cuda":
        return {"engine": "torch-cuda", "device": "cuda", "label": "demucs (PyTorch CUDA)"}
    if override == "torch-cpu":
        return {"engine": "torch-cpu", "device": "cpu", "label": "demucs (PyTorch CPU)"}

    if is_macos() and tool_exists("uvx"):
        return {"engine": "mlx", "device": "mps", "label": "demucs-mlx via uvx"}

    if _probe_module(target_py, "demucs"):
        if gpu.get("cuda"):
            return {"engine": "torch-cuda", "device": "cuda", "label": "demucs (PyTorch CUDA)"}
        return {"engine": "torch-cpu", "device": "cpu", "label": "demucs (PyTorch CPU)"}

    if tool_exists("uvx"):
        return {"engine": "mlx" if is_macos() else "torch-cpu", "device": "mps" if is_macos() else "cpu", "label": "demucs via uvx"}

    return {"engine": "missing", "device": "cpu", "label": "MISSING (install demucs or uv)"}
