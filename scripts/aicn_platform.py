#!/usr/bin/env python3
"""
aicn_platform.py - Cross-platform runtime, hardware, locking, and FFmpeg abstraction for AICN.

Stdlib-only module supporting macOS (Apple Silicon / Metal / VideoToolbox) and
Windows 10/11 + Linux (NVIDIA CUDA / NVENC / CPU fallback).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

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


# ----------------------------------------------------------------- Cross-platform file locking & global cache
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCK_DEPTH = threading.local()


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
        if (p / "config" / "machine.json").is_file():
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


def child_env_with_held_locks(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a subprocess env propagating locks currently held by THIS thread so child scripts
    calling ap.gpu_lock() / ap.file_lock(reentrant=True) inherit ownership without self-deadlocking."""
    env = dict(os.environ if base_env is None else base_env)
    held = _env_held_locks()
    depths = getattr(_LOCAL_LOCK_DEPTH, "depths", None) or {}
    for k, v in depths.items():
        if v > 0:
            held.add(k)
    if held:
        env["AICN_HELD_LOCKS"] = os.pathsep.join(sorted(held))
    return env


@contextlib.contextmanager
def file_lock(path: str | Path, reentrant: bool = True, label: str = "") -> Iterator[None]:
    """Cross-platform advisory + process file lock using fcntl (POSIX) or msvcrt (Windows).
    Re-entrant within the same thread AND across child subprocesses spawned with
    `child_env_with_held_locks()` when `reentrant=True`, so nested GPU/engine calls never self-deadlock."""
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


@contextlib.contextmanager
def gpu_lock() -> Iterator[None]:
    """Machine-wide exclusive GPU lock preventing concurrent Whisper/Demucs/VieNeu CUDA/Metal VRAM thrashing."""
    with file_lock(os.path.join(machine_lock_dir(), "gpu.lock"), reentrant=True, label="GPU-LOCK"):
        yield


def content_sha256(path: str | Path | None, cache_dir: str | Path | None = None) -> str:
    """Fast content-addressed SHA-256 backed by a persistent (size, mtime_ns) stat index.
    Unlike path+mtime signatures, moving or recreating identical media preserves the exact same hash!"""
    if not path or not os.path.exists(path):
        return "missing"
    ap_str = os.path.abspath(str(path))
    st = os.stat(ap_str)
    idx_dir = str(cache_dir) if cache_dir else global_cache_dir(ap_str)
    idx_path = os.path.join(idx_dir, "global_stat_hash.json")
    db: dict[str, Any] = {}
    if os.path.exists(idx_path):
        try:
            with open(idx_path, "r", encoding="utf-8") as f:
                db = json.load(f)
        except Exception:
            db = {}
    rec = db.get(ap_str)
    if rec and rec.get("size") == st.st_size and rec.get("mtime_ns") == st.st_mtime_ns and rec.get("sha256"):
        return str(rec["sha256"])
    import hashlib
    h = hashlib.sha256()
    with open(ap_str, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    db[ap_str] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest}
    try:
        os.makedirs(idx_dir, exist_ok=True)
        tmp = f"{idx_path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f)
        os.replace(tmp, idx_path)
    except Exception:
        pass
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


# ----------------------------------------------------------------- Video encoder abstraction
_ENCODER_NAMES = (
    "h264_videotoolbox",
    "hevc_videotoolbox",
    "h264_nvenc",
    "hevc_nvenc",
    "libx264",
    "libx265",
)

_FILTER_NAMES = ("overlay", "scale_vt")


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
    """Probe available FFmpeg video filters."""
    ff = ffmpeg_bin or find_tool("ffmpeg")
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-filters"],
            capture_output=True, text=True, errors="replace", timeout=10,
        ).stdout
    except Exception:
        out = ""
    return {name: (f" {name} " in out) for name in _FILTER_NAMES}


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
