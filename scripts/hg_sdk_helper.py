#!/usr/bin/env python3
"""
hg_sdk_helper.py — Cross-platform (Windows/macOS/Linux) Hongguo SDK v5 playback resolver.

Wraps `hongguo-drama/scripts/sdk-playback/vendor/phone_account` using `aicn_platform.file_lock`
so Windows, macOS, and Linux all resolve full 1080p/720p Hongguo episodes directly from the
official SDK digest path, with a persistent `--serve` mode that reuses Unicorn + session state
across batch downloads.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import aicn_platform as ap  # noqa: E402


def default_cache_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "aicn" / "hg-sdk-cache"
    return Path.home() / ".cache" / "aicn" / "hg-sdk-cache"


def resolve_phone_account_dir(repo_dir: Path) -> Path:
    for cand in (
        repo_dir / "scripts" / "sdk-playback" / "vendor" / "phone_account",
        repo_dir / "scripts" / "sdk-playback" / "phone_account",
    ):
        if (cand / "phone_sdk_core_v5.py").is_file():
            return cand
    raise RuntimeError(f"phone_account modules not found under {repo_dir}")


def load_device(cache: Path):
    path = cache / "device.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf8"))
        params = value.get("params")
        if not isinstance(params, dict) or params.get("aid") != "8662":
            return None
        if not str(params.get("device_id", "")).isdigit() or not str(params.get("iid", "")).isdigit():
            return None
        return {"params": params, "cookies": value.get("cookies", {})}
    except (OSError, ValueError, TypeError):
        return None


def save_device(cache: Path, context: dict):
    path = cache / "device.json"
    temporary = path.with_suffix(".tmp")
    data = json.dumps(context, separators=(",", ":")).encode("utf8")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


class SdkResolver:
    def __init__(self, repo_dir: Path, cache: Path):
        self.repo_dir = repo_dir
        self.cache = cache
        pa_dir = resolve_phone_account_dir(repo_dir)
        if str(pa_dir) not in sys.path:
            sys.path.insert(0, str(pa_dir))

        from phone_account_device import parse_identity, prepare_registration, registration_cookies
        from phone_playback_request_v5 import prepare_playback_v5
        from phone_sdk_assets_v5 import load_assets
        from phone_sdk_bootstrap_v5 import prepare_bootstrap
        from phone_sdk_core_v5 import SdkDigestCore
        from phone_sdk_exchange_v5 import exchange_response
        from phone_sdk_materials_v5 import decode_app_config, read_sdk_image
        from phone_sdk_session_v5 import send_prepared

        self._parse_identity = parse_identity
        self._prepare_registration = prepare_registration
        self._registration_cookies = registration_cookies
        self._prepare_playback_v5 = prepare_playback_v5
        self._load_assets = load_assets
        self._prepare_bootstrap = prepare_bootstrap
        self._SdkDigestCore = SdkDigestCore
        self._exchange_response = exchange_response
        self._decode_app_config = decode_app_config
        self._read_sdk_image = read_sdk_image
        self._send_prepared = send_prepared

        self.context = None
        self.materials = None
        self.image = None
        self.sdk_core = None
        self.token = None
        self.ladon = None
        self.session_ts = 0.0

    def _register(self, force_new: bool = False):
        if not force_new:
            cached = load_device(self.cache)
            if cached is not None:
                return cached
        url, body, headers, params = self._prepare_registration()
        with requests.Session() as client:
            client.trust_env = False
            response = client.post(
                url,
                data=body,
                headers=headers,
                timeout=(10, 25),
                allow_redirects=False,
                stream=True,
            )
            with response:
                if response.status_code != 200:
                    raise RuntimeError(f"registration HTTP {response.status_code}")
                payload = bytearray()
                for chunk in response.iter_content(65536):
                    payload.extend(chunk)
                    if len(payload) > 2 * 1024 * 1024:
                        raise RuntimeError("registration response too large")
                identity = self._parse_identity(bytes(payload))
                cookies = self._registration_cookies(response, client)
        context = {
            "params": {
                **params,
                "device_id": identity["device_id"],
                "iid": identity["iid"],
            },
            "cookies": cookies,
        }
        save_device(self.cache, context)
        return context

    def _core(self, payload, selector, seconds):
        return self.sdk_core.digest(payload, selector, seconds)[0][:16]

    def ensure_initialized(self, force_new_device: bool = False):
        self.cache.mkdir(parents=True, exist_ok=True)
        lock_path = self.cache / "init.lock"
        with ap.file_lock(str(lock_path)):
            self.context = self._register(force_new=force_new_device)
            if self.materials is None or self.sdk_core is None:
                assets = self._load_assets(self.cache, allow_download=True)
                self.materials = self._decode_app_config(assets.configuration)
                self.image = self._read_sdk_image(assets.image)
                self.sdk_core = self._SdkDigestCore(assets.image)

    def ensure_session(self, force_refresh: bool = False):
        self.ensure_initialized(force_new_device=False)
        if not force_refresh and self.token and self.ladon and (time.monotonic() - self.session_ts < 600):
            return
        prep = self._prepare_bootstrap(
            self.context,
            int(time.time() * 1000),
            materials=self.materials,
            image=self.image,
            core=self._core,
        )
        res = self._exchange_response(
            self._send_prepared(prep),
            common=self.materials.exchange_key,
            inner=self.image.exchange_inner_key,
        )
        ladon = prep.next_ladon
        if res.status == 202:
            prep = self._prepare_bootstrap(
                self.context,
                int(time.time() * 1000),
                materials=self.materials,
                image=self.image,
                core=self._core,
                token=res.token,
                previous_ladon=ladon,
            )
            res = self._exchange_response(
                self._send_prepared(prep),
                common=self.materials.exchange_key,
                inner=self.image.exchange_inner_key,
            )
            ladon = prep.next_ladon
        if res.status != 200 or not res.token:
            raise RuntimeError(f"SDK session status {res.status}")
        self.token = res.token
        self.ladon = ladon
        self.session_ts = time.monotonic()

    def resolve_video(self, video_id: str) -> dict:
        vid = str(video_id).strip()
        if not vid.isdigit():
            raise ValueError(f"invalid video_id: {vid}")
        last_err = None
        for attempt in range(2):
            self.ensure_session(force_refresh=(attempt > 0))
            try:
                playback = self._prepare_playback_v5(
                    self.context,
                    vid,
                    int(time.time() * 1000),
                    token=self.token,
                    previous_ladon=self.ladon,
                    materials=self.materials,
                    image=self.image,
                    core=self._core,
                )
                self.ladon = playback.next_ladon
                payload = json.loads(self._send_prepared(playback))
                data = payload.get("data", {})
                entries = data.get("1004", data)
                entry = entries.get(vid, {}) if isinstance(entries, dict) else {}
                model = entry.get("video_model")
                if isinstance(model, str):
                    model = json.loads(model)
                if payload.get("code") == 0 and isinstance(model, dict) and model.get("video_list"):
                    return {
                        "ok": True,
                        "vid": vid,
                        "model": model,
                        "userAgent": playback.headers["User-Agent"],
                    }
                last_err = RuntimeError(f"SDK playback model unavailable (code={payload.get('code')})")
            except Exception as err:
                last_err = err
                self.token = None
        raise last_err


def main():
    parser = argparse.ArgumentParser(description="Hongguo SDK v5 cross-platform resolver")
    parser.add_argument("--repo", required=True, type=Path, help="Path to tools/hongguo-drama")
    parser.add_argument("--cache", type=Path, default=default_cache_dir())
    parser.add_argument("--video", default="", help="Single video_id to resolve")
    parser.add_argument("--serve", action="store_true", help="Read JSONL {vid} requests on stdin and emit JSONL responses")
    args = parser.parse_args()

    resolver = SdkResolver(args.repo, args.cache)
    if args.serve:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                vid = req.get("vid") or req.get("video") or ""
                out = resolver.resolve_video(vid)
            except Exception as exc:
                out = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
            sys.stdout.write(json.dumps(out, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        return 0

    if not args.video:
        parser.error("Provide --video <vid> or --serve")
    try:
        out = resolver.resolve_video(args.video)
        print(json.dumps(out, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
