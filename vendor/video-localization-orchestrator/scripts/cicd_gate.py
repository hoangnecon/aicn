#!/usr/bin/env python3
"""
Cryptographic & Programmatic CI/CD Gate Engine for Video Localization Suite.
Enforces hard gates between stages (Stages 0..5).
Downstream scripts MUST call `require_gate(workspace, stage_num)` before executing.
If an upstream gate is missing, failed, or its input/output files were modified after signing,
execution aborts immediately with Exit Code 2 (CI/CD GATE BLOCKED).
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Any


GATE_EXIT_CODE = 2


def sha256_file(path: str) -> str:
    """Compute SHA-256 digest of a file (streams in 1MB chunks for large media files)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Cannot hash missing file: {path}")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def cicd_dir(workspace: str) -> str:
    d = os.path.join(os.path.abspath(workspace), ".cicd")
    os.makedirs(d, exist_ok=True)
    return d


def gate_path(workspace: str, stage: int) -> str:
    return os.path.join(cicd_dir(workspace), f"stage_{stage}_gate.json")


def write_gate_token(
    workspace: str,
    stage: int,
    stage_name: str,
    checks: List[Dict[str, Any]],
    metrics: Dict[str, Any],
    artifact_paths: List[str],
) -> Dict[str, Any]:
    """
    Evaluate rubric checks, compute deterministic score (0-100), hash artifacts,
    and write signed gate token to workspace/<slug>/.cicd/stage_N_gate.json.
    """
    workspace = os.path.abspath(workspace)
    total_weight = sum(int(c.get("weight", 10)) for c in checks)
    earned_weight = sum(int(c.get("weight", 10)) for c in checks if c.get("passed", False))
    score = round((earned_weight / max(1, total_weight)) * 100.0, 1)
    all_passed = all(bool(c.get("passed", False)) for c in checks)

    artifacts_hashed = {}
    for rel_or_abs in artifact_paths:
        p = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(workspace, rel_or_abs)
        if not os.path.exists(p):
            all_passed = False
            checks.append({
                "id": f"artifact_exists:{ os.path.basename(p) }",
                "description": f"Required artifact exists: {p}",
                "weight": 20,
                "passed": False,
                "detail": "File does not exist on disk"
            })
        else:
            rel_key = os.path.relpath(p, workspace) if p.startswith(workspace) else p
            artifacts_hashed[rel_key] = {
                "sha256": sha256_file(p),
                "size_bytes": os.path.getsize(p)
            }

    status = "PASS" if (all_passed and score == 100.0) else "FAIL"
    payload = {
        "stage": stage,
        "stage_name": stage_name,
        "status": status,
        "score": score,
        "timestamp": datetime.now().isoformat(),
        "workspace": workspace,
        "metrics": metrics,
        "checks": checks,
        "artifacts": artifacts_hashed,
    }
    # Compute integrity signature over the gate payload
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    payload["signature"] = hashlib.sha256(canonical).hexdigest()

    out_file = gate_path(workspace, stage)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*72}", flush=True)
    print(f"[CI/CD GATE REPORT] Stage {stage}: {stage_name} | Status: {status} | Score: {score}/100", flush=True)
    print(f"{'-'*72}", flush=True)
    for c in checks:
        mark = "PASS" if c.get("passed") else "FAIL"
        print(f"  [{mark}] ({c.get('weight', 10)} pts) {c.get('id')}: {c.get('detail', '')}", flush=True)
    print(f"{'='*72}\n", flush=True)

    if status != "PASS":
        print(f"[CI/CD GATE BLOCKED] Stage {stage} failed verification ({score}/100). Fix all [FAIL] checks above.", file=sys.stderr, flush=True)
        sys.exit(GATE_EXIT_CODE)

    return payload


def require_gate(workspace: str, required_stage: int, verify_hashes: bool = True) -> Dict[str, Any]:
    """
    Enforce that `stage_{required_stage}_gate.json` exists, has status == 'PASS' and score == 100.0,
    and that all hashed artifacts remain unmodified on disk.
    """
    workspace = os.path.abspath(workspace)
    gfile = gate_path(workspace, required_stage)
    if not os.path.exists(gfile):
        print(
            f"[CI/CD GATE BLOCKED] Cannot proceed: Stage {required_stage} gate token is missing ({gfile}).\n"
            f"You MUST run Stage {required_stage} verification and pass 100/100 before running this step.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(GATE_EXIT_CODE)

    with open(gfile, "r", encoding="utf-8") as f:
        data = json.load(f)

    sig = data.pop("signature", None)
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    expected_sig = hashlib.sha256(canonical).hexdigest()
    if sig != expected_sig:
        print(
            f"[CI/CD GATE BLOCKED] Stage {required_stage} gate token signature mismatch! Manual tampering detected in {gfile}.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(GATE_EXIT_CODE)

    if data.get("status") != "PASS" or float(data.get("score", 0)) < 100.0:
        print(
            f"[CI/CD GATE BLOCKED] Stage {required_stage} gate status is {data.get('status')} ({data.get('score')}/100).",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(GATE_EXIT_CODE)

    if verify_hashes:
        for rel_path, meta in data.get("artifacts", {}).items():
            p = rel_path if os.path.isabs(rel_path) else os.path.join(workspace, rel_path)
            if not os.path.exists(p):
                print(
                    f"[CI/CD GATE BLOCKED] Stage {required_stage} verified artifact was deleted: {p}",
                    file=sys.stderr,
                    flush=True,
                )
                sys.exit(GATE_EXIT_CODE)
            cur_hash = sha256_file(p)
            if cur_hash != meta.get("sha256"):
                print(
                    f"[CI/CD GATE BLOCKED] Artifact '{rel_path}' was modified after Stage {required_stage} gate was signed!\n"
                    f"Re-run Stage {required_stage} validator to re-verify and update the gate token.",
                    file=sys.stderr,
                    flush=True,
                )
                sys.exit(GATE_EXIT_CODE)

    print(f"[CI/CD GATE OK] Verified Stage {required_stage} ({data.get('stage_name')}) token & artifact SHA-256 hashes.", flush=True)
    return data


def main():
    parser = argparse.ArgumentParser(description="Inspect CI/CD pipeline gate status for a workspace.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--require-stage", type=int, help="Require a specific stage gate to be PASS.")
    args = parser.parse_args()

    if args.require_stage is not None:
        require_gate(args.workspace, args.require_stage)
        return

    ws = os.path.abspath(args.workspace)
    print(f"CI/CD Gate Status for Workspace: {ws}")
    print("-" * 72)
    for st in range(0, 6):
        gf = gate_path(ws, st)
        if os.path.exists(gf):
            with open(gf, "r", encoding="utf-8") as f:
                d = json.load(f)
            print(f"  Stage {st} ({d.get('stage_name')}): {d.get('status')} | Score: {d.get('score')}/100 | Signed: {d.get('timestamp')}")
        else:
            print(f"  Stage {st}: NOT RUN / MISSING")


if __name__ == "__main__":
    main()
