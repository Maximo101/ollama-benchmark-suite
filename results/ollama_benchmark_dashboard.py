#!/usr/bin/env python3
"""
ollama_benchmark_dashboard.py
─────────────────────────────
https://github.com/Maximo101/ollama-benchmark-suite
Reads a benchmark CSV produced by your Ollama benchmarking script and
generates a self-contained HTML dashboard.

Usage
─────
  python ollama_benchmark_dashboard.py                          # auto-detects CSV in same folder
  python ollama_benchmark_dashboard.py my_results.csv          # explicit CSV
  python ollama_benchmark_dashboard.py my_results.csv out.html # explicit CSV + output name

Requirements: Python 3.8+  ·  No third-party packages needed.
"""

import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime


# ──────────────────────────────────────────────────────────────────────────────
# GPU / VRAM detection
# ──────────────────────────────────────────────────────────────────────────────

def detect_vram_gb() -> float:
    """
    Try several platform-specific methods to discover total GPU VRAM (GB).
    Returns the largest VRAM value found across all GPUs, or 0.0 if nothing
    could be determined.

    Priority order:
      1. nvidia-smi          (Linux / Windows / WSL with NVIDIA driver)
      2. rocm-smi            (AMD ROCm on Linux)
      3. wmic / PowerShell   (Windows, any GPU)
      4. /sys sysfs          (Linux fallback)
    """

    # 1 ── NVIDIA-SMI ─────────────────────────────────────────────────────────
    vram = _try_nvidia_smi()
    if vram:
        return vram

    # 2 ── AMD ROCm ────────────────────────────────────────────────────────────
    vram = _try_rocm_smi()
    if vram:
        return vram

    # 3 ── Windows (wmic / PowerShell) ────────────────────────────────────────
    vram = _try_windows()
    if vram:
        return vram

    # 4 ── Linux sysfs drm ────────────────────────────────────────────────────
    vram = _try_sysfs()
    if vram:
        return vram

    return 0.0


def _run(cmd: list[str], timeout: int = 6) -> str:
    """Run a subprocess and return stdout, or '' on any error."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, errors="replace"
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _try_nvidia_smi() -> float:
    out = _run(["nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits"])
    if not out:
        return 0.0
    values = []
    for line in out.splitlines():
        try:
            values.append(float(line.strip()))   # MiB
        except ValueError:
            pass
    return round(max(values) / 1024, 1) if values else 0.0


def _try_rocm_smi() -> float:
    out = _run(["rocm-smi", "--showmeminfo", "vram", "--csv"])
    if not out:
        return 0.0
    # Look for lines like: "0,vram Total Memory (B),17163091968"
    values = []
    for line in out.splitlines():
        m = re.search(r"Total Memory \(B\),(\d+)", line, re.IGNORECASE)
        if m:
            values.append(int(m.group(1)) / (1024 ** 3))
    return round(max(values), 1) if values else 0.0


def _try_windows() -> float:
    # wmic (older Windows)
    out = _run(["wmic", "path", "win32_VideoController",
                "get", "AdapterRAM", "/value"])
    if out:
        values = []
        for line in out.splitlines():
            m = re.search(r"AdapterRAM=(\d+)", line)
            if m:
                val = int(m.group(1))
                if val > 0:
                    values.append(val / (1024 ** 3))
        if values:
            return round(max(values), 1)

    # PowerShell fallback
    ps_cmd = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object -ExpandProperty AdapterRAM"
    )
    out = _run(["powershell", "-NoProfile", "-Command", ps_cmd])
    if out:
        values = []
        for line in out.splitlines():
            try:
                val = int(line.strip())
                if val > 0:
                    values.append(val / (1024 ** 3))
            except ValueError:
                pass
        if values:
            return round(max(values), 1)

    return 0.0


def _try_sysfs() -> float:
    """Read /sys/class/drm/card*/device/mem_info_vram_total (AMD on Linux)."""
    values = []
    base = Path("/sys/class/drm")
    if not base.exists():
        return 0.0
    for p in base.glob("card*/device/mem_info_vram_total"):
        try:
            val = int(p.read_text().strip())
            if val > 0:
                values.append(val / (1024 ** 3))
        except Exception:
            pass
    return round(max(values), 1) if values else 0.0


# ──────────────────────────────────────────────────────────────────────────────
# CSV auto-detection
# ──────────────────────────────────────────────────────────────────────────────

def find_csv(script_dir: Path) -> Path:
    candidates = sorted(script_dir.glob("benchmark_results*.csv"), reverse=True)
    if not candidates:
        candidates = sorted(script_dir.glob("*.csv"), reverse=True)
    if not candidates:
        raise FileNotFoundError(
            "No CSV file found. Pass a path as the first argument."
        )
    return candidates[0]


# ──────────────────────────────────────────────────────────────────────────────
# CSV parsing
# ──────────────────────────────────────────────────────────────────────────────

def _f(v: str) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _avg(lst: list) -> float:
    return sum(lst) / len(lst) if lst else 0.0


def _short_name(raw: str) -> str:
    """Best-effort human-readable name from an Ollama model ID."""
    name = re.sub(r"^hf\.co/[^/]+/", "", raw)   # strip hf.co/author/
    name = re.sub(r":[^:]+$", "", name)           # strip :tag
    if len(name) > 42:
        name = name[:40] + "…"
    return name


def _params_float(s: str) -> float:
    """Convert '9.7B', '27B', '24.5B' → float."""
    try:
        return float(re.sub(r"[^\d.]", "", s))
    except ValueError:
        return 0.0


def is_cloud_model(vram_str: str, ram_str: str) -> bool:
    """
    Cloud / API models have NO VRAM and NO RAM recorded in the CSV
    (both fields are empty or missing entirely). Local models always
    have at least one of these populated because Ollama measures them.
    """
    vram_empty = vram_str.strip() == ""
    ram_empty  = ram_str.strip() in ("", "0", "0.0", "0.00")
    return vram_empty and ram_empty


def parse_csv(csv_path: Path) -> list[dict]:
    """Return one dict per unique *local* model with aggregated stats."""
    raw: dict[str, dict] = {}

    with open(csv_path, encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            model     = row.get("Model", "").strip()
            vram_str  = row.get("VRAM_GB", "")
            ram_str   = row.get("RAM_GB",  "")

            if not model:
                continue

            # ── Exclude cloud / API models ────────────────────────────────────
            if is_cloud_model(vram_str, ram_str):
                continue

            if model not in raw:
                raw[model] = {
                    "name":       model,
                    "params_s":   row.get("Params", "0B"),
                    "quant":      row.get("Quant", "unknown").strip() or "unknown",
                    "caps":       row.get("Capabilities", "completion"),
                    "vram":       _f(vram_str),
                    "ram":        _f(ram_str),
                    "tps_list":   [],
                    "correct":    0,
                    "warm_n":     0,
                    "power_list": [],
            }

            d = raw[model]
            # Keep VRAM/RAM updated (in case first row was COLD_BOOT with values)
            v = _f(vram_str)
            r = _f(ram_str)
            if v > 0:
                d["vram"] = v
            if r > 0:
                d["ram"] = r

            if row.get("Test Type", "") == "WARM":
                tps = _f(row.get("TPS", "0"))
                if tps > 0:
                    d["tps_list"].append(tps)
                d["warm_n"] += 1
                if row.get("Correct", "") in ("Yes", "Yes (JSON)", "Info/Creative"):
                    d["correct"] += 1
                pw = _f(row.get("Power (W)", "0"))
                if pw > 0:
                    d["power_list"].append(pw)

    models = []
    for d in raw.values():
        tps    = _avg(d["tps_list"])
        acc    = round(d["correct"] / d["warm_n"] * 100) if d["warm_n"] else 0
        pw     = _avg(d["power_list"])
        params = _params_float(d["params_s"])

        # Skip models that produced no output
        if tps == 0:
            continue

        models.append({
            "short":  _short_name(d["name"]),
            "params": params,
            "quant":  d["quant"],
            "vram":   round(d["vram"], 2),
            "ram":    round(d["ram"],  2),
            "tps":    round(tps, 1),
            "acc":    acc,
            "power":  round(pw, 1),
            "caps":   d["caps"] or "completion",
        })

    return models


# ──────────────────────────────────────────────────────────────────────────────
# HTML template  (uses {{PLACEHOLDERS}} replaced at render time)
# ──────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ollama Benchmark Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root{--bg:#0a0a0f;--surface:#12121a;--surface2:#1a1a26;--border:#2a2a3e;
  --accent:#7fff6e;--accent2:#ffca3a;--accent3:#ff6b9d;--accent4:#4fc3f7;
  --text:#e8e8f0;--muted:#7878a0;--gold:#FFD700;--silver:#C0C0C0;--bronze:#CD7F32;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;min-height:100vh;overflow-x:hidden;}
body::before{content:'';position:fixed;inset:0;
  background-image:linear-gradient(rgba(127,255,110,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(127,255,110,.03) 1px,transparent 1px);
  background-size:40px 40px;pointer-events:none;z-index:0;}
.container{position:relative;z-index:1;max-width:1280px;margin:0 auto;padding:0 24px 60px;}
header{padding:48px 0 36px;border-bottom:1px solid var(--border);margin-bottom:40px;}
.header-label{font-family:'Space Mono',monospace;font-size:11px;color:var(--accent);letter-spacing:3px;text-transform:uppercase;margin-bottom:12px;}
h1{font-size:clamp(28px,5vw,52px);font-weight:800;line-height:1.05;letter-spacing:-1px;}
h1 span{color:var(--accent);}
.hw-badge{display:inline-flex;align-items:center;gap:8px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;padding:6px 14px;font-family:'Space Mono',monospace;font-size:11px;color:var(--muted);margin-top:16px;margin-right:8px;}
.hw-badge .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);flex-shrink:0;}
.hw-badge.warn .dot{background:var(--accent2);}
.section-title{font-size:11px;font-family:'Space Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:3px;margin-bottom:8px;display:flex;align-items:center;gap:12px;}
.section-title::after{content:'';flex:1;height:1px;background:var(--border);}
.section-subtitle{font-size:11px;color:var(--muted);font-family:'Space Mono',monospace;margin-bottom:20px;opacity:.8;}
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
.rank-badge{position:absolute;top:20px;right:20px;width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-family:'Space Mono',monospace;font-size:14px;font-weight:700;}
.rank-1 .rank-badge{background:rgba(255,215,0,.15);color:var(--gold);}
.rank-2 .rank-badge{background:rgba(192,192,192,.15);color:var(--silver);}
.rank-3 .rank-badge{background:rgba(205,127,50,.15);color:var(--bronze);}
.model-name{font-size:15px;font-weight:700;margin-bottom:4px;padding-right:44px;line-height:1.3;}
.model-sub{font-family:'Space Mono',monospace;font-size:10px;color:var(--muted);margin-bottom:20px;}
.stat-row{display:flex;justify-content:space-between;margin-bottom:10px;font-size:13px;}
.stat-label{color:var(--muted);font-family:'Space Mono',monospace;font-size:10px;}
.stat-value{font-family:'Space Mono',monospace;font-weight:700;}
.tps-big{font-size:36px;font-weight:800;margin:12px 0 4px;letter-spacing:-1px;}
.rank-1 .tps-big{color:var(--gold);}
.rank-2 .tps-big{color:var(--silver);}
.rank-3 .tps-big{color:var(--bronze);}
.tps-unit{font-size:13px;color:var(--muted);font-weight:400;}
.pill{display:inline-block;padding:2px 8px;border-radius:3px;font-family:'Space Mono',monospace;font-size:10px;background:rgba(127,255,110,.1);color:var(--accent);border:1px solid rgba(127,255,110,.2);margin-top:8px;margin-right:4px;}
.pill.warn{background:rgba(255,202,58,.1);color:var(--accent2);border-color:rgba(255,202,58,.2);}
.pill.info{background:rgba(79,195,247,.1);color:var(--accent4);border-color:rgba(79,195,247,.2);}
.pill.pink{background:rgba(255,107,157,.1);color:var(--accent3);border-color:rgba(255,107,157,.2);}
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:48px;}
.chart-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:24px;}
.chart-card.wide{grid-column:1/-1;}
.chart-title{font-size:13px;font-weight:600;margin-bottom:6px;}
.chart-desc{font-family:'Space Mono',monospace;font-size:10px;color:var(--muted);margin-bottom:20px;}
.cap-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:20px;margin-bottom:48px;}
.cap-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:24px;}
.cap-label{font-family:'Space Mono',monospace;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--accent4);margin-bottom:4px;}
.cap-title{font-size:14px;font-weight:700;margin-bottom:4px;}
.cap-desc{font-family:'Space Mono',monospace;font-size:10px;color:var(--muted);margin-bottom:16px;}
.table-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:48px;overflow-x:auto;}
table{width:100%;border-collapse:collapse;min-width:900px;}
thead th{background:var(--surface2);padding:12px 14px;font-family:'Space Mono',monospace;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;text-align:left;border-bottom:1px solid var(--border);cursor:pointer;user-select:none;white-space:nowrap;transition:color .15s;}
thead th:hover{color:var(--accent);}
thead th .sort-icon{margin-left:5px;opacity:.5;font-size:10px;}
thead th.sort-asc .sort-icon::after{content:'▲';color:var(--accent);opacity:1;}
thead th.sort-desc .sort-icon::after{content:'▼';color:var(--accent);opacity:1;}
thead th:not(.sort-asc):not(.sort-desc) .sort-icon::after{content:'⇅';}
tbody tr{border-bottom:1px solid rgba(42,42,62,.5);transition:background .15s;}
tbody tr:hover{background:var(--surface2);}
tbody tr:last-child{border-bottom:none;}
tbody td{padding:10px 14px;font-size:12px;font-family:'Space Mono',monospace;}
.vram-bar{height:4px;background:var(--border);border-radius:2px;margin-top:4px;overflow:hidden;}
.vram-bar-fill{height:100%;border-radius:2px;}
.vram-ok{background:var(--accent);}
.vram-warn{background:var(--accent2);}
.vram-over{background:var(--accent3);}
.acc-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle;}
.quant-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:48px;}
.quant-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px;}
.quant-name{font-family:'Space Mono',monospace;font-size:16px;font-weight:700;color:var(--accent4);margin-bottom:4px;}
.quant-model{font-size:11px;color:var(--muted);margin-bottom:14px;font-family:'Space Mono',monospace;}
.quant-stat-row{display:flex;justify-content:space-between;margin-bottom:8px;font-size:12px;font-family:'Space Mono',monospace;}
.quant-stat-label{color:var(--muted);}
.footer{text-align:center;font-family:'Space Mono',monospace;font-size:10px;color:var(--muted);padding-top:24px;border-top:1px solid var(--border);}
@media(max-width:768px){.podium-grid,.chart-grid,.cap-grid,.quant-grid{grid-template-columns:1fr;}.chart-card.wide{grid-column:auto;}}
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
    <span class="hw-badge {{VRAM_BADGE_CLASS}}"><span class="dot"></span>VRAM limit {{VRAM_LIMIT}} GB {{VRAM_SOURCE}}</span>
  </div>
</header>

<div class="section-title">Top 3 — Goldilocks (Best Overall)</div>
<p class="section-subtitle">Highest combined score of parameters × tokens-per-second — the most capable <em>and</em> fastest VRAM-only models</p>
<div class="podium-grid" id="goldilocksGrid"></div>

<div class="section-title">Top 3 — Agent Use (Tool-Calling)</div>
<p class="section-subtitle">Best tool/function-calling models — VRAM-only, ranked by params × TPS</p>
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
    <div class="chart-desc">green = VRAM-only · red = RAM spill · line = {{VRAM_LIMIT}} GB limit</div>
    <canvas id="scatter"></canvas>
  </div>
  <div class="chart-card">
    <div class="chart-title">Accuracy vs Speed</div>
    <div class="chart-desc">gold = Goldilocks top-3 · teal = Agent top-3</div>
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
      <th data-col="6" data-type="num">Accuracy <span class="sort-icon"></span></th>
      <th data-col="7" data-type="num">Power W <span class="sort-icon"></span></th>
      <th data-col="8" data-type="str">Capabilities <span class="sort-icon"></span></th>
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

const valid = MODELS.filter(m => m.tps > 0 && m.ram === 0);
const allV  = MODELS.filter(m => m.tps > 0);

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
function goldiScore(m) { return m.params * m.tps; }

const ax = t => ({
  ticks: { color:'#7878a0', font:{ family:'Space Mono', size:9 } },
  grid:  { color:'rgba(42,42,62,.4)' },
  title: { display:!!t, text:t||'', color:'#7878a0', font:{ family:'Space Mono', size:10 } }
});

function capPillsHtml(caps, small) {
  return caps.split(',').map(c => {
    const cl = c==='tools'?'warn': c==='thinking'?'pink': c==='vision'?'info':'';
    const st = small ? 'margin-top:0;padding:1px 5px;font-size:9px' : '';
    return `<span class="pill ${cl}" style="${st}">${c}</span>`;
  }).join('');
}

// ── PODIUM ────────────────────────────────────────────────────────────────────
function buildPodium(id, list, badge) {
  const g = document.getElementById(id);
  list.slice(0, 3).forEach((m, i) => {
    const ranks = ['rank-1','rank-2','rank-3'], nums = ['#1','#2','#3'];
    g.innerHTML += `
    <div class="podium-card ${ranks[i]}">
      <div class="rank-badge">${nums[i]}</div>
      <div class="model-name">${m.short}</div>
      <div class="model-sub">${m.quant} · ${m.params}B params</div>
      <div class="tps-big">${m.tps.toFixed(1)} <span class="tps-unit">tok/s</span></div>
      <div class="stat-row"><span class="stat-label">VRAM Used</span><span class="stat-value" style="color:var(--accent)">${m.vram.toFixed(2)} GB</span></div>
      <div class="stat-row"><span class="stat-label">Accuracy</span><span class="stat-value" style="color:${m.acc===100?'var(--accent)':'var(--accent2)'}">${m.acc}%</span></div>
      <div class="stat-row"><span class="stat-label">Power</span><span class="stat-value">${m.power.toFixed(1)} W</span></div>
      <span class="pill">${badge}</span>${capPillsHtml(m.caps)}
    </div>`;
  });
}

// Goldilocks: VRAM-only, min 67% accuracy, sorted by params × TPS descending
const goldilocks = valid
  .filter(m => m.acc >= 67)
  .sort((a, b) => goldiScore(b) - goldiScore(a));

// Agents: VRAM-only, has tools, min 67% accuracy, sorted by params × TPS
const agents = valid
  .filter(m => hasCap(m, 'tools') && m.acc >= 67)
  .sort((a, b) => goldiScore(b) - goldiScore(a));

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
new Chart(document.getElementById('scatter'), {
  type: 'bubble',
  data: { datasets: [
    { label:'VRAM-only',
      data: allV.filter(m=>m.ram===0).map(m=>({x:m.vram,y:m.tps,r:Math.sqrt(m.params)*2.5,label:m.short})),
      backgroundColor:'rgba(127,255,110,.35)', borderColor:'#7fff6e', borderWidth:1 },
    { label:'RAM spill',
      data: allV.filter(m=>m.ram>0).map(m=>({x:m.vram,y:m.tps,r:Math.sqrt(m.params)*2.5,label:m.short})),
      backgroundColor:'rgba(255,107,157,.35)', borderColor:'#ff6b9d', borderWidth:1 }
  ]},
  options: { responsive:true,
    plugins:{ legend:{labels:{color:'#7878a0',font:{family:'Space Mono',size:10}}},
      tooltip:{callbacks:{label:c=>` ${c.raw.label}: ${c.raw.x}GB / ${c.raw.y.toFixed(1)} TPS`}} },
    scales:{ x:{...ax('VRAM (GB)'),min:0,max:VRAM_LIMIT+1}, y:ax('Avg TPS') } }
});

// ── ACC vs TPS SCATTER ────────────────────────────────────────────────────────
const g3 = goldilocks.slice(0,3).map(m=>m.short);
const a3  = agents   .slice(0,3).map(m=>m.short);
new Chart(document.getElementById('accScatter'), {
  type: 'scatter',
  data: { datasets:[{ label:'Models',
    data: valid.map(m=>({x:m.tps,y:m.acc,label:m.short})),
    backgroundColor: valid.map(m=> g3.includes(m.short)?'#ffd700cc': a3.includes(m.short)?'#4fc3f7cc':'rgba(127,127,180,.5)'),
    borderColor:     valid.map(m=> g3.includes(m.short)?'#ffd700':  a3.includes(m.short)?'#4fc3f7':'rgba(127,127,180,.7)'),
    pointRadius:     valid.map(m=> g3.includes(m.short)||a3.includes(m.short)?9:5),
    borderWidth:     valid.map(m=> g3.includes(m.short)||a3.includes(m.short)?2:1),
  }]},
  options:{ responsive:true,
    plugins:{legend:{display:false}, tooltip:{callbacks:{label:c=>` ${c.raw.label}: ${c.raw.y}% / ${c.raw.x.toFixed(1)} TPS`}}},
    scales:{ x:ax('Avg TPS'), y:{...ax('Accuracy %'),min:50,max:105} } }
});

// ── QUANT BAR ─────────────────────────────────────────────────────────────────
const qMap = {};
valid.forEach(m=>{ if(!qMap[m.quant])qMap[m.quant]=[]; qMap[m.quant].push(m.tps); });
const qL = Object.keys(qMap).sort();
const qA = qL.map(q => qMap[q].reduce((a,b)=>a+b,0)/qMap[q].length);
const qC = ['#7fff6e','#4fc3f7','#ffca3a','#ff6b9d','#c88dff','#ff9d4f','#aaffee'];
new Chart(document.getElementById('quantBar'), {
  type:'bar',
  data:{ labels:qL, datasets:[{ label:'Avg TPS', data:qA,
    backgroundColor: qC.slice(0,qL.length).map(c=>c+'cc'),
    borderColor:     qC.slice(0,qL.length), borderWidth:1, borderRadius:4 }] },
  options:{ responsive:true,
    plugins:{legend:{display:false}, tooltip:{callbacks:{label:c=>` avg ${c.raw.toFixed(1)} TPS (n=${qMap[qL[c.dataIndex]].length})`}}},
    scales:{ x:ax(), y:ax('Avg TPS') } }
});

// ── EFFICIENCY BAR ────────────────────────────────────────────────────────────
const eff = [...valid].filter(m=>m.power>0)
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
        tooltip:{callbacks:{label:c=>` ${c.raw.label}\n  ${c.raw.x}B · ${c.raw.y.toFixed(1)} TPS · ${c.raw.acc}% acc`}} },
      scales:{ x:{...ax('Parameters (B)'),min:0}, y:{...ax('Avg TPS'),min:0} } }
  });
}
capScatter('capCompletion', m=>hasCap(m,'completion'), '#7fff6e');
capScatter('capTools',      m=>hasCap(m,'tools'),      '#ffca3a');
capScatter('capThinking',   m=>hasCap(m,'thinking'),   '#c88dff');
capScatter('capVision',     m=>hasCap(m,'vision'),     '#4fc3f7');

// ── FULL TABLE ────────────────────────────────────────────────────────────────
function renderTable(data) {
  const tb = document.getElementById('fullTable');
  tb.innerHTML = '';
  data.forEach(m => {
    const vp = Math.min((m.vram / VRAM_LIMIT) * 100, 100);
    const vc = m.ram > 0 ? 'vram-over' : m.vram > VRAM_LIMIT * 0.88 ? 'vram-warn' : 'vram-ok';
    const ac = m.acc===100?'var(--accent)': m.acc>=83?'var(--accent2)':'var(--accent3)';
    tb.innerHTML += `<tr
      data-0="${m.short}" data-1="${m.params}" data-2="${m.quant}"
      data-3="${m.vram}"  data-4="${m.ram}"    data-5="${m.tps}"
      data-6="${m.acc}"   data-7="${m.power}"  data-8="${m.caps}">
      <td style="color:var(--text);font-size:11px">${m.short}</td>
      <td>${m.params}B</td>
      <td style="color:var(--accent4)">${m.quant}</td>
      <td>${m.vram.toFixed(2)}<div class="vram-bar"><div class="vram-bar-fill ${vc}" style="width:${vp}%"></div></div></td>
      <td style="color:${m.ram>0?'var(--accent3)':'var(--muted)'}">${m.ram>0?m.ram.toFixed(2)+' ⚠':'—'}</td>
      <td style="font-weight:700;color:${tpsColor(m.tps)}">${m.tps.toFixed(1)}</td>
      <td><span class="acc-dot" style="background:${ac}"></span>${m.acc}%</td>
      <td style="color:var(--muted)">${m.power.toFixed(1)}</td>
      <td>${capPillsHtml(m.caps, true)}</td>
    </tr>`;
  });
}

const initData = [...MODELS].filter(m=>m.tps>0).sort((a,b)=>a.ram-b.ram||b.tps-a.tps);
renderTable(initData);

// Column sort
let sCol = null, sDir = 1;
document.querySelectorAll('#mainTable thead th').forEach(th => {
  th.addEventListener('click', () => {
    const col  = parseInt(th.dataset.col);
    const type = th.dataset.type;
    sDir = sCol === col ? -sDir : 1;
    sCol = col;
    document.querySelectorAll('#mainTable thead th').forEach(h => h.classList.remove('sort-asc','sort-desc'));
    th.classList.add(sDir === 1 ? 'sort-asc' : 'sort-desc');
    const rows = [...document.querySelectorAll('#mainTable tbody tr')];
    rows.sort((a, b) => {
      let av = a.dataset[col], bv = b.dataset[col];
      if (type === 'num') { av = parseFloat(av)||0; bv = parseFloat(bv)||0; return (av-bv)*sDir; }
      return av.localeCompare(bv) * sDir;
    });
    const tb = document.getElementById('fullTable');
    rows.forEach(r => tb.appendChild(r));
  });
});

// ── QUANT CARDS ───────────────────────────────────────────────────────────────
[
  { label:'Q4_K_M',      models: valid.filter(m=>m.quant==='Q4_K_M') },
  { label:'Q8_0',        models: valid.filter(m=>m.quant==='Q8_0') },
  { label:'F16 / MXFP4',models: valid.filter(m=>['F16','MXFP4'].includes(m.quant)) },
].forEach(({label, models:ms}) => {
  if (!ms.length) return;
  const at = ms.reduce((a,b)=>a+b.tps,0)/ms.length;
  const av = ms.reduce((a,b)=>a+b.vram,0)/ms.length;
  document.getElementById('quantGrid').innerHTML += `
  <div class="quant-card">
    <div class="quant-name">${label}</div>
    <div class="quant-model">${ms.length} model(s) · VRAM-only</div>
    <div class="quant-stat-row"><span class="quant-stat-label">Avg TPS</span><span style="color:var(--accent)">${at.toFixed(1)}</span></div>
    <div class="quant-stat-row"><span class="quant-stat-label">Avg VRAM</span><span>${av.toFixed(2)} GB</span></div>
    <div class="quant-stat-row"><span class="quant-stat-label">Examples</span><span style="color:var(--muted);font-size:9px">${ms.slice(0,2).map(m=>m.short).join(', ')}</span></div>
  </div>`;
});
</script>
</body>
</html>
"""


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    script_dir = Path(__file__).parent

    # ── Resolve CSV path ──────────────────────────────────────────────────────
    if len(sys.argv) >= 2:
        csv_path = Path(sys.argv[1])
    else:
        csv_path = find_csv(script_dir)

    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}")
        sys.exit(1)

    # ── Resolve output path ───────────────────────────────────────────────────
    if len(sys.argv) >= 3:
        out_path = Path(sys.argv[2])
    else:
        stem     = csv_path.stem.replace(" ", "_")
        out_path = script_dir / f"{stem}_dashboard.html"

    print(f"\n  CSV    : {csv_path}")
    print(f"  Output : {out_path}")

    # ── Detect GPU VRAM ───────────────────────────────────────────────────────
    detected_vram = detect_vram_gb()
    if detected_vram > 0:
        vram_limit  = detected_vram
        vram_source = "(auto-detected)"
        vram_badge  = ""
        print(f"  VRAM   : {vram_limit} GB  ← detected via GPU query")
    else:
        # Fall back: infer from the largest VRAM value seen in the CSV data
        # (parse once quickly just to find max VRAM)
        vram_limit  = 0.0
        vram_source = "(inferred from data)"
        vram_badge  = "warn"
        try:
            with open(csv_path, encoding="utf-8-sig", errors="replace") as f:
                for row in csv.DictReader(f):
                    v = _f(row.get("VRAM_GB", ""))
                    if v > vram_limit:
                        vram_limit = v
        except Exception:
            pass
        vram_limit = max(round(vram_limit * 1.1), 8)  # add 10% headroom, min 8
        print(f"  VRAM   : {vram_limit} GB  ← no GPU detected, inferred from CSV data")

    # ── Parse models ──────────────────────────────────────────────────────────
    models = parse_csv(csv_path)
    if not models:
        print("ERROR: No usable local model data found in CSV.")
        sys.exit(1)

    print(f"  Models : {len(models)} local models (cloud/API excluded)")

    # ── Render HTML ───────────────────────────────────────────────────────────
    html = HTML_TEMPLATE
    html = html.replace("{{CSV_NAME}}",       csv_path.name)
    html = html.replace("{{DATE}}",           datetime.now().strftime("%Y-%m-%d %H:%M"))
    html = html.replace("{{MODEL_COUNT}}",    str(len(models)))
    html = html.replace("{{VRAM_LIMIT}}",     str(vram_limit))
    html = html.replace("{{VRAM_SOURCE}}",    vram_source)
    html = html.replace("{{VRAM_BADGE_CLASS}}",vram_badge)
    html = html.replace("{{MODELS_JSON}}",    json.dumps(models, indent=2))

    out_path.write_text(html, encoding="utf-8")
    print(f"\n  Done!  → {out_path}\n")


if __name__ == "__main__":
    main()
