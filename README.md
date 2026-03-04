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
3. Run the updates: `./update_ollama_models.sh`.
4. Run the benchmark: `./ollama_benchmark_v1.sh`.

Can be used with 'User Scripts' in Unraid to keep your ollama models up to date automatically.

In your ollama_benchmark_v1.sh, there is the line:
chown -R nobody:users "$BASE_DIR" && chmod -R 777 "$BASE_DIR"
This is perfect for Unraid's permission system, but users on standard Ubuntu or Windows (WSL) might get a "Permission Denied" error if they aren't running as root. 
Note that the script is optimized for Unraid/Docker environments, if you want ot use this on other systems, comment out that line.
