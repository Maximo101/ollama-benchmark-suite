# Explanation of Metrics

Reference for every column the suite writes. All sizes labelled "GB" are **GiB (1024³ bytes)** so that file sizes, VRAM usage and the GPU's reported memory are directly comparable.

## Benchmark CSV (`ollama_benchmark.sh` → `results/benchmark_results_v2_*.csv`)

Each model produces one `COLD_BOOT` row followed by one `WARM` row per prompt (per run).

### Model description (same on every row of a model)

| Column | Meaning |
|---|---|
| **Model** | Model name and tag as shown by `ollama list`. |
| **Size (GB)** | Size of the model files on disk. |
| **Arch** | Architecture family (`llama`, `qwen3`, `gemma3`, …). |
| **Params** | Parameter count as reported by Ollama (e.g. `8.2B`, `270M`). |
| **Context** | Maximum context length the model supports (not the context Ollama actually allocates). |
| **Embed** | Embedding (hidden) dimension. |
| **Quant** | Quantisation level (`Q4_K_M`, `Q8_0`, `F16`, …). |
| **Capabilities** | Comma-separated capability tags: `completion`, `tools`, `thinking`, `vision`, … |
| **Temp** | The model's *default* temperature from its Modelfile. Benchmark prompts are run at 0.1 regardless. |
| **License** | First non-blank line of the model's license text. |

### Run environment

| Column | Meaning |
|---|---|
| **VRAM_GB** | Model memory resident in GPU VRAM after the cold load (`size_vram` from `/api/ps`). |
| **RAM_GB** | Model memory that spilled into system RAM (`size − size_vram`). **Anything above 0 means partial CPU offload** and usually explains a sudden TPS drop. |
| **GPU** | GPU name(s) from `nvidia-smi`, `none` if unavailable. |
| **GPU_VRAM_GB** | Total VRAM of the sampled GPU(s). The dashboard uses this as its VRAM limit. |
| **Cache_Flushed** | `yes` if the Linux page cache was dropped before the cold load, `no` if not (not root, `--no-flush`, …). A `no` means cold-load times may be optimistic. |

### Measurements

| Column | `COLD_BOOT` row | `WARM` row |
|---|---|---|
| **Test Type** | `COLD_BOOT` | `WARM` |
| **Prompt** | `Load Test` | The prompt text. |
| **Correct** | `N/A` | `Yes`, `Yes (JSON)`, `No`, or `Info/Creative` (ungraded prompt). |
| **Latency (s)** | Wall-clock time from sending the load request until Ollama answered. Includes reading the weights from storage, so it is only a true cold figure when `Cache_Flushed` is `yes`. | Server-reported `total_duration` for the whole request. |
| **Load (s)** | Server-reported `load_duration` for the load. | `load_duration` for the request — about 0 once the model is resident. |
| **TTFT (s)** | – | **Approximate** time to first token: `load_duration + prompt_eval_duration`. Requests are non-streaming, so this is derived, not measured. |
| **Prefill TPS** | – | Prompt-processing speed: `prompt_eval_count ÷ prompt_eval_duration`. |
| **Eval Count** | – | Tokens generated (for thinking models this includes thinking tokens). |
| **TPS** | – | **Generation speed**: `eval_count ÷ eval_duration`. The headline number. |
| **Power (W)** | – | Average **total** GPU power (sum across sampled GPUs) taken from `nvidia-smi` samples every 250 ms *while the request was running*. 0 if no NVIDIA GPU. |
| **Response** | `Loaded` or `Failed` | The answer, flattened to one line and cut at 1000 characters. If the model produced no final answer (e.g. a thinking model hit the token cap) it says so here. |

Fixed generation settings for warm prompts: `temperature=0.1`, `seed=42`, `num_predict=4096` (override with `NUM_PREDICT`).

## How answers are graded

Grading is **heuristic** and looks only at the final answer (text after any `</think>`, last ~600 characters), so echoing the question does not earn credit.

| # | Prompt | Counts as correct when… |
|---|---|---|
| 1 | Letters in "Strawberry" | the answer says **3** / "three" |
| 2 | Bat and ball | the answer says **$0.05** / "5 cents" |
| 3 | Five people in a line | the **chef** is named as the middle person (rewritten in v2 — see README) |
| 4 | Smart-home JSON | the output contains a valid JSON object with keys `action`, `entity`, `color`, `condition` and `color` = blue |
| 5, 6 | Identity / Simulation Hypothesis | never graded (`Info/Creative`) |

Dashboard **Accuracy** = correct ÷ *graded* prompts (1–4). Ungraded prompts are excluded.

## Update report (`update_ollama_models.sh` → `results/ollama_models_report_*.csv`)

| Column | Meaning |
|---|---|
| **Model Name** | Installed model. |
| **Model ID** | First 12 characters of the model digest *after* the update. |
| **Size (GB)** | On-disk size after the update. |
| **Local Modified Date** | Last-modified time Ollama records for the model (after the update). |
| **Library Status** | `Ollama` (registry), `Hugging Face` (`hf.co/…`) or `Cloud`. |
| **Update Status** | `Updated` (digest changed), `Already up to date`, `Update Failed`, `Not in registry` (locally created models), `Skipped (cloud)`, `Dry run`. |
| **Notes** | Digest change (`old -> new`) or the error message. |

## Inventory (`ollama_inventory.py` → `results/ollama_inventory_*.csv`)

`Model, Size (GB), Arch, Params, Context, Embed, Quant, Capabilities, Temp, License` — same meaning as the benchmark columns above (capabilities are separated by ` | `).

## Dashboard-derived metrics

| Metric | Definition |
|---|---|
| **Avg TPS** | Mean `TPS` over a model's warm prompts (failed prompts excluded). |
| **Accuracy** | Correct ÷ graded prompts, in percent. |
| **Goldilocks score** | `params × Avg TPS` — rewards bigger *and* faster models. Only VRAM-only models (`RAM_GB = 0`) with accuracy ≥ `--min-accuracy` (default 50) are ranked. MoE models are scored on total, not active, parameters. |
| **TPS / Watt** | `Avg TPS ÷ Power (W)`. |
| **Cold Load s** | `Latency (s)` of the `COLD_BOOT` row. |
