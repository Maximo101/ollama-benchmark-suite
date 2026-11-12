#!/usr/bin/env bash
# ==============================================================================
# OLLAMA BENCHMARK v1.10
#   Cold-boot load timing · VRAM/RAM split · real power sampling · graded prompts
#
# Usage:   ./ollama_benchmark.sh [options]
#   -m, --models REGEX    only benchmark models whose name matches REGEX
#   -x, --exclude REGEX   skip models whose name matches REGEX
#   -r, --runs N          run the prompt set N times per model (default 1)
#       --no-flush        do not drop the Linux page cache between models
#       --include-cloud   also benchmark cloud/remote models (off by default)
#   -h, --help            show this help
#
# Environment overrides:
#   OLLAMA_URL         e.g. http://192.168.1.10:11434 (skips container detection)
#   OLLAMA_CONTAINER   docker container name to look for (default: ollama)
#   NUM_PREDICT        max tokens per answer (default 4096)
#   KEEP_ALIVE         how long the model stays loaded during its run (default 10m)
#   GPU_IDS            comma separated nvidia-smi GPU indexes to sample for power
#   RESULTS_OWNER      chown the results dir to this user:group (root only;
#                      defaults to nobody:users on Unraid)
# ==============================================================================
set -uo pipefail

VERSION="1.1"

# --- CONFIGURATION -----------------------------------------------------------
BASE_DIR="$(dirname "$(realpath "$0")")"
RESULTS_DIR="$BASE_DIR/results"
OLLAMA_CONTAINER_NAME="${OLLAMA_CONTAINER:-ollama}"
NUM_PREDICT="${NUM_PREDICT:-4096}"
KEEP_ALIVE="${KEEP_ALIVE:-10m}"
TEMPERATURE="0.1"
SEED=42
SETTLE_SECS=3          # pause after cache flush
UNLOAD_TIMEOUT=60      # max seconds to wait for a model to unload
REQ_TIMEOUT_LOAD=900   # curl --max-time for a cold load
REQ_TIMEOUT_GEN=1200   # curl --max-time for one answer

MODEL_FILTER=""; MODEL_EXCLUDE=""; RUNS=1; FLUSH=1; INCLUDE_CLOUD=0

usage() { sed -n '3,/^# =====/p' "$0" | sed '$d;s/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        -m|--models)     MODEL_FILTER="${2:?missing value for $1}"; shift 2 ;;
        -x|--exclude)    MODEL_EXCLUDE="${2:?missing value for $1}"; shift 2 ;;
        -r|--runs)       RUNS="${2:?missing value for $1}"; shift 2 ;;
        --no-flush)      FLUSH=0; shift ;;
        --include-cloud) INCLUDE_CLOUD=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[[ "$RUNS" =~ ^[1-9][0-9]*$ ]] || { echo "--runs must be a positive integer" >&2; exit 2; }

# --- PROMPTS & GRADING -------------------------------------------------------
# Index N of PROMPTS is graded by grade_answer N. Prompts 5 and 6 are ungraded.
# NOTE: prompt 3 was rewritten in v2.0 - the v1 puzzle had four valid answers
# (chef, doctor, engineer or artist could all be "in the middle"). Scores for
# that prompt are therefore not comparable with v1 CSVs.
PROMPTS=(
    "How many 'r's are in the word 'Strawberry'?"
    'A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How much does the ball cost?'
    'Five people (chef, doctor, engineer, artist, pilot) stand in a line. The pilot is at the far left. The artist is immediately right of the pilot. The chef is not at either end. The doctor is somewhere to the right of the artist. The engineer is immediately right of the doctor. Who is in the middle?'
    "You are a smart home controller. User says: 'Turn on the kitchen lights and set them to Blue, but only if it is after 7 PM.' Output ONLY a JSON object with keys: action, entity, color, condition."
    'What is your base AI Model and who built it?'
    "Describe the 'Simulation Hypothesis' in 50 words."
)

# Grading is heuristic: it looks at the FINAL answer only (text after any
# </think> block, last ~600 chars) so a model that merely echoes the question
# does not get credit.
final_answer() {
    local t="$1"
    t="${t//$'\n'/ }"
    [[ "$t" == *"</think>"* ]] && t="${t##*</think>}"
    # (a negative substring offset longer than the string yields "", so guard it)
    [ "${#t}" -gt 600 ] && t="${t: -600}"
    printf '%s' "$t" | tr '[:upper:]' '[:lower:]'
}

grade_answer() {   # $1 = 1-based prompt index, $2 = raw response
    local idx="$1" ans
    ans=$(final_answer "$2")
    case "$idx" in
        1) grep -Eq '(^|[^0-9.])3([^0-9]|$)|three' <<<"$ans" && echo "Yes" || echo "No" ;;
        2) grep -Eq '0?\.05([^0-9]|$)|\b5 ?(cents?|¢)|five cents?' <<<"$ans" && echo "Yes" || echo "No" ;;
        3) # chef must be tied to "middle" in one sentence, or be the whole (short) answer
           if grep -Eq '(middle|cent(er|re)|third|3rd|position 3)[^.]{0,40}chef|chef[^.]{0,40}(middle|cent(er|re)|third|3rd|position 3)' <<<"$ans" \
              || { [ "${#ans}" -le 40 ] && grep -q 'chef' <<<"$ans"; }
           then echo "Yes"; else echo "No"; fi ;;
        4) local js="${2//$'\n'/ }"
           [[ "$js" == *"</think>"* ]] && js="${js##*</think>}"
           js="${js#*\{}"; js="{${js%\}*}}"
           if jq -e 'type=="object" and has("action") and has("entity") and has("color") and has("condition")
                     and ((.color|tostring|ascii_downcase)=="blue")' <<<"$js" >/dev/null 2>&1
           then echo "Yes (JSON)"; else echo "No"; fi ;;
        *) echo "Info/Creative" ;;
    esac
}

# --- PRE-FLIGHT --------------------------------------------------------------
for dep in curl jq awk; do
    command -v "$dep" >/dev/null 2>&1 || { echo "❌ Missing dependency: $dep" >&2; exit 1; }
done

resolve_ollama_url() {
    if [ -n "${OLLAMA_URL:-}" ]; then echo "${OLLAMA_URL%/}"; return; fi
    if command -v docker >/dev/null 2>&1; then
        local cid ip
        cid=$(docker ps -q --filter "name=^/?${OLLAMA_CONTAINER_NAME}\$" 2>/dev/null | head -n 1)
        [ -z "$cid" ] && cid=$(docker ps -q --filter "name=${OLLAMA_CONTAINER_NAME}" 2>/dev/null | head -n 1)
        if [ -n "$cid" ]; then
            ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$cid" 2>/dev/null)
            [ -n "$ip" ] && { echo "http://$ip:11434"; return; }
        fi
    fi
    echo "http://localhost:11434"
}
OLLAMA="$(resolve_ollama_url)"
if ! curl -sf --max-time 10 "$OLLAMA/api/version" >/dev/null; then
    echo "❌ Cannot reach Ollama at $OLLAMA (set OLLAMA_URL to override)" >&2
    exit 1
fi

mkdir -p "$RESULTS_DIR"
OUTPUT_CSV="$RESULTS_DIR/benchmark_results_v1.1_$(date +%Y-%m-%d_%H-%M-%S).csv"
TMP_DIR=$(mktemp -d)
POWER_FILE="$TMP_DIR/power.txt"
SAMPLER_PID=""

# GPU info (optional)
NVIDIA=0; GPU_NAME="none"; GPU_VRAM_GB=""; GPU_COUNT=0
GPU_ARGS=()
[ -n "${GPU_IDS:-}" ] && GPU_ARGS=(-i "$GPU_IDS")
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    NVIDIA=1
    GPU_NAME=$(nvidia-smi "${GPU_ARGS[@]}" --query-gpu=name --format=csv,noheader | paste -sd'+' -)
    GPU_COUNT=$(nvidia-smi "${GPU_ARGS[@]}" --query-gpu=index --format=csv,noheader | wc -l)
    GPU_VRAM_GB=$(nvidia-smi "${GPU_ARGS[@]}" --query-gpu=memory.total --format=csv,noheader,nounits \
        | awk '{s+=$1} END {printf "%.1f", s/1024}')
fi

# Cache flushing needs root (or passwordless sudo)
CACHE_FLUSHED="no"
can_flush=0
if [ "$FLUSH" = 1 ]; then
    if [ "$(id -u)" = 0 ]; then can_flush=1
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then can_flush=1
    else echo "⚠️  Not root and no passwordless sudo: page cache will NOT be flushed (cold loads may be optimistic)."; fi
fi

flush_page_cache() {
    [ "$can_flush" = 1 ] || return 0
    sync
    if [ "$(id -u)" = 0 ]; then
        echo 3 > /proc/sys/vm/drop_caches 2>/dev/null && CACHE_FLUSHED="yes"
    else
        echo 3 | sudo -n tee /proc/sys/vm/drop_caches >/dev/null 2>&1 && CACHE_FLUSHED="yes"
    fi
    [ "$CACHE_FLUSHED" = "yes" ] || echo "   ⚠️  Page-cache flush failed (read-only /proc/sys? container?)"
}

# --- HELPERS -----------------------------------------------------------------
cleanup() {
    stop_power_sampler >/dev/null 2>&1
    rm -rf "$TMP_DIR"
}
on_interrupt() { echo; echo "⛔ Interrupted - unloading models and exiting."; unload_all; finalize; exit 130; }
trap cleanup EXIT
trap on_interrupt INT TERM

finalize() {
    local owner="${RESULTS_OWNER:-}"
    [ -z "$owner" ] && [ -f /etc/unraid-version ] && owner="nobody:users"
    if [ "$(id -u)" = 0 ] && [ -n "$owner" ]; then
        chown -R "$owner" "$RESULTS_DIR" 2>/dev/null
        chmod -R u=rwX,g=rwX,o=rX "$RESULTS_DIR" 2>/dev/null
    fi
}

csv_row() { jq -nr '$ARGS.positional | @csv' --args -- "$@" >> "$OUTPUT_CSV"; }

unload_all() {
    local m
    while IFS= read -r m; do
        [ -z "$m" ] && continue
        curl -s --max-time 30 "$OLLAMA/api/generate" \
            -d "$(jq -nc --arg m "$m" '{model:$m, keep_alive:0}')" >/dev/null
    done < <(curl -s "$OLLAMA/api/ps" | jq -r '.models[]?.name')
    local waited=0
    while [ "$(curl -s "$OLLAMA/api/ps" | jq '.models | length' 2>/dev/null || echo 0)" -gt 0 ]; do
        sleep 1; waited=$((waited + 1))
        [ "$waited" -ge "$UNLOAD_TIMEOUT" ] && { echo "   ⚠️  Models still loaded after ${UNLOAD_TIMEOUT}s"; break; }
    done
}

start_power_sampler() {
    [ "$NVIDIA" = 1 ] || return 0
    : > "$POWER_FILE"
    nvidia-smi "${GPU_ARGS[@]}" --query-gpu=power.draw --format=csv,noheader,nounits -lms 250 \
        > "$POWER_FILE" 2>/dev/null &
    SAMPLER_PID=$!
}

# Prints average TOTAL power (sum over sampled GPUs) in watts, or 0.00
stop_power_sampler() {
    if [ -n "$SAMPLER_PID" ]; then
        kill "$SAMPLER_PID" 2>/dev/null
        { wait "$SAMPLER_PID"; } 2>/dev/null
        SAMPLER_PID=""
    fi
    [ "$NVIDIA" = 1 ] && [ -s "$POWER_FILE" ] || { echo "0.00"; return; }
    awk -v n="$GPU_COUNT" '$1 ~ /^[0-9.]+$/ {s+=$1; c++} END {if (c) printf "%.2f", s/c*n; else printf "0.00"}' "$POWER_FILE"
}

generate() {   # $1 model, $2 prompt, $3 num_predict, $4 timeout
    curl -s --max-time "$4" "$OLLAMA/api/generate" -d "$(jq -nc \
        --arg m "$1" --arg p "$2" --arg ka "$KEEP_ALIVE" \
        --argjson np "$3" --argjson t "$TEMPERATURE" --argjson seed "$SEED" \
        '{model:$m, prompt:$p, stream:false, keep_alive:$ka,
          options:{temperature:$t, seed:$seed, num_predict:$np}}')"
}

# --- CSV HEADER --------------------------------------------------------------
csv_row "Model" "Size (GB)" "Arch" "Params" "Context" "Embed" "Quant" "Capabilities" "Temp" "License" \
        "VRAM_GB" "RAM_GB" "GPU" "GPU_VRAM_GB" "Cache_Flushed" \
        "Test Type" "Prompt" "Correct" "Latency (s)" "Load (s)" "TTFT (s)" "Prefill TPS" \
        "Eval Count" "TPS" "Power (W)" "Response"

echo "🧪 Ollama Benchmark v$VERSION"
echo "🌐 Server : $OLLAMA"
echo "🎮 GPU    : $GPU_NAME${GPU_VRAM_GB:+ (${GPU_VRAM_GB} GB total)}"
echo "📂 Output : $OUTPUT_CSV"
echo "------------------------------------------------------"

# --- MODEL LIST (from the API - no CLI scraping) -----------------------------
MODELS_TSV=$(curl -s "$OLLAMA/api/tags" | jq -r --argjson cloud "$INCLUDE_CLOUD" '
    .models | sort_by(.name)[]
    | select($cloud == 1 or (((.remote_host // "") == "") and (.name | test("[:-]cloud$") | not)))
    | [.name, .size] | @tsv')
if [ -z "$MODELS_TSV" ]; then echo "❌ No models found."; exit 1; fi

n_done=0; n_skipped=0; n_failed=0

while IFS=$'\t' read -r -u 3 MODEL_NAME SIZE_BYTES; do
    [ -z "$MODEL_NAME" ] && continue
    if [ -n "$MODEL_FILTER" ]  && ! grep -Eq "$MODEL_FILTER"  <<<"$MODEL_NAME"; then continue; fi
    if [ -n "$MODEL_EXCLUDE" ] &&   grep -Eq "$MODEL_EXCLUDE" <<<"$MODEL_NAME"; then continue; fi

    # 1. Metadata - one API call, every field guaranteed non-empty
    META=$(curl -s --max-time 30 "$OLLAMA/api/show" -d "$(jq -nc --arg m "$MODEL_NAME" '{model:$m}')" | jq -r '
        (.model_info["general.architecture"] // "unknown") as $a
        | [ $a,
            (.details.parameter_size // "unknown"),
            ((.model_info[$a + ".context_length"] // .model_info["general.context_length"] // "unknown") | tostring),
            ((.model_info[$a + ".embedding_length"] // "unknown") | tostring),
            (.details.quantization_level // "unknown"),
            ((.capabilities // ["completion"]) | join(",")),
            ((.parameters // "") | split("\n") | map(select(test("^temperature"))) | (.[0] // "") | split(" ") | last // "unknown" | if . == "" then "unknown" else . end),
            ((.license // "") | split("\n") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0)) | (.[0] // "unknown"))
          ] | join("\u001f")')
    IFS=$'\x1f' read -r ARCH PARAMS CONTEXT EMBED QUANT CAPS TEMP LICENSE <<<"$META"
    [ -z "${ARCH:-}" ] && { echo "⚠️  $MODEL_NAME: could not read metadata, skipping"; n_skipped=$((n_skipped + 1)); continue; }
    SIZE_GB=$(awk -v b="$SIZE_BYTES" 'BEGIN {printf "%.2f", b / 1073741824}')

    if [[ ",$CAPS," != *",completion,"* ]]; then
        echo "⏭️  Skipping $MODEL_NAME (capabilities: $CAPS)"
        n_skipped=$((n_skipped + 1)); continue
    fi

    echo "🚀 Benchmarking: $MODEL_NAME  ($PARAMS, $QUANT, ${SIZE_GB} GB)"
    META_COLS=("$MODEL_NAME" "$SIZE_GB" "$ARCH" "$PARAMS" "$CONTEXT" "$EMBED" "$QUANT" "$CAPS" "$TEMP" "$LICENSE")

    # 2. Hard purge + OS cache flush
    unload_all
    CACHE_FLUSHED="no"
    flush_page_cache
    sleep "$SETTLE_SECS"

    # 3. Timed cold boot: an empty-prompt request loads the model and nothing else
    T0=$(date +%s.%N)
    COLD_RES=$(curl -s --max-time "$REQ_TIMEOUT_LOAD" "$OLLAMA/api/generate" \
        -d "$(jq -nc --arg m "$MODEL_NAME" --arg ka "$KEEP_ALIVE" '{model:$m, keep_alive:$ka}')")
    T1=$(date +%s.%N)
    COLD_WALL=$(awk -v a="$T0" -v b="$T1" 'BEGIN {printf "%.2f", b - a}')
    COLD_LOAD=$(jq -r '(.load_duration // 0) / 1e9 | . * 100 | round / 100' <<<"$COLD_RES" 2>/dev/null || echo 0)

    # 4. VRAM / RAM split (GiB)
    read -r V_GB R_GB < <(curl -s "$OLLAMA/api/ps" | jq -r --arg m "$MODEL_NAME" '
        (.models[]? | select(.name == $m or .model == $m)) // {size:0, size_vram:0}
        | [((.size_vram // 0) / 1073741824), (((.size // 0) - (.size_vram // 0)) / 1073741824)]
        | map(. * 100 | round / 100 | tostring) | join(" ")')
    V_GB="${V_GB:-0}"; R_GB="${R_GB:-0}"

    VARS=("$V_GB" "$R_GB" "$GPU_NAME" "$GPU_VRAM_GB" "$CACHE_FLUSHED")
    if [ -z "$COLD_RES" ] || [ "$(jq -r 'has("error")' <<<"$COLD_RES" 2>/dev/null)" != "false" ]; then
        echo "   ❌ Cold boot FAILED: $(jq -r '.error // "no response / timeout"' <<<"$COLD_RES" 2>/dev/null)"
        csv_row "${META_COLS[@]}" "${VARS[@]}" "COLD_BOOT" "Load Test" "N/A" "$COLD_WALL" "$COLD_LOAD" "" "" "" "" "" "Failed"
        n_failed=$((n_failed + 1)); echo "------------------------------------------------------"; continue
    fi
    echo "   ❄️  Cold boot: ${COLD_WALL}s wall (${COLD_LOAD}s load) | VRAM ${V_GB} GiB | RAM ${R_GB} GiB | cache flushed: $CACHE_FLUSHED"
    csv_row "${META_COLS[@]}" "${VARS[@]}" "COLD_BOOT" "Load Test" "N/A" "$COLD_WALL" "$COLD_LOAD" "" "" "" "" "" "Loaded"

    # 5. Discarded warm-up so first-inference overhead does not skew prompt 1
    generate "$MODEL_NAME" "Say hi." 8 120 >/dev/null

    # 6. Timed warm prompts
    for ((run = 1; run <= RUNS; run++)); do
        for i in "${!PROMPTS[@]}"; do
            PROMPT_TEXT="${PROMPTS[$i]}"
            start_power_sampler
            RES=$(generate "$MODEL_NAME" "$PROMPT_TEXT" "$NUM_PREDICT" "$REQ_TIMEOUT_GEN")
            AVG_P=$(stop_power_sampler)

            ERR=$(jq -r '.error // empty' <<<"$RES" 2>/dev/null)
            if [ -z "$RES" ] || ! jq -e . >/dev/null 2>&1 <<<"$RES" || [ -n "$ERR" ]; then
                echo "   Prompt $((i + 1)): FAILED ${ERR:-(no response / timeout)}"
                csv_row "${META_COLS[@]}" "${VARS[@]}" "WARM" "$PROMPT_TEXT" "No" "0" "0" "0" "0" "0" "0" "$AVG_P" "[No response received]"
                continue
            fi

            read -r TOTAL_NS LOAD_NS PE_NS PE_N EVAL_N EVAL_NS < <(jq -r '[(.total_duration // 0), (.load_duration // 0),
                (.prompt_eval_duration // 0), (.prompt_eval_count // 0), (.eval_count // 0), (.eval_duration // 0)] | @tsv' <<<"$RES")
            read -r LAT LOADS TTFT PTPS TPS < <(awk -v t="$TOTAL_NS" -v l="$LOAD_NS" -v pe="$PE_NS" -v pn="$PE_N" -v en="$EVAL_N" -v ed="$EVAL_NS" 'BEGIN {
                printf "%.2f %.2f %.3f %.2f %.2f\n", t/1e9, l/1e9, (l+pe)/1e9, (pe > 0 ? pn/(pe/1e9) : 0), (ed > 0 ? en/(ed/1e9) : 0)}')

            RAW_TEXT=$(jq -r '.response // ""' <<<"$RES")
            if [ -z "$RAW_TEXT" ]; then
                CLEAN="[No final answer - ${EVAL_N} tokens generated, possibly cut off by num_predict=$NUM_PREDICT]"
                IS_CORRECT=$([ "$i" -le 3 ] && echo "No" || echo "Info/Creative")
            else
                IS_CORRECT=$(grade_answer "$((i + 1))" "$RAW_TEXT")
                # one line, 1000 chars, neutralise spreadsheet formula prefixes
                CLEAN=$(jq -Rrs 'gsub("[\\r\\n]+"; " ") | .[0:1000] | if test("^[=+@-]") then " " + . else . end' <<<"$RAW_TEXT")
            fi

            echo "   Prompt $((i + 1)): ${TPS} t/s | ${EVAL_N} tok | ${AVG_P} W | Correct: $IS_CORRECT"
            csv_row "${META_COLS[@]}" "${VARS[@]}" "WARM" "$PROMPT_TEXT" "$IS_CORRECT" "$LAT" "$LOADS" "$TTFT" "$PTPS" "$EVAL_N" "$TPS" "$AVG_P" "$CLEAN"
            sleep 1
        done
    done
    unload_all     # free the GPU before the next model's metadata/purge cycle
    n_done=$((n_done + 1))
    echo "------------------------------------------------------"
done 3<<<"$MODELS_TSV"

finalize
echo "✅ Done: $n_done benchmarked, $n_failed failed to load, $n_skipped skipped."
echo "📄 Results: $OUTPUT_CSV"
