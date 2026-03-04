# Ollama Benchmark & Maintenance Suite

A collection of Bash scripts designed to help users manage and profile local AI models running in Docker (specifically optimized for Unraid/Linux environments).

## 🚀 Features
- **Hard VRAM Purge:** Clears the GPU memory and OS cache between runs for clean "Cold Boot" results.
- **Accuracy Testing:** includes logic-based prompts (Strawberry, Math, Logic) to verify model "intelligence."
- **Power Monitoring:** Integrates with `nvidia-smi` to track average power draw during inference.
- **Auto-Updater:** A robust script to pull the latest layers for all your installed models and generate a status report.

## 🛠️ Requirements
- [Ollama](https://ollama.com/) running in a Docker container on standard port 11434.
- `jq` (for JSON parsing).
- `nvidia-smi` (optional, for power tracking).

## 📂 Usage
1. Clone this repo.
2. Make scripts executable: `chmod +x *.sh`.
3. Run the benchmark: `./ollama_benchmark_v1.sh`.
