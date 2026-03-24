#!/bin/bash

# ==============================================================================
# OLLAMA AI BENCHMARK SCRIPT v1.1
# Features: Hard VRAM Purge, OS Cache Flush, VRAM vs RAM Split Detection
# ==============================================================================

# --- CONFIGURATION ---
BASE_DIR="$(dirname "$(realpath "$0")")"
RESULTS_DIR="$BASE_DIR/results"
mkdir -p "$RESULTS_DIR"
OUTPUT_CSV="$RESULTS_DIR/benchmark_results_v11_$(date +%Y-%m-%d_%H-%M-%S).csv"

echo "📂 Benchmark results: $OUTPUT_CSV"

# --- PROMPTS ---
declare -a PROMPTS=(
    "How many 'r's are in the word 'Strawberry'?"
    "A bat and a ball cost \$1.10 in total. The bat costs \$1.00 more than the ball. How much does the ball cost?"
    "Five people (chef, doctor, engineer, artist, pilot) are in a line. The chef is not at the end. The doctor is right of the artist. The pilot is at the far left. Who is in the middle?"
    "You are a smart home controller. User says: 'Turn on the kitchen lights and set them to Blue, but only if it is after 7 PM.' Output ONLY a JSON object with keys: action, entity, color, condition."
    "What is your base AI Model and who built it?"
    "Describe the 'Simulation Hypothesis' in 50 words."
)

function check_answer_logic() {
    local prompt_idx="$1"
    local resp=$(echo "$2" | tr '[:upper:]' '[:lower:]')
    case $prompt_idx in
        1) [[ "$resp" =~ "3" || "$resp" =~ "three" ]] && echo "Yes" || echo "No" ;;
        2) [[ "$resp" =~ "0.05" || "$resp" =~ "5 cent" ]] && echo "Yes" || echo "No" ;;
        3) [[ "$resp" =~ "engineer" ]] && echo "Yes" || echo "No" ;;
        4) [[ "$resp" =~ \{ && "$resp" =~ "action" ]] && echo "Yes (JSON)" || echo "No" ;;
        *) echo "Info/Creative" ;;
    esac
}

# --- SYSTEM SETUP ---
OLLAMA_CONTAINER=$(docker ps -q --filter "name=ollama")
OLLAMA_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$OLLAMA_CONTAINER")
NVIDIA_SMI_AVAILABLE=$(command -v nvidia-smi &> /dev/null && echo true || echo false)

# --- CSV HEADER (Includes VRAM vs RAM columns) ---
echo "Model,Size (GB),Arch,Params,Context,Embed,Quant,Capabilities,Temp,License,VRAM_GB,RAM_GB,Test Type,Prompt,Correct,Latency (s),TPS,Power (W),Response" > "$OUTPUT_CSV"

# --- MAIN LOOP ---
MODELS_LIST=$(docker exec "$OLLAMA_CONTAINER" ollama list | sed '1d' | grep -v "ID")

while IFS= read -r line; do
    [ -z "$line" ] && continue
    MODEL_NAME=$(echo "$line" | awk '{print $1}')
    
    # 1. Fetch Basic Metadata
    INFO_JSON=$(curl -s -X POST http://"$OLLAMA_IP":11434/api/show -d "{\"name\": \"$MODEL_NAME\"}")
    ARCH=$(echo "$INFO_JSON" | jq -r '.model_info["general.architecture"] // "unknown"')
    PARAMS=$(echo "$INFO_JSON" | jq -r '.details.parameter_size // "unknown"')
    CONTEXT=$(echo "$INFO_JSON" | jq -r '.model_info["'"$ARCH"'.context_length"] // .model_info["general.context_length"] // "unknown"')
    EMBED=$(echo "$INFO_JSON" | jq -r '.model_info["'"$ARCH"'.embedding_length"] // "unknown"')
    QUANT=$(echo "$INFO_JSON" | jq -r '.details.quantization_level // "unknown"')
    CLI_SHOW=$(docker exec "$OLLAMA_CONTAINER" ollama show "$MODEL_NAME")
    CAPS=$(echo "$CLI_SHOW" | sed -n '/Capabilities/,/^[[:space:]]*$/p' | sed '1d' | xargs | tr ' ' ',')
    TEMP=$(echo "$CLI_SHOW" | grep -i "temperature" | awk '{print $NF}' | xargs)
    LICENSE=$(echo "$CLI_SHOW" | sed -n '/License/,/^[[:space:]]*$/p' | sed '1d' | head -n 1 | xargs | tr ',' ' ')
    
    RAW_SIZE=$(echo "$line" | grep -oE '[0-9.]+[[:space:]]*(GB|MB)')
    SIZE_VAL=$(echo "$RAW_SIZE" | awk '{print $1}')
    [[ "$RAW_SIZE" == *"MB"* ]] && SIZE_GB=$(awk "BEGIN {printf \"%.2f\", $SIZE_VAL / 1024}") || SIZE_GB="$SIZE_VAL"

    if [[ ! "$CAPS" =~ "completion" ]]; then continue; fi

    echo "🔍 Fetching: $MODEL_NAME"
    echo "🚀 Benchmarking: $MODEL_NAME"

    # --- THE HARD PURGE ---
    LOADED_MODELS=$(curl -s http://"$OLLAMA_IP":11434/api/ps | jq -r '.models[].name')
    for m in $LOADED_MODELS; do
        curl -s -X POST http://"$OLLAMA_IP":11434/api/generate -d "{\"model\": \"$m\", \"keep_alive\": 0}" > /dev/null
    done

    while true; do
        STILL_LOADED=$(curl -s http://"$OLLAMA_IP":11434/api/ps | jq '.models | length')
        [ "$STILL_LOADED" -eq 0 ] && break
        sleep 1
    done

    # Flush OS PageCache
    sync && echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
    sleep 5

    # --- TIMED COLD BOOT ---
    START_TIME=$(date +%s.%N)
    COLD_RES=$(curl -s --max-time 600 -X POST http://"$OLLAMA_IP":11434/api/generate \
        -d "{\"model\": \"$MODEL_NAME\", \"prompt\": \"Hello\", \"stream\": false}")
    END_TIME=$(date +%s.%N)
    COLD_LATENCY=$(awk "BEGIN {printf \"%.2f\", $END_TIME - $START_TIME}")

    # --- MEMORY SPLIT DETECTION ---
    # Query API to see how Ollama distributed the model
    PS_STATS=$(curl -s http://"$OLLAMA_IP":11434/api/ps | jq -r ".models[] | select(.name == \"$MODEL_NAME\")")
    V_BYTES=$(echo "$PS_STATS" | jq -r '.size_vram // 0')
    T_BYTES=$(echo "$PS_STATS" | jq -r '.size // 0')
    R_BYTES=$((T_BYTES - V_BYTES))
    
    V_GB=$(awk "BEGIN {printf \"%.2f\", $V_BYTES / 1073741824}")
    R_GB=$(awk "BEGIN {printf \"%.2f\", $R_BYTES / 1073741824}")

    if [[ -z "$COLD_RES" || "$COLD_RES" == *"error"* ]]; then
        echo "   Cold Boot - FAILED"
        COLD_STATUS="Failed"
    else
        echo "   Cold Boot - Time to load model: ${COLD_LATENCY}s"
        echo "   Memory Split - VRAM: ${V_GB}GB | System RAM: ${R_GB}GB"
        COLD_STATUS="Loaded"
    fi

    # Record Cold Boot
    printf '"%s","%s","%s","%s","%s","%s","%s","%s","%s","%s","%s","%s",COLD_BOOT,Load Test,N/A,%.2f,N/A,N/A,"%s"\n' \
        "$MODEL_NAME" "$SIZE_GB" "$ARCH" "$PARAMS" "$CONTEXT" "$EMBED" "$QUANT" "$CAPS" "$TEMP" "$LICENSE" \
        "$V_GB" "$R_GB" "$COLD_LATENCY" "$COLD_STATUS" >> "$OUTPUT_CSV"

    # --- WARM PROMPTS ---
    for i in "${!PROMPTS[@]}"; do
        PROMPT_TEXT=${PROMPTS[$i]}
        GPU_START=0
        [ "$NVIDIA_SMI_AVAILABLE" = true ] && GPU_START=$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits | head -n 1)

        RES=$(curl -s --max-time 800 -X POST http://"$OLLAMA_IP":11434/api/generate \
            -d "{\"model\": \"$MODEL_NAME\", \"prompt\": \"$PROMPT_TEXT\", \"stream\": false, \"options\": {\"temperature\": 0.1}}")
            
        GPU_END=0; AVG_P=0
        if [ "$NVIDIA_SMI_AVAILABLE" = true ]; then
            GPU_END=$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits | head -n 1)
            AVG_P=$(awk "BEGIN {printf \"%.2f\", ($GPU_START + GPU_END) / 2}")
        fi

        TOTAL_NS=$(echo "$RES" | jq -r '.total_duration // 0')
        EVAL_C=$(echo "$RES" | jq -r '.eval_count // 0')
        EVAL_NS=$(echo "$RES" | jq -r '.eval_duration // 1')
        TOTAL_S=$(awk "BEGIN {printf \"%.2f\", $TOTAL_NS / 1000000000}")
        TPS=$(awk "BEGIN {printf \"%.2f\", $EVAL_C / ($EVAL_NS / 1000000000)}")
        
        RAW_TEXT=$(echo "$RES" | jq -r '.response // empty')
        [ -z "$RAW_TEXT" ] && RAW_TEXT="[No response received]"
        
        IS_CORRECT=$(check_answer_logic "$((i+1))" "$RAW_TEXT")
        CLEAN_TEXT=$(echo "$RAW_TEXT" | tr '\n' ' ' | sed 's/"/""/g' | cut -c1-1000)

        echo "   Prompt $((i+1)): ${TPS} t/s | Correct: $IS_CORRECT"

        printf '"%s","%s","%s","%s","%s","%s","%s","%s","%s","%s","%s","%s",WARM,"%s","%s",%.2f,%.2f,%.2f,"%s"\n' \
            "$MODEL_NAME" "$SIZE_GB" "$ARCH" "$PARAMS" "$CONTEXT" "$EMBED" "$QUANT" "$CAPS" "$TEMP" "$LICENSE" \
            "$V_GB" "$R_GB" "$(echo "$PROMPT_TEXT" | sed 's/"/""/g')" "$IS_CORRECT" "$TOTAL_S" "$TPS" "$AVG_P" "$CLEAN_TEXT" >> "$OUTPUT_CSV"
        sleep 1
    done
    echo "------------------------------------------------------"
done <<< "$MODELS_LIST"

chown -R nobody:users "$BASE_DIR" && chmod -R 777 "$BASE_DIR"
echo "✅ Benchmark Complete. Results: $OUTPUT_CSV"

# --- LAUNCH DASHBOARD ---
DASHBOARD_SCRIPT="$RESULTS_DIR/ollama_benchmark_dashboard.py"
LATEST_CSV=$(ls -t "$RESULTS_DIR"/benchmark_results_*.csv 2>/dev/null | head -n 1)

if [ ! -f "$DASHBOARD_SCRIPT" ]; then
    echo "⚠️  Dashboard script not found: $DASHBOARD_SCRIPT"
elif [ -z "$LATEST_CSV" ]; then
    echo "⚠️  No benchmark CSV found in $RESULTS_DIR"
else
    echo "📊 Launching dashboard with: $LATEST_CSV"
    if command -v python &> /dev/null; then
        python "$DASHBOARD_SCRIPT" "$LATEST_CSV"
    elif command -v python3 &> /dev/null; then
        python3 "$DASHBOARD_SCRIPT" "$LATEST_CSV"
    else
        echo "❌ Python not found. Cannot launch dashboard."
    fi
fi
