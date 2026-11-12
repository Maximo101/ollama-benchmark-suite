# Ollama Benchmark & Maintenance Suite

A small set of scripts to keep local [Ollama](https://ollama.com/) models up to date, inventory them, and benchmark them under strict, repeatable conditions. Built for Linux / Unraid with Ollama in Docker, but it works equally well against a native install or a remote server.

Everything talks to the **Ollama HTTP API** — no scraping of `ollama list` / `ollama show` output, and no hard dependency on Docker.

## 📦 What's in the box

| File | Purpose |
|---|---|
| `ollama_benchmark.sh` | Cold-boot + warm-prompt benchmark → timestamped CSV |
| `update_ollama_models.sh` | Pulls the latest version of every installed model → CSV report |
| `ollama_inventory.py` | CSV inventory of installed models (size, params, quant, context, capabilities, licence) |
| `ollama_benchmark_dashboard.py` | Turns a benchmark CSV into an interactive HTML report |
| `METRICS.md` | Definition of every CSV column and how answers are graded |

All output goes to `results/`.

## 🚀 Quick start

```bash
git clone https://github.com/Maximo101/ollama-benchmark-suite.git
cd ollama-benchmark-suite
chmod +x *.sh

./update_ollama_models.sh      # optional: bring models up to date first
sudo ./ollama_benchmark.sh     # root lets it flush the page cache (see below)

cd results
python3 ollama_benchmark_dashboard.py   # writes <csv name>_dashboard.html next to the CSV
```

**Requirements:** `bash`, `curl`, `jq`, `awk` · `nvidia-smi` (optional, for power and VRAM size) · Python 3.8+ for the inventory and dashboard (standard library only).

### Finding Ollama

All scripts locate the server the same way: `OLLAMA_URL` if set → the IP of a running Docker container named `ollama` → `http://localhost:11434`.

```bash
OLLAMA_URL=http://192.168.1.50:11434 ./ollama_benchmark.sh    # remote server
OLLAMA_CONTAINER=ollama-gpu ./ollama_benchmark.sh              # different container name
```

> Cache flushing and `nvidia-smi` power sampling act on the machine running the *script*. For meaningful cold-boot and power numbers, run it on the Ollama host.

## 🧪 `ollama_benchmark.sh`

```
./ollama_benchmark.sh [options]
  -m, --models REGEX    only benchmark models whose name matches REGEX
  -x, --exclude REGEX   skip models whose name matches REGEX
  -r, --runs N          run the prompt set N times per model (default 1)
      --no-flush        do not drop the Linux page cache between models
      --include-cloud   also benchmark cloud/remote models (skipped by default)
  -h, --help
```

| Variable | Default | Meaning |
|---|---|---|
| `OLLAMA_URL` / `OLLAMA_CONTAINER` | auto | Where to find Ollama |
| `NUM_PREDICT` | `4096` | Max tokens per answer (stops runaway generation; flagged in the CSV if hit) |
| `KEEP_ALIVE` | `10m` | How long the model stays loaded during its own run |
| `GPU_IDS` | all | Comma-separated GPU indexes to sample for power |
| `RESULTS_OWNER` | `nobody:users` on Unraid | `chown` the results folder (root only) |

Examples:

```bash
./ollama_benchmark.sh -m 'qwen3|gemma3'         # just those families
./ollama_benchmark.sh -x '70b|72b' --runs 3     # skip huge models, 3 passes for steadier averages
```

Models without the `completion` capability (embedding models) and cloud models are skipped automatically.

### How it works

Standard benchmarking suffers from "warm cache" bias — the second query is faster simply because the weights are already in memory. For each model the script therefore:

1. **Hard-purges** every loaded model through the API and waits (with a timeout) until `/api/ps` is empty.
2. **Flushes the Linux page cache** (`sync` + `drop_caches`) so the load must read from disk (NVMe/SSD/HDD). Needs root or passwordless sudo; if unavailable it carries on and records `Cache_Flushed = no`.
3. **Times the cold boot** with an empty-prompt request that only loads the model, recording wall-clock time and Ollama's `load_duration`.
4. **Detects the VRAM / RAM split** from `/api/ps`. A model that spills into system RAM is run anyway, but is flagged so you can interpret its lower t/s.
5. **Discards one tiny warm-up generation** so first-inference overhead doesn't skew prompt 1.
6. **Runs the six prompts**, measuring tokens/s, prefill speed, approximate time-to-first-token, and **average GPU power sampled every 250 ms while the request is in flight**.
7. **Unloads the model** before moving on.

Generation is deterministic-ish: `temperature=0.1`, `seed=42`.

### Prompts and grading

Four prompts are auto-graded (letter counting, the bat-and-ball problem, a logic puzzle, strict JSON output); two (identity, a 50-word explanation) are informational. Grading looks only at the model's **final answer**, never at text it merely echoes from the question, and it is a heuristic — read the `Response` column before drawing conclusions about a specific model. Details in [`METRICS.md`](METRICS.md).

## 🔄 `update_ollama_models.sh`

```
./update_ollama_models.sh [-m REGEX] [-x REGEX] [-n|--dry-run]
```

Pulls every installed model through the API and decides what happened by comparing the model **digest before and after** — more reliable than parsing progress output. Each model ends as `Updated`, `Already up to date`, `Update Failed`, `Not in registry` (locally created models), or `Skipped (cloud)`. The exit status is non-zero if any pull failed, so it is safe in cron.

## 📋 `ollama_inventory.py`

```bash
python3 ollama_inventory.py                      # auto-detect server
python3 ollama_inventory.py -u http://nas:11434  # explicit server
```

## 📊 Dashboard

`ollama_benchmark_dashboard.py` generates a single HTML file with podium rankings (Goldilocks = bigger *and* faster; Agent = tool-calling models), TPS, accuracy, quantisation, power-efficiency, VRAM-vs-RAM and **cold-load-time** charts, plus a sortable comparison table.

```bash
python3 ollama_benchmark_dashboard.py                                       # newest results/benchmark_results*.csv
python3 ollama_benchmark_dashboard.py results/my_results.csv                # explicit CSV
python3 ollama_benchmark_dashboard.py results/my_results.csv report.html    # explicit CSV + output name
python3 ollama_benchmark_dashboard.py --vram 24                             # override the VRAM limit (GB)
python3 ollama_benchmark_dashboard.py --min-accuracy 75                     # stricter podium filter
python3 ollama_benchmark_dashboard.py --embed-chartjs                       # fully offline report
```

The report is saved next to the CSV (in `results/`). Open the `.html` file in a browser.

* The VRAM limit comes from the CSV (`GPU_VRAM_GB`, recorded on the benchmark machine), then `--vram`/local GPU detection, then the data itself.
* By default the report loads Chart.js and fonts from a CDN, so it needs internet **when viewed**. `--embed-chartjs` inlines Chart.js (downloaded once at generation time, or pass a local file) and uses system fonts.
* Only models fully resident in VRAM (`RAM_GB = 0`) are ranked; spill models still appear in the table and VRAM chart. On a CPU-only machine nothing qualifies, so the podiums will be empty.
* The dashboard still reads older CSVs; columns that did not exist then (cold load, prefill) simply show `—`.

## 🆕 What changed in v2

**Breaking / comparability**

* `ollama_benchmark_v1.sh` → **`ollama_benchmark.sh`**; the version lives in the script and CSV name (`benchmark_results_v2_…`).
* **Prompt 3 was replaced.** The original puzzle had four valid answers (chef, doctor, engineer or artist could be in the middle), so "engineer" was an arbitrary expectation. The new puzzle has exactly one solution. Prompt-3 results are not comparable with v1 CSVs.
* Accuracy is now computed over **graded prompts only**. Previously the two ungraded prompts counted as correct, inflating every score by up to a third. The podium threshold (`--min-accuracy`, default 50) is equivalent to the old 67 % bar.
* `ollama_inventory.sh` (which was Python) is now `ollama_inventory.py`.

**Fixes**

* Power was the average of two samples taken *before* and *after* the request (≈ idle). It is now sampled continuously during generation.
* The updater read the exit code of `tee`, so failed pulls were reported as successes.
* Dashboard: `270M` models were parsed as 270 B parameters; `qwen3:8b` and `qwen3:14b` collapsed to the same label; model names were injected into the page unescaped; `list[str]` annotations crashed Python 3.8.
* The old benchmark chmod'ed the **whole script directory** to 777; only `results/` is touched now, and only when running as root.
* The answer check for prompt 1 matched any "3" anywhere; checks now inspect the final answer, JSON is validated with `jq`, and truncated answers are flagged instead of silently graded.
* `jq`-built JSON and CSV (proper quoting, no `printf` escaping), `/api/show` instead of CLI scraping, `while read` no longer shares stdin with child processes, and the unload-wait loop has a timeout.

**New**

* Filters (`-m`, `-x`), `--runs`, `--dry-run`, `--no-flush`, cloud-model skipping, Docker-optional operation.
* New CSV columns: `Load (s)`, `TTFT (s)`, `Prefill TPS`, `Eval Count`, `GPU`, `GPU_VRAM_GB`, `Cache_Flushed`.
* Dashboard: cold-load chart and table columns, prefill speed, per-quant cards for every quantisation present, offline mode.

## ⚠️ Limitations worth knowing

* **TTFT is approximate** (`load + prompt eval`); requests are non-streaming.
* **Power** is total board power of the sampled GPUs, not energy per token, and short answers yield few samples.
* One pass is noisy; use `--runs 3` or more when comparing close contenders.
* Thinking models' `Eval Count` and TPS include thinking tokens.
* Goldilocks ranking uses total parameters, which favours MoE models relative to their active size.

## Reference

[Ollama](https://ollama.com/) · [Ollama API docs](https://github.com/ollama/ollama/blob/main/docs/api.md)

## Dashboard example

<img width="1328" height="656" alt="b1" src="https://github.com/user-attachments/assets/f735248c-72f2-4a68-8f4a-04bb343be5c0" />

<img width="1328" height="452" alt="b2" src="https://github.com/user-attachments/assets/e5f13570-fc2b-4751-9f8c-c15246a16263" />

<img width="1328" height="786" alt="b3" src="https://github.com/user-attachments/assets/10503e71-bdb4-42a6-98f9-5afb1d300b7e" />

<img width="1328" height="808" alt="b4" src="https://github.com/user-attachments/assets/d5d6fd42-4979-4c00-b7e6-e637a6a5dc1d" />

<img width="1328" height="808" alt="b5" src="https://github.com/user-attachments/assets/f182bece-f30d-4617-b53a-a9eaea42d2bf" />
