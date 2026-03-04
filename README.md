# Ollama Benchmark & Maintenance Suite

A collection of Bash scripts designed to help users manage, update, and rigorously profile local AI models running on Ollama in Docker (specifically optimized for Linux/Unraid environments). 

This suite ensures your local LLMs are not only kept up-to-date but are also tested under strict, standardized conditions to provide accurate performance metrics.

## 📦 The Scripts

This repository contains two primary tools:

### 1. `update_ollama_models.sh`
An automated maintenance script that iterates through your entire library of installed Ollama models. 
* Pulls the latest layers for every model.
* Tracks which models were updated, which were already up-to-date, and which failed.
* Generates a detailed CSV report with timestamps, file sizes, and status logs in the `/results` directory.

### 2. `ollama_benchmark_v1.sh`
A robust profiling tool that tests models against a standardized set of prompts (ranging from simple logic to complex JSON formatting).
* Outputs precise metrics including Time to First Token (TTFT), Tokens per Second (t/s), and Total Duration.
* Evaluates the logical accuracy of the model's response.
* Logs average power draw during inference using `nvidia-smi`.
* Outputs everything to a timestamped CSV for easy graphing and comparison.

---

## 🔬 Technical Deep Dive: The Benchmark Architecture

Standard benchmarking often suffers from "warm cache" bias. If you query a model twice, the second query is artificially faster because the weights are already loaded into VRAM or cached by the operating system. 

The `ollama_benchmark_v11google.sh` script eliminates this variable to provide true "Cold Boot" metrics. Here is how the logic works:

### 1. Hard VRAM Purge & API Unload
Before a benchmark begins, the script explicitly calls the Ollama API to unload any active models from memory. It doesn't just wait for a timeout; it actively severs the memory allocation to ensure the GPU is at its baseline resting state.

### 2. Linux OS Cache Flush
Even when a model is unloaded from VRAM, the Linux kernel often retains the model files in the OS page cache in system RAM. The script forces a cache drop (syncing the filesystem and clearing dentries/inodes). This guarantees that the next benchmark run forces a true physical read from your storage array (NVMe/SSD/HDD). This allows you to accurately measure your storage I/O and RAM-to-VRAM transfer bottlenecks.

### 3. VRAM vs. RAM Split Detection
Running large models often requires offloading layers to system RAM. The script detects and logs exactly how much of the model fits into the GPU and how much spills over into system memory. This context is critical for interpreting why a specific model might suddenly drop in `t/s` performance.

---

## 🛠️ Requirements
* **OS:** Linux-based system (Highly recommended for Unraid users running Ollama in Docker).
* **Ollama:** Installed and accessible.
* **Dependencies:** * `jq` (for parsing JSON API responses).
  * `nvidia-smi` (optional, for hardware power tracking).

## 🚀 Quick Start & How to Use

**1. Clone this repository** to your preferred scripts folder:
```bash
git clone https://github.com/Maximo101/ollama-benchmark-suite.git
cd ollama-benchmark-suite
```

**2. Make the scripts executable:**
```bash
chmod +x *.sh
```

**3. Run the updater** to ensure your models are current:
```bash
./update_ollama_models.sh
```

**4. Run the benchmark:**
```bash
./ollama_benchmark_v1.sh
```

> **Note:** All output logs and CSV files are automatically saved to the `results/` folder.

## Reference
[Ollama](https://ollama.com/)
