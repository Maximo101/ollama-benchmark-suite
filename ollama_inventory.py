#!/usr/bin/env python3
"""
ollama_inventory.py
───────────────────
Writes a CSV inventory of every installed Ollama model: size, architecture,
parameters, context length, quantisation, capabilities, default temperature
and licence. Uses only the Ollama HTTP API - no `ollama` CLI or Docker needed.

Usage
─────
  python3 ollama_inventory.py                       # server from $OLLAMA_URL, the
                                                    # Docker container, or localhost
  python3 ollama_inventory.py -u http://nas:11434   # explicit server
  python3 ollama_inventory.py -o my_inventory.csv   # explicit output file

Requirements: Python 3.8+  ·  No third-party packages.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

GIB = 1024 ** 3
HEADERS = ["Model", "Size (GB)", "Arch", "Params", "Context", "Embed", "Quant",
           "Capabilities", "Temp", "License"]


def _docker(*args: str) -> str:
    """stdout of a docker command ('' if it prints nothing)."""
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=10).stdout


def resolve_url(cli_url: str | None, container: str) -> str:
    """CLI flag > $OLLAMA_URL > Docker container IP > localhost."""
    if cli_url:
        return cli_url.rstrip("/")
    if os.environ.get("OLLAMA_URL"):
        return os.environ["OLLAMA_URL"].rstrip("/")
    if shutil.which("docker"):
        try:
            ids = _docker("ps", "-q", "--filter", f"name=^/?{container}$").split()
            if not ids:
                # No exact match: accept a partial name match only when it is unambiguous
                # (otherwise "ollama" would also match e.g. "ollama-webui").
                loose = _docker("ps", "-q", "--filter", f"name={container}").split()
                if len(loose) == 1:
                    ids = loose
                elif len(loose) > 1:
                    print(f"⚠️  {len(loose)} running containers match '{container}'; pass --container with "
                          "the exact name or use --url. Trying localhost.", file=sys.stderr)
            if ids:
                # One address per network, space separated - use the first (none with host networking).
                ips = _docker("inspect", "-f",
                              "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", ids[0]).split()
                if ips:
                    return f"http://{ips[0]}:11434"
        except (subprocess.SubprocessError, OSError):
            pass
    return "http://localhost:11434"


def api(base: str, path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{base}/api/{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def first_nonblank_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return "unknown"


def default_temperature(parameters: str) -> str:
    for line in (parameters or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == "temperature":
            return parts[-1]
    return "unknown"


def describe(base: str, tag: dict) -> list:
    name = tag.get("name", "unknown")
    try:
        info = api(base, "show", {"model": name})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"   ⚠️  {name}: could not read details ({exc})", file=sys.stderr)
        info = {}
    mi, details = info.get("model_info", {}) or {}, info.get("details", {}) or {}
    arch = mi.get("general.architecture", "unknown")
    return [
        name,
        f"{tag.get('size', 0) / GIB:.2f}",
        arch,
        details.get("parameter_size", "unknown"),
        mi.get(f"{arch}.context_length", mi.get("general.context_length", "unknown")),
        mi.get(f"{arch}.embedding_length", "unknown"),
        details.get("quantization_level", "unknown"),
        " | ".join(info.get("capabilities") or []) or "unknown",
        default_temperature(info.get("parameters", "")),
        first_nonblank_line(info.get("license", "")).replace(",", " "),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="CSV inventory of installed Ollama models.")
    ap.add_argument("-u", "--url", help="Ollama server URL (default: auto-detect)")
    ap.add_argument("-o", "--output", help="output CSV path (default: results/ next to this script)")
    ap.add_argument("--container", default=os.environ.get("OLLAMA_CONTAINER", "ollama"),
                    help="Docker container name used for auto-detection (default: ollama)")
    args = ap.parse_args()

    base = resolve_url(args.url, args.container)
    try:
        models = api(base, "tags").get("models", [])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"❌ Cannot reach Ollama at {base}: {exc}\n   Use --url or set OLLAMA_URL.", file=sys.stderr)
        return 1
    if not models:
        print("No models installed.")
        return 1

    if args.output:
        out = Path(args.output)
    else:
        results = Path(__file__).resolve().parent / "results"
        results.mkdir(exist_ok=True)
        out = results / f"ollama_inventory_{datetime.now():%Y-%m-%d_%H-%M-%S}.csv"

    print(f"🔍 {base} - found {len(models)} models. Gathering details...")
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        for tag in sorted(models, key=lambda t: t.get("name", "")):
            print(f"   {tag.get('name')}")
            writer.writerow(describe(base, tag))

    print(f"\n✅ Done! Results saved to:\n{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
