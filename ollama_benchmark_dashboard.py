#!/usr/bin/env python3
"""
ollama_benchmark_dashboard.py
─────────────────────────────
https://github.com/Maximo101/ollama-benchmark-suite

Reads a benchmark CSV produced by ollama_benchmark.sh and generates a
self-contained HTML dashboard (Chart.js is loaded from a CDN unless you pass
--embed-chartjs, which makes the report work fully offline).

Usage
─────
  python3 ollama_benchmark_dashboard.py                         # newest benchmark_results*.csv
  python3 ollama_benchmark_dashboard.py results/my_results.csv          # explicit CSV
  python3 ollama_benchmark_dashboard.py results/my_results.csv out.html # explicit CSV + output name
  python3 ollama_benchmark_dashboard.py --vram 24               # override the VRAM limit (GB)
  python3 ollama_benchmark_dashboard.py --min-accuracy 75       # stricter podium filter
  python3 ollama_benchmark_dashboard.py --embed-chartjs         # offline report (downloads once)
  python3 ollama_benchmark_dashboard.py --embed-chartjs chart.umd.min.js   # offline, local file

Requirements: Python 3.8+  ·  No third-party packages needed.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

CHARTJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"
FONTS_URL = ("https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700"
             "&family=Syne:wght@400;600;800&display=swap")

CORRECT_VALUES = {"Yes", "Yes (JSON)"}
GRADED_VALUES = CORRECT_VALUES | {"No"}      # "Info/Creative" prompts are not graded


# ──────────────────────────────────────────────────────────────────────────────
# GPU / VRAM detection (only used when the CSV does not record GPU_VRAM_GB)
# ──────────────────────────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 6) -> str:
    """Run a subprocess and return stdout, or '' on any error."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.stdout.strip()
    except Exception:
        return ""


def _try_nvidia_smi() -> float:
    out = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    total = 0.0
    for line in out.splitlines():
        try:
            total += float(line.strip()) / 1024      # MiB → GiB, summed: Ollama splits across GPUs
        except ValueError:
            pass
    return round(total, 1)


def _try_rocm_smi() -> float:
    out = _run(["rocm-smi", "--showmeminfo", "vram", "--csv"])
    total = 0.0
    for line in out.splitlines():
        m = re.search(r"Total Memory \(B\),(\d+)", line, re.IGNORECASE)
        if m:
            total += int(m.group(1)) / (1024 ** 3)
    return round(total, 1)


def _try_windows() -> float:
    # NB: Win32_VideoController.AdapterRAM is a 32-bit field and caps at ~4 GB.
    # Use --vram if this is wrong for your card.
    out = _run(["wmic", "path", "win32_VideoController", "get", "AdapterRAM", "/value"])
    values = [int(m.group(1)) / (1024 ** 3) for m in re.finditer(r"AdapterRAM=(\d+)", out) if int(m.group(1)) > 0]
    if not values:
        out = _run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty AdapterRAM"])
        for line in out.splitlines():
            try:
                if int(line.strip()) > 0:
                    values.append(int(line.strip()) / (1024 ** 3))
            except ValueError:
                pass
    return round(max(values), 1) if values else 0.0


def _try_sysfs() -> float:
    base = Path("/sys/class/drm")
    if not base.exists():
        return 0.0
    total = 0.0
    for p in base.glob("card*/device/mem_info_vram_total"):
        try:
            total += int(p.read_text().strip()) / (1024 ** 3)
        except Exception:
            pass
    return round(total, 1)


def detect_vram_gb() -> float:
    """Total GPU VRAM (GiB) of THIS machine, or 0.0 if it cannot be determined."""
    for probe in (_try_nvidia_smi, _try_rocm_smi, _try_windows, _try_sysfs):
        v = probe()
        if v:
            return v
    return 0.0


# ──────────────────────────────────────────────────────────────────────────────
# CSV discovery & parsing
# ──────────────────────────────────────────────────────────────────────────────

def find_csv(script_dir: Path) -> Path:
    """Newest benchmark_results*.csv in the script dir, ./results, or the cwd."""
    cwd = Path.cwd()
    found = []
    for d in (script_dir, script_dir / "results", cwd, cwd / "results"):
        if d.is_dir():
            found.extend(d.glob("benchmark_results*.csv"))
    if not found:
        raise FileNotFoundError("No benchmark_results*.csv found. Pass a CSV path as the first argument.")
    return max(found, key=lambda p: p.stat().st_mtime)


def _f(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _avg(lst: list) -> float:
    return sum(lst) / len(lst) if lst else 0.0


_UNIT = {"": 1.0, "B": 1.0, "K": 1e-6, "M": 1e-3, "T": 1e3}


def params_billions(s: str) -> float:
    """'9.7B' → 9.7, '270M' → 0.27, '1.2T' → 1200.  Unknown → 0."""
    m = re.match(r"\s*([\d.]+)\s*([KMBT]?)", (s or "").upper())
    if not m:
        return 0.0
    try:
        return round(float(m.group(1)) * _UNIT[m.group(2)], 3)
    except ValueError:
        return 0.0


def short_name(raw: str) -> str:
    """Readable label that KEEPS the tag (qwen3:8b ≠ qwen3:14b); drops hf.co/author/ and :latest."""
    name = re.sub(r"^hf\.co/[^/]+/", "", raw)
    name = re.sub(r":latest$", "", name)
    return name if len(name) <= 42 else name[:40] + "…"


def is_cloud_model(vram_str: str, ram_str: str) -> bool:
    """Cloud/API rows have neither VRAM nor RAM recorded (older CSVs)."""
    return vram_str.strip() == "" and ram_str.strip() in ("", "0", "0.0", "0.00")


def parse_csv(csv_path: Path) -> tuple:
    """Returns (models, meta). One dict per unique *local* model with aggregated stats."""
    raw = {}
    meta = {"gpu_vram": 0.0, "gpu_name": ""}

    with open(csv_path, encoding="utf-8-sig", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            model = (row.get("Model") or "").strip()
            vram_str, ram_str = row.get("VRAM_GB") or "", row.get("RAM_GB") or ""
            if not model or is_cloud_model(vram_str, ram_str):
                continue

            meta["gpu_vram"] = meta["gpu_vram"] or _f(row.get("GPU_VRAM_GB"))
            if not meta["gpu_name"] and (row.get("GPU") or "") not in ("", "none"):
                meta["gpu_name"] = row["GPU"]

            d = raw.setdefault(model, {
                "id": model, "params_s": row.get("Params") or "", "quant": (row.get("Quant") or "").strip() or "unknown",
                "caps": row.get("Capabilities") or "completion", "size": _f(row.get("Size (GB)")),
                "vram": 0.0, "ram": 0.0, "cold": 0.0,
                "tps": [], "power": [], "prefill": [], "graded": 0, "correct": 0,
            })
            d["vram"] = _f(vram_str) or d["vram"]
            d["ram"] = _f(ram_str) or d["ram"]

            kind = row.get("Test Type", "")
            if kind == "COLD_BOOT" and (row.get("Response") or "") in ("Loaded", ""):
                d["cold"] = _f(row.get("Latency (s)"))
            elif kind == "WARM":
                tps = _f(row.get("TPS"))
                if tps > 0:
                    d["tps"].append(tps)
                c = row.get("Correct", "")
                if c in GRADED_VALUES:
                    d["graded"] += 1
                    d["correct"] += c in CORRECT_VALUES
                pw = _f(row.get("Power (W)"))
                if pw > 0:
                    d["power"].append(pw)
                pf = _f(row.get("Prefill TPS"))
                if pf > 0:
                    d["prefill"].append(pf)

    models = []
    for d in raw.values():
        tps = _avg(d["tps"])
        if tps == 0:                      # produced no output at all
            continue
        models.append({
            "id": d["id"], "short": short_name(d["id"]),
            "params": params_billions(d["params_s"]), "quant": d["quant"],
            "vram": round(d["vram"], 2), "ram": round(d["ram"], 2), "size": round(d["size"], 2),
            "tps": round(tps, 1), "prefill": round(_avg(d["prefill"]), 1),
            "cold": round(d["cold"], 1),
            "acc": round(d["correct"] / d["graded"] * 100) if d["graded"] else 0,
            "power": round(_avg(d["power"]), 1), "caps": d["caps"] or "completion",
        })

    # Labels must be unique (charts highlight by label) - fall back to the full name on clashes.
    seen = {}
    for m in models:
        seen.setdefault(m["short"], []).append(m)
    for group in seen.values():
        if len(group) > 1:
            for m in group:
                m["short"] = m["id"]
    return models, meta


# ──────────────────────────────────────────────────────────────────────────────
# HTML template  ({{PLACEHOLDERS}} are replaced at render time)
# ──────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ollama Benchmark Dashboard</title>
{{HEAD_ASSETS}}
<style>
:root{--bg:#0a0a0f;--surface:#12121a;--surface2:#1a1a26;--border:#2a2a3e;
  --accent:#7fff6e;--accent2:#ffca3a;--accent3:#ff6b9d;--accent4:#4fc3f7;
  --text:#e8e8f0;--muted:#7878a0;--gold:#FFD700;--silver:#C0C0C0;--bronze:#CD7F32;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:'Syne',system-ui,sans-serif;min-height:100vh;overflow-x:hidden;}
body::before{content:'';position:fixed;inset:0;
  background-image:linear-gradient(rgba(127,255,110,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(127,255,110,.03) 1px,transparent 1px);
  background-size:40px 40px;pointer-events:none;z-index:0;}
.container{position:relative;z-index:1;max-width:1280px;margin:0 auto;padding:0 24px 60px;}
header{padding:48px 0 36px;border-bottom:1px solid var(--border);margin-bottom:40px;}
.header-label{font-family:'Space Mono',ui-monospace,monospace;font-size:11px;color:var(--accent);letter-spacing:3px;text-transform:uppercase;margin-bottom:12px;}
h1{font-size:clamp(28px,5vw,52px);font-weight:800;line-height:1.05;letter-spacing:-1px;}
h1 span{color:var(--accent);}
.hw-badge{display:inline-flex;align-items:center;gap:8px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;padding:6px 14px;font-family:'Space Mono',ui-monospace,monospace;font-size:11px;color:var(--muted);margin-top:16px;margin-right:8px;}
.hw-badge .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);flex-shrink:0;}
.hw-badge.warn .dot{background:var(--accent2);}
.section-title{font-size:11px;font-family:'Space Mono',ui-monospace,monospace;color:var(--muted);text-transform:uppercase;letter-spacing:3px;margin-bottom:8px;display:flex;align-items:center;gap:12px;}
.section-title::after{content:'';flex:1;height:1px;background:var(--border);}
.section-subtitle{font-size:11px;color:var(--muted);font-family:'Space Mono',ui-monospace,monospace;margin-bottom:20px;opacity:.8;}
.podium-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:48px;}
.podium-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:24px 20px;position:relative;overflow:hidden;transition:transform .2s;}
.podium-card:hover{transform:translateY(-3px);}
.podium-card.rank-1{border-color:var(--gold);}
.podium-card.rank-2{border-color:var(--silver);}
.podium-card.rank-3{border-color:var(--bronze);}
.podium-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;}
.rank-1::before{background:var(--gold);}
.rank-2::before{background:var(--silver);}
.rank-3::before{background:var(--bronze);}
.rank-badge{position:absolute;top:20px;right:20px;width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-family:'Space Mono',ui-monospace,monospace;font-size:14px;font-weight:700;}
.rank-1 .rank-badge{background:rgba(255,215,0,.15);color:var(--gold);}
.rank-2 .rank-badge{background:rgba(192,192,192,.15);color:var(--silver);}
.rank-3 .rank-badge{background:rgba(205,127,50,.15);color:var(--bronze);}
.model-name{font-size:15px;font-weight:700;margin-bottom:4px;padding-right:44px;line-height:1.3;word-break:break-word;}
.model-sub{font-family:'Space Mono',ui-monospace,monospace;font-size:10px;color:var(--muted);margin-bottom:20px;}
.stat-row{display:flex;justify-content:space-between;margin-bottom:10px;font-size:13px;}
.stat-label{color:var(--muted);font-family:'Space Mono',ui-monospace,monospace;font-size:10px;}
.stat-value{font-family:'Space Mono',ui-monospace,monospace;font-weight:700;}
.tps-big{font-size:36px;font-weight:800;margin:12px 0 4px;letter-spacing:-1px;}
.rank-1 .tps-big{color:var(--gold);}
.rank-2 .tps-big{color:var(--silver);}
.rank-3 .tps-big{color:var(--bronze);}
.tps-unit{font-size:13px;color:var(--muted);font-weight:400;}
.empty{grid-column:1/-1;background:var(--surface);border:1px dashed var(--border);border-radius:8px;padding:24px;font-family:'Space Mono',ui-monospace,monospace;font-size:11px;color:var(--muted);}
.pill{display:inline-block;padding:2px 8px;border-radius:3px;font-family:'Space Mono',ui-monospace,monospace;font-size:10px;background:rgba(127,255,110,.1);color:var(--accent);border:1px solid rgba(127,255,110,.2);margin-top:8px;margin-right:4px;}
.pill.warn{background:rgba(255,202,58,.1);color:var(--accent2);border-color:rgba(255,202,58,.2);}
.pill.info{background:rgba(79,195,247,.1);color:var(--accent4);border-color:rgba(79,195,247,.2);}
.pill.pink{background:rgba(255,107,157,.1);color:var(--accent3);border-color:rgba(255,107,157,.2);}
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:48px;}
.chart-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:24px;}
.chart-card.wide{grid-column:1/-1;}
.chart-title{font-size:13px;font-weight:600;margin-bottom:6px;}
.chart-desc{font-family:'Space Mono',ui-monospace,monospace;font-size:10px;color:var(--muted);margin-bottom:20px;}
.cap-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:20px;margin-bottom:48px;}
.cap-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:24px;}
.cap-label{font-family:'Space Mono',ui-monospace,monospace;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--accent4);margin-bottom:4px;}
.cap-title{font-size:14px;font-weight:700;margin-bottom:4px;}
.cap-desc{font-family:'Space Mono',ui-monospace,monospace;font-size:10px;color:var(--muted);margin-bottom:16px;}
.table-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:48px;overflow-x:auto;}
table{width:100%;border-collapse:collapse;min-width:1000px;}
thead th{background:var(--surface2);padding:12px 14px;font-family:'Space Mono',ui-monospace,monospace;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;text-align:left;border-bottom:1px solid var(--border);cursor:pointer;user-select:none;white-space:nowrap;transition:color .15s;}
thead th:hover{color:var(--accent);}
thead th .sort-icon{margin-left:5px;opacity:.5;font-size:10px;}
thead th.sort-asc .sort-icon::after{content:'▲';color:var(--accent);opacity:1;}
thead th.sort-desc .sort-icon::after{content:'▼';color:var(--accent);opacity:1;}
thead th:not(.sort-asc):not(.sort-desc) .sort-icon::after{content:'⇅';}
tbody tr{border-bottom:1px solid rgba(42,42,62,.5);transition:background .15s;}
tbody tr:hover{background:var(--surface2);}
tbody tr:last-child{border-bottom:none;}
tbody td{padding:10px 14px;font-size:12px;font-family:'Space Mono',ui-monospace,monospace;}
.vram-bar{height:4px;background:var(--border);border-radius:2px;margin-top:4px;overflow:hidden;}
.vram-bar-fill{height:100%;border-radius:2px;}
.vram-ok{background:var(--accent);}
.vram-warn{background:var(--accent2);}
.vram-over{background:var(--accent3);}
.acc-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle;}
.quant-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;margin-bottom:48px;}
.quant-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px;}
.quant-name{font-family:'Space Mono',ui-monospace,monospace;font-size:16px;font-weight:700;color:var(--accent4);margin-bottom:4px;}
.quant-model{font-size:11px;color:var(--muted);margin-bottom:14px;font-family:'Space Mono',ui-monospace,monospace;}
.quant-stat-row{display:flex;justify-content:space-between;gap:12px;margin-bottom:8px;font-size:12px;font-family:'Space Mono',ui-monospace,monospace;}
.quant-stat-label{color:var(--muted);}
.footer{text-align:center;font-family:'Space Mono',ui-monospace,monospace;font-size:10px;color:var(--muted);padding-top:24px;border-top:1px solid var(--border);}
@media(max-width:768px){.podium-grid,.chart-grid,.cap-grid{grid-template-columns:1fr;}.chart-card.wide{grid-column:auto;}}
</style>
</head>
<body>
<div class="container">

<header>
  <div class="header-label">// local inference benchmark — {{CSV_NAME}}</div>
  <h1>Ollama Model<br><span>Performance Report</span></h1>
  <div>
    <span class="hw-badge"><span class="dot"></span>Generated {{DATE}}</span>
    <span class="hw-badge"><span class="dot"></span>{{MODEL_COUNT}} local models</span>
    {{GPU_BADGE}}
    <span class="hw-badge {{VRAM_BADGE_CLASS}}"><span class="dot"></span>VRAM limit {{VRAM_LIMIT}} GB {{VRAM_SOURCE}}</span>
  </div>
</header>

<div class="section-title">Top 3 — Goldilocks (Best Overall)</div>
<p class="section-subtitle">Highest params × tokens-per-second among VRAM-only models with ≥ {{MIN_ACC}}% accuracy (graded prompts only). MoE models are scored on total, not active, parameters.</p>
<div class="podium-grid" id="goldilocksGrid"></div>

<div class="section-title">Top 3 — Agent Use (Tool-Calling)</div>
<p class="section-subtitle">Models tagged 'tools' — VRAM-only, ≥ {{MIN_ACC}}% accuracy, ranked by params × TPS</p>
<div class="podium-grid" id="agentGrid"></div>

<div class="section-title">Speed Overview</div>
<div class="chart-grid">
  <div class="chart-card wide">
    <div class="chart-title">Average Generation TPS — All VRAM-Only Models</div>
    <div class="chart-desc">sorted fastest → slowest · green ≥200 · blue ≥100 · yellow ≥50 · pink &lt;50</div>
    <canvas id="tpsBar" style="max-height:290px"></canvas>
  </div>
  <div class="chart-card">
    <div class="chart-title">VRAM Used vs TPS (bubble size = params)</div>
    <div class="chart-desc">green = VRAM-only · red = RAM spill · axis ends at the {{VRAM_LIMIT}} GB limit</div>
    <canvas id="scatter"></canvas>
  </div>
  <div class="chart-card">
    <div class="chart-title">Accuracy vs Speed</div>
    <div class="chart-desc">gold = Goldilocks top-3 · teal = Agent top-3 · accuracy over graded prompts only</div>
    <canvas id="accScatter"></canvas>
  </div>
  <div class="chart-card">
    <div class="chart-title">Quantization — Avg TPS by Type</div>
    <div class="chart-desc">VRAM-only models · n = sample count</div>
    <canvas id="quantBar"></canvas>
  </div>
  <div class="chart-card">
    <div class="chart-title">Power Efficiency — Top 16 (TPS / Watt)</div>
    <div class="chart-desc">more tokens per watt = better running cost</div>
    <canvas id="effBar" style="max-height:330px"></canvas>
  </div>
  <div class="chart-card wide" id="coldCard">
    <div class="chart-title">Cold-Boot Load Time vs Model Size</div>
    <div class="chart-desc">wall-clock seconds to load from disk after a VRAM purge + page-cache flush · green = VRAM-only · red = RAM spill</div>
    <canvas id="coldScatter" style="max-height:300px"></canvas>
  </div>
</div>

<div class="section-title">TPS vs Parameter Size by Capability</div>
<p class="section-subtitle">x-axis = model size (B params) · y-axis = avg tokens/sec · bright dot = 100% accurate · dim = partial accuracy</p>
<div class="cap-grid">
  <div class="cap-card">
    <div class="cap-label">capability</div>
    <div class="cap-title">Completion (All Models)</div>
    <div class="cap-desc">Every local model — VRAM-only</div>
    <canvas id="capCompletion" style="max-height:260px"></canvas>
  </div>
  <div class="cap-card">
    <div class="cap-label">capability</div>
    <div class="cap-title">Tool Use / Function Calling</div>
    <div class="cap-desc">Models tagged with 'tools' — VRAM-only</div>
    <canvas id="capTools" style="max-height:260px"></canvas>
  </div>
  <div class="cap-card">
    <div class="cap-label">capability</div>
    <div class="cap-title">Thinking / Chain-of-Thought</div>
    <div class="cap-desc">Models tagged with 'thinking' — VRAM-only</div>
    <canvas id="capThinking" style="max-height:260px"></canvas>
  </div>
  <div class="cap-card">
    <div class="cap-label">capability</div>
    <div class="cap-title">Vision / Multimodal</div>
    <div class="cap-desc">Models tagged with 'vision' — VRAM-only</div>
    <canvas id="capVision" style="max-height:260px"></canvas>
  </div>
</div>

<div class="section-title">Full Model Comparison</div>
<p class="section-subtitle">Click any column header to sort ↑ or ↓ — click again to reverse · cloud/API models excluded</p>
<div class="table-card">
  <table id="mainTable">
    <thead><tr>
      <th data-col="0" data-type="str">Model <span class="sort-icon"></span></th>
      <th data-col="1" data-type="num">Params <span class="sort-icon"></span></th>
      <th data-col="2" data-type="str">Quant <span class="sort-icon"></span></th>
      <th data-col="3" data-type="num">VRAM GB <span class="sort-icon"></span></th>
      <th data-col="4" data-type="num">RAM Spill <span class="sort-icon"></span></th>
      <th data-col="5" data-type="num">Avg TPS <span class="sort-icon"></span></th>
      <th data-col="6" data-type="num">Prefill TPS <span class="sort-icon"></span></th>
      <th data-col="7" data-type="num">Cold Load s <span class="sort-icon"></span></th>
      <th data-col="8" data-type="num">Accuracy <span class="sort-icon"></span></th>
      <th data-col="9" data-type="num">Power W <span class="sort-icon"></span></th>
      <th data-col="10" data-type="str">Capabilities <span class="sort-icon"></span></th>
    </tr></thead>
    <tbody id="fullTable"></tbody>
  </table>
</div>

<div class="section-title">Quantization Analysis</div>
<div class="quant-grid" id="quantGrid"></div>

<div class="footer">
  Generated from {{CSV_NAME}} · {{DATE}} · VRAM limit {{VRAM_LIMIT}} GB {{VRAM_SOURCE}} · cloud/API models excluded
</div>
</div>

<script>
const MODELS     = {{MODELS_JSON}};
const VRAM_LIMIT = {{VRAM_LIMIT}};
const MIN_ACC    = {{MIN_ACC}};

const valid = MODELS.filter(m => m.tps > 0 && m.ram === 0);
const allV  = MODELS.filter(m => m.tps > 0);

const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtParams = p => p >= 1 ? `${p}B` : p > 0 ? `${Math.round(p * 1000)}M` : '?';

function tpsColor(t) {
  if (t >= 200) return '#7fff6e';
  if (t >= 100) return '#4fc3f7';
  if (t >= 50)  return '#ffca3a';
  return '#ff6b9d';
}
function hasCap(m, c) {
  return m.caps.split(',').map(x => x.trim()).includes(c);
}

// Goldilocks score = params × TPS  (rewards bigger AND faster models equally)
const goldiScore = m => m.params * m.tps;

const ax = t => ({
  ticks: { color:'#7878a0', font:{ family:'Space Mono', size:9 } },
  grid:  { color:'rgba(42,42,62,.4)' },
  title: { display:!!t, text:t||'', color:'#7878a0', font:{ family:'Space Mono', size:10 } }
});

function capPillsHtml(caps, small) {
  return caps.split(',').map(c => {
    c = c.trim();
    const cl = c==='tools'?'warn': c==='thinking'?'pink': c==='vision'?'info':'';
    const st = small ? 'margin-top:0;padding:1px 5px;font-size:9px' : '';
    return `<span class="pill ${cl}" style="${st}">${esc(c)}</span>`;
  }).join('');
}

// ── PODIUM ────────────────────────────────────────────────────────────────────
function buildPodium(id, list, badge) {
  const g = document.getElementById(id);
  if (!list.length) {
    g.innerHTML = `<div class="empty">No VRAM-only model reached ${MIN_ACC}% accuracy for this category.</div>`;
    return;
  }
  const ranks = ['rank-1','rank-2','rank-3'], nums = ['#1','#2','#3'];
  g.innerHTML = list.slice(0, 3).map((m, i) => `
    <div class="podium-card ${ranks[i]}">
      <div class="rank-badge">${nums[i]}</div>
      <div class="model-name">${esc(m.short)}</div>
      <div class="model-sub">${esc(m.quant)} · ${fmtParams(m.params)} params</div>
      <div class="tps-big">${m.tps.toFixed(1)} <span class="tps-unit">tok/s</span></div>
      <div class="stat-row"><span class="stat-label">VRAM Used</span><span class="stat-value" style="color:var(--accent)">${m.vram.toFixed(2)} GB</span></div>
      <div class="stat-row"><span class="stat-label">Accuracy</span><span class="stat-value" style="color:${m.acc===100?'var(--accent)':'var(--accent2)'}">${m.acc}%</span></div>
      <div class="stat-row"><span class="stat-label">Power</span><span class="stat-value">${m.power ? m.power.toFixed(1) + ' W' : '—'}</span></div>
      <span class="pill">${badge}</span>${capPillsHtml(m.caps)}
    </div>`).join('');
}

const goldilocks = valid.filter(m => m.acc >= MIN_ACC).sort((a, b) => goldiScore(b) - goldiScore(a));
const agents     = valid.filter(m => hasCap(m, 'tools') && m.acc >= MIN_ACC).sort((a, b) => goldiScore(b) - goldiScore(a));
buildPodium('goldilocksGrid', goldilocks, 'Goldilocks ✓');
buildPodium('agentGrid',      agents,     'tool-calling ✓');

// ── TPS BAR ───────────────────────────────────────────────────────────────────
const tpsSorted = [...valid].sort((a, b) => b.tps - a.tps);
new Chart(document.getElementById('tpsBar'), {
  type: 'bar',
  data: {
    labels: tpsSorted.map(m => m.short),
    datasets: [{ data: tpsSorted.map(m => m.tps),
      backgroundColor: tpsSorted.map(m => tpsColor(m.tps)+'cc'),
      borderColor:     tpsSorted.map(m => tpsColor(m.tps)),
      borderWidth:1, borderRadius:3 }]
  },
  options: { responsive:true,
    plugins:{ legend:{display:false}, tooltip:{callbacks:{label:c=>` ${c.raw.toFixed(1)} tok/s`}} },
    scales:{ x:{ ...ax(), ticks:{...ax().ticks,maxRotation:55,minRotation:45} }, y:ax('Avg TPS') } }
});

// ── BUBBLE SCATTER ────────────────────────────────────────────────────────────
const bubble = m => ({x:m.vram, y:m.tps, r:Math.max(3, Math.sqrt(m.params)*2.5), label:m.short});
new Chart(document.getElementById('scatter'), {
  type: 'bubble',
  data: { datasets: [
    { label:'VRAM-only', data: allV.filter(m=>m.ram===0).map(bubble),
      backgroundColor:'rgba(127,255,110,.35)', borderColor:'#7fff6e', borderWidth:1 },
    { label:'RAM spill', data: allV.filter(m=>m.ram>0).map(bubble),
      backgroundColor:'rgba(255,107,157,.35)', borderColor:'#ff6b9d', borderWidth:1 }
  ]},
  options: { responsive:true,
    plugins:{ legend:{labels:{color:'#7878a0',font:{family:'Space Mono',size:10}}},
      tooltip:{callbacks:{label:c=>` ${c.raw.label}: ${c.raw.x}GB / ${c.raw.y.toFixed(1)} TPS`}} },
    scales:{ x:{...ax('VRAM (GB)'),min:0,max:VRAM_LIMIT+1}, y:ax('Avg TPS') } }
});

// ── ACC vs TPS SCATTER ────────────────────────────────────────────────────────
const g3 = new Set(goldilocks.slice(0,3).map(m=>m.id));
const a3 = new Set(agents.slice(0,3).map(m=>m.id));
const hi = m => g3.has(m.id) || a3.has(m.id);
new Chart(document.getElementById('accScatter'), {
  type: 'scatter',
  data: { datasets:[{ label:'Models',
    data: valid.map(m=>({x:m.tps,y:m.acc,label:m.short})),
    backgroundColor: valid.map(m=> g3.has(m.id)?'#ffd700cc': a3.has(m.id)?'#4fc3f7cc':'rgba(127,127,180,.5)'),
    borderColor:     valid.map(m=> g3.has(m.id)?'#ffd700':  a3.has(m.id)?'#4fc3f7':'rgba(127,127,180,.7)'),
    pointRadius:     valid.map(m=> hi(m)?9:5),
    borderWidth:     valid.map(m=> hi(m)?2:1),
  }]},
  options:{ responsive:true,
    plugins:{legend:{display:false}, tooltip:{callbacks:{label:c=>` ${c.raw.label}: ${c.raw.y}% / ${c.raw.x.toFixed(1)} TPS`}}},
    scales:{ x:ax('Avg TPS'), y:{...ax('Accuracy %'),min:-5,max:105} } }
});

// ── QUANT BAR ─────────────────────────────────────────────────────────────────
const qMap = {};
valid.forEach(m=>{ (qMap[m.quant] = qMap[m.quant] || []).push(m.tps); });
const qL = Object.keys(qMap).sort();
const qA = qL.map(q => qMap[q].reduce((a,b)=>a+b,0)/qMap[q].length);
const qC = ['#7fff6e','#4fc3f7','#ffca3a','#ff6b9d','#c88dff','#ff9d4f','#aaffee'];
const qCol = qL.map((_, i) => qC[i % qC.length]);
new Chart(document.getElementById('quantBar'), {
  type:'bar',
  data:{ labels:qL, datasets:[{ label:'Avg TPS', data:qA,
    backgroundColor: qCol.map(c=>c+'cc'), borderColor: qCol, borderWidth:1, borderRadius:4 }] },
  options:{ responsive:true,
    plugins:{legend:{display:false}, tooltip:{callbacks:{label:c=>` avg ${c.raw.toFixed(1)} TPS (n=${qMap[qL[c.dataIndex]].length})`}}},
    scales:{ x:ax(), y:ax('Avg TPS') } }
});

// ── EFFICIENCY BAR ────────────────────────────────────────────────────────────
const eff = valid.filter(m=>m.power>0)
  .map(m=>({short:m.short, e:m.tps/m.power}))
  .sort((a,b)=>b.e-a.e).slice(0,16);
new Chart(document.getElementById('effBar'), {
  type:'bar',
  data:{ labels:eff.map(m=>m.short), datasets:[{ data:eff.map(m=>m.e),
    backgroundColor: eff.map((_,i)=>i<3?'#7fff6ecc':'#4fc3f788'),
    borderColor:     eff.map((_,i)=>i<3?'#7fff6e':'#4fc3f7'),
    borderWidth:1, borderRadius:3 }] },
  options:{ responsive:true, indexAxis:'y',
    plugins:{legend:{display:false}, tooltip:{callbacks:{label:c=>` ${c.raw.toFixed(2)} TPS/W`}}},
    scales:{ x:ax('TPS / Watt'), y:{...ax(),ticks:{...ax().ticks,font:{family:'Space Mono',size:9}}} } }
});

// ── COLD-BOOT LOAD TIME ───────────────────────────────────────────────────────
const coldSet = allV.filter(m => m.cold > 0);
if (!coldSet.length) {
  document.getElementById('coldCard').style.display = 'none';
} else {
  const pt = m => ({x: m.size || (m.vram + m.ram), y: m.cold, label: m.short});
  new Chart(document.getElementById('coldScatter'), {
    type:'scatter',
    data:{ datasets:[
      { label:'VRAM-only', data: coldSet.filter(m=>m.ram===0).map(pt),
        backgroundColor:'rgba(127,255,110,.5)', borderColor:'#7fff6e', pointRadius:6 },
      { label:'RAM spill', data: coldSet.filter(m=>m.ram>0).map(pt),
        backgroundColor:'rgba(255,107,157,.5)', borderColor:'#ff6b9d', pointRadius:6 } ]},
    options:{ responsive:true,
      plugins:{ legend:{labels:{color:'#7878a0',font:{family:'Space Mono',size:10}}},
        tooltip:{callbacks:{label:c=>` ${c.raw.label}: ${c.raw.x} GB → ${c.raw.y}s`}} },
      scales:{ x:{...ax('Model file size (GB)'),min:0}, y:{...ax('Cold load (s)'),min:0} } }
  });
}

// ── CAPABILITY SCATTER (x=params, y=TPS) ─────────────────────────────────────
function capScatter(canvasId, filterFn, color) {
  const sub = valid.filter(filterFn);
  if (!sub.length) {
    document.getElementById(canvasId).closest('.cap-card').style.opacity='.4';
    return;
  }
  new Chart(document.getElementById(canvasId), {
    type:'scatter',
    data:{ datasets:[{ label:'Models',
      data: sub.map(m=>({x:m.params,y:m.tps,label:m.short,acc:m.acc})),
      backgroundColor: sub.map(m=>m.acc===100?color+'cc':color+'55'),
      borderColor:     sub.map(m=>m.acc===100?color:color+'88'),
      pointRadius: sub.map(m=>m.acc===100?8:6), borderWidth:1 }] },
    options:{ responsive:true,
      plugins:{ legend:{display:false},
        tooltip:{callbacks:{label:c=>` ${c.raw.label}: ${fmtParams(c.raw.x)} · ${c.raw.y.toFixed(1)} TPS · ${c.raw.acc}% acc`}} },
      scales:{ x:{...ax('Parameters (B)'),min:0}, y:{...ax('Avg TPS'),min:0} } }
  });
}
capScatter('capCompletion', m=>hasCap(m,'completion'), '#7fff6e');
capScatter('capTools',      m=>hasCap(m,'tools'),      '#ffca3a');
capScatter('capThinking',   m=>hasCap(m,'thinking'),   '#c88dff');
capScatter('capVision',     m=>hasCap(m,'vision'),     '#4fc3f7');

// ── FULL TABLE ────────────────────────────────────────────────────────────────
function renderTable(data) {
  document.getElementById('fullTable').innerHTML = data.map(m => {
    const vp = Math.min((m.vram / VRAM_LIMIT) * 100, 100);
    const vc = m.ram > 0 ? 'vram-over' : m.vram > VRAM_LIMIT * 0.88 ? 'vram-warn' : 'vram-ok';
    const ac = m.acc===100?'var(--accent)': m.acc>=75?'var(--accent2)':'var(--accent3)';
    return `<tr
      data-0="${esc(m.short)}" data-1="${m.params}" data-2="${esc(m.quant)}"
      data-3="${m.vram}" data-4="${m.ram}" data-5="${m.tps}" data-6="${m.prefill}"
      data-7="${m.cold}" data-8="${m.acc}" data-9="${m.power}" data-10="${esc(m.caps)}">
      <td style="color:var(--text);font-size:11px">${esc(m.short)}</td>
      <td>${fmtParams(m.params)}</td>
      <td style="color:var(--accent4)">${esc(m.quant)}</td>
      <td>${m.vram.toFixed(2)}<div class="vram-bar"><div class="vram-bar-fill ${vc}" style="width:${vp}%"></div></div></td>
      <td style="color:${m.ram>0?'var(--accent3)':'var(--muted)'}">${m.ram>0?m.ram.toFixed(2)+' ⚠':'—'}</td>
      <td style="font-weight:700;color:${tpsColor(m.tps)}">${m.tps.toFixed(1)}</td>
      <td style="color:var(--muted)">${m.prefill>0?m.prefill.toFixed(0):'—'}</td>
      <td style="color:var(--muted)">${m.cold>0?m.cold.toFixed(1):'—'}</td>
      <td><span class="acc-dot" style="background:${ac}"></span>${m.acc}%</td>
      <td style="color:var(--muted)">${m.power>0?m.power.toFixed(1):'—'}</td>
      <td>${capPillsHtml(m.caps, true)}</td>
    </tr>`;
  }).join('');
}
renderTable([...allV].sort((a,b)=>a.ram-b.ram||b.tps-a.tps));

let sCol = null, sDir = 1;
document.querySelectorAll('#mainTable thead th').forEach(th => {
  th.addEventListener('click', () => {
    const col  = th.dataset.col;
    const type = th.dataset.type;
    sDir = sCol === col ? -sDir : 1;
    sCol = col;
    document.querySelectorAll('#mainTable thead th').forEach(h => h.classList.remove('sort-asc','sort-desc'));
    th.classList.add(sDir === 1 ? 'sort-asc' : 'sort-desc');
    const rows = [...document.querySelectorAll('#mainTable tbody tr')];
    rows.sort((a, b) => type === 'num'
      ? ((parseFloat(a.dataset[col])||0) - (parseFloat(b.dataset[col])||0)) * sDir
      : a.dataset[col].localeCompare(b.dataset[col]) * sDir);
    const tb = document.getElementById('fullTable');
    rows.forEach(r => tb.appendChild(r));
  });
});

// ── QUANT CARDS (one per quantisation actually present) ──────────────────────
document.getElementById('quantGrid').innerHTML = qL.map(q => {
  const ms = valid.filter(m => m.quant === q);
  const at = ms.reduce((a,b)=>a+b.tps,0)/ms.length;
  const av = ms.reduce((a,b)=>a+b.vram,0)/ms.length;
  return `
  <div class="quant-card">
    <div class="quant-name">${esc(q)}</div>
    <div class="quant-model">${ms.length} model(s) · VRAM-only</div>
    <div class="quant-stat-row"><span class="quant-stat-label">Avg TPS</span><span style="color:var(--accent)">${at.toFixed(1)}</span></div>
    <div class="quant-stat-row"><span class="quant-stat-label">Avg VRAM</span><span>${av.toFixed(2)} GB</span></div>
    <div class="quant-stat-row"><span class="quant-stat-label">Examples</span><span style="color:var(--muted);font-size:9px;text-align:right">${ms.slice(0,2).map(m=>esc(m.short)).join(', ')}</span></div>
  </div>`;
}).join('');
</script>
</body>
</html>
"""


# ──────────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────────

def head_assets(embed) -> str:
    """<link>/<script> tags for fonts + Chart.js. `embed` is None (CDN), '' (download) or a file path."""
    cdn = f'<script src="{CHARTJS_URL}"></script>'
    fonts = f'<link href="{FONTS_URL}" rel="stylesheet">'
    if embed is None:
        return fonts + "\n" + cdn
    try:
        if embed:
            js = Path(embed).read_text(encoding="utf-8")
        else:
            print(f"  Chart.js : downloading {CHARTJS_URL}")
            with urllib.request.urlopen(CHARTJS_URL, timeout=30) as r:
                js = r.read().decode("utf-8")
        if "Chart" not in js[:2000000] or len(js) < 50000:
            raise ValueError("file does not look like Chart.js")
    except Exception as exc:                       # fall back rather than produce a broken report
        print(f"  WARNING : could not embed Chart.js ({exc}); using the CDN instead.")
        return fonts + "\n" + cdn
    print("  Chart.js : embedded (report works offline; system fonts are used)")
    return "<script>" + js.replace("</script", "<\\/script") + "</script>"


def render(models: list, meta: dict, csv_name: str, vram_limit: float, vram_source: str,
           vram_badge: str, min_acc: int, embed) -> str:
    payload = json.dumps(models, indent=1).replace("</", "<\\/").replace("<!--", "<\\!--")
    gpu_badge = (f'<span class="hw-badge"><span class="dot"></span>{html.escape(meta["gpu_name"])}</span>'
                 if meta.get("gpu_name") else "")
    out = HTML_TEMPLATE
    # HEAD_ASSETS is substituted last so the Chart.js source is never scanned for placeholders.
    for key, val in (("{{MODELS_JSON}}", payload), ("{{GPU_BADGE}}", gpu_badge),
                     ("{{CSV_NAME}}", html.escape(csv_name)),
                     ("{{DATE}}", datetime.now().strftime("%Y-%m-%d %H:%M")),
                     ("{{MODEL_COUNT}}", str(len(models))), ("{{VRAM_LIMIT}}", str(vram_limit)),
                     ("{{VRAM_SOURCE}}", vram_source), ("{{VRAM_BADGE_CLASS}}", vram_badge),
                     ("{{MIN_ACC}}", str(min_acc))):
        out = out.replace(key, val)
    return out.replace("{{HEAD_ASSETS}}", head_assets(embed))


def resolve_vram(override, meta: dict, models: list) -> tuple:
    """(limit_gb, source_label, badge_class): --vram > value recorded in CSV > local GPU > inferred."""
    if override:
        return override, "(--vram)", ""
    if meta.get("gpu_vram"):
        return meta["gpu_vram"], "(from CSV)", ""
    detected = detect_vram_gb()
    if detected:
        return detected, "(auto-detected on this machine)", ""
    biggest = max([m["vram"] + m["ram"] for m in models] + [0])
    return max(round(biggest * 1.1), 8), "(inferred from data)", "warn"


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate an HTML dashboard from an Ollama benchmark CSV.")
    ap.add_argument("csv", nargs="?", help="benchmark CSV (default: newest benchmark_results*.csv)")
    ap.add_argument("output", nargs="?", help="output HTML (default: <csv name>_dashboard.html next to the CSV)")
    ap.add_argument("--vram", type=float, help="VRAM limit in GB (overrides CSV/auto-detection)")
    ap.add_argument("--min-accuracy", type=int, default=50, metavar="PCT",
                    help="minimum accuracy %% on graded prompts for the podiums (default 50)")
    ap.add_argument("--embed-chartjs", nargs="?", const="", default=None, metavar="FILE",
                    help="inline Chart.js so the report works offline (downloads it, or reads FILE)")
    args = ap.parse_args()

    try:
        csv_path = Path(args.csv) if args.csv else find_csv(Path(__file__).resolve().parent)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 1
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}")
        return 1
    out_path = Path(args.output) if args.output else csv_path.with_name(csv_path.stem.replace(" ", "_") + "_dashboard.html")

    print(f"\n  CSV    : {csv_path}\n  Output : {out_path}")
    models, meta = parse_csv(csv_path)
    if not models:
        print("ERROR: No usable local model data found in CSV.")
        return 1

    vram_limit, vram_source, vram_badge = resolve_vram(args.vram, meta, models)
    print(f"  VRAM   : {vram_limit} GB {vram_source}")
    print(f"  Models : {len(models)} local models (cloud/API excluded)")

    out_path.write_text(render(models, meta, csv_path.name, vram_limit, vram_source,
                               vram_badge, args.min_accuracy, args.embed_chartjs), encoding="utf-8")
    print(f"\n  Done!  → {out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
