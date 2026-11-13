#!/usr/bin/env bash
# ==============================================================================
# OLLAMA MODEL UPDATER v1.1
# Pulls the latest version of every installed model and writes a CSV report.
#
# Usage:   ./update_ollama_models.sh [options]
#   -m, --models REGEX   only update models whose name matches REGEX
#   -x, --exclude REGEX  skip models whose name matches REGEX
#   -n, --dry-run        list what would be checked, pull nothing
#   -h, --help           show this help
#
# Environment overrides:
#   OLLAMA_URL         e.g. http://192.168.1.10:11434 (skips container detection)
#   OLLAMA_CONTAINER   docker container name to look for (default: ollama)
#
# Exit status is 1 if any pull failed, so the script is safe to use from cron.
# ==============================================================================
set -uo pipefail

BASE_DIR="$(dirname "$(realpath "$0")")"
RESULTS_DIR="$BASE_DIR/results"
OLLAMA_CONTAINER_NAME="${OLLAMA_CONTAINER:-ollama}"
MODEL_FILTER=""; MODEL_EXCLUDE=""; DRY_RUN=0

usage() { sed -n '3,/^# =====/p' "$0" | sed '$d;s/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
    case "$1" in
        -m|--models)  MODEL_FILTER="${2:?missing value for $1}"; shift 2 ;;
        -x|--exclude) MODEL_EXCLUDE="${2:?missing value for $1}"; shift 2 ;;
        -n|--dry-run) DRY_RUN=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# Colours only when writing to a terminal
if [ -t 1 ]; then
    RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; BLUE=""; NC=""
fi

for dep in curl jq awk; do
    command -v "$dep" >/dev/null 2>&1 || { echo "${RED}❌ Missing dependency: $dep${NC}" >&2; exit 1; }
done

resolve_ollama_url() {
    if [ -n "${OLLAMA_URL:-}" ]; then echo "${OLLAMA_URL%/}"; return; fi
    if command -v docker >/dev/null 2>&1; then
        local cid ip loose=()
        cid=$(docker ps -q --filter "name=^/?${OLLAMA_CONTAINER_NAME}\$" 2>/dev/null | head -n 1)
        if [ -z "$cid" ]; then
            # No exact match: accept a partial name match only when it is unambiguous
            # (otherwise "ollama" would also match e.g. "ollama-webui").
            mapfile -t loose < <(docker ps -q --filter "name=${OLLAMA_CONTAINER_NAME}" 2>/dev/null)
            if [ "${#loose[@]}" -eq 1 ]; then
                cid="${loose[0]}"
            elif [ "${#loose[@]}" -gt 1 ]; then
                echo "⚠️  ${#loose[@]} running containers match '${OLLAMA_CONTAINER_NAME}'; set OLLAMA_CONTAINER to the exact name or set OLLAMA_URL. Trying localhost." >&2
            fi
        fi
        if [ -n "$cid" ]; then
            # One address per network, space separated - use the first (none with host networking).
            ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$cid" 2>/dev/null)
            read -r ip _ <<<"$ip"
            [ -n "$ip" ] && { echo "http://$ip:11434"; return; }
        fi
    fi
    echo "http://localhost:11434"
}
OLLAMA="$(resolve_ollama_url)"
if ! curl -sf --max-time 10 "$OLLAMA/api/version" >/dev/null; then
    echo "${RED}❌ Cannot reach Ollama at $OLLAMA (set OLLAMA_URL to override)${NC}" >&2
    exit 1
fi

mkdir -p "$RESULTS_DIR"
CSV_FILE="$RESULTS_DIR/ollama_models_report_$(date +%Y%m%d_%H%M%S).csv"
csv_row() { jq -nr '$ARGS.positional | @csv' --args -- "$@" >> "$CSV_FILE"; }

# Snapshot of one model from /api/tags: "digest12<TAB>size_GiB<TAB>modified<TAB>cloud(0/1)"
model_snapshot() {
    curl -s "$OLLAMA/api/tags" | jq -r --arg m "$1" '
        .models[]? | select(.name == $m)
        | [ (.digest[0:12]), (((.size // 0) / 1073741824 * 100 | round) / 100 | tostring),
            ((.modified_at // "")[0:19] | sub("T"; " ") | if . == "" then "unknown" else . end),
            (if ((.remote_host // "") != "" or (.name | test("[:-]cloud$"))) then "1" else "0" end) ]
        | join("\t")'
}

# Pull via the API; prints progress to stderr and "OK" or "ERR:<message>" to stdout.
pull_model() {
    curl -sN --connect-timeout 15 "$OLLAMA/api/pull" -d "$(jq -nc --arg m "$1" '{model:$m, stream:true}')" \
    | jq -rc --unbuffered '[(.status // ""), (.completed // 0), (.total // 0), (.error // "")] | @tsv' \
    | awk -F'\t' '
        { st=$1; c=$2+0; t=$3+0; err=$4
          if (err != "") { result = "ERR:" err; next }
          if (st == "success") { result = "OK"; print "    success" > "/dev/stderr"; next }
          if (t > 0) { pct = int(c * 100 / t); step = int(pct / 20)
                       if (st != last || step > laststep) { printf "    %s %d%%\n", substr(st, 1, 40), pct > "/dev/stderr"; last = st; laststep = step } }
          else if (st != last) { print "    " substr(st, 1, 60) > "/dev/stderr"; last = st; laststep = -1 }
        }
        END { print (result == "" ? "ERR:no response from server" : result) }'
}

echo "${BLUE}=== Ollama Model Updater v1.1 ===${NC}"
echo "${BLUE}Server  : $OLLAMA${NC}"
echo "${BLUE}Started : $(date)${NC}"
echo "${BLUE}Report  : $CSV_FILE${NC}"
echo

mapfile -t ALL_MODELS < <(curl -s "$OLLAMA/api/tags" | jq -r '.models[]?.name' | sort)
MODELS=()
for m in "${ALL_MODELS[@]}"; do
    [ -n "$MODEL_FILTER" ]  && ! grep -Eq "$MODEL_FILTER"  <<<"$m" && continue
    [ -n "$MODEL_EXCLUDE" ] &&   grep -Eq "$MODEL_EXCLUDE" <<<"$m" && continue
    MODELS+=("$m")
done
TOTAL=${#MODELS[@]}
[ "$TOTAL" -eq 0 ] && { echo "${RED}No matching models found.${NC}"; exit 1; }
echo "${GREEN}✓ $TOTAL model(s) to check${NC}"; echo

csv_row "Model Name" "Model ID" "Size (GB)" "Local Modified Date" "Library Status" "Update Status" "Notes"

updated=(); failed=(); n_uptodate=0; n_skipped=0

for i in "${!MODELS[@]}"; do
    model="${MODELS[$i]}"
    echo "${YELLOW}[$((i + 1))/$TOTAL] $model${NC}"

    IFS=$'\t' read -r old_id size_gb modified is_cloud < <(model_snapshot "$model")
    case "$model" in hf.co/*) library="Hugging Face" ;; *) library="Ollama" ;; esac

    if [ "${is_cloud:-0}" = 1 ]; then
        echo "  ${BLUE}↷ Cloud model - nothing to pull${NC}"
        csv_row "$model" "$old_id" "$size_gb" "$modified" "Cloud" "Skipped (cloud)" ""
        n_skipped=$((n_skipped + 1)); echo; continue
    fi
    if [ "$DRY_RUN" = 1 ]; then
        echo "  ${BLUE}↷ dry run${NC}"
        csv_row "$model" "$old_id" "$size_gb" "$modified" "$library" "Dry run" ""
        echo; continue
    fi

    result=$(pull_model "$model")
    note=""
    if [ "$result" = "OK" ]; then
        IFS=$'\t' read -r new_id size_gb modified _ < <(model_snapshot "$model")
        if [ "$new_id" != "$old_id" ]; then
            status="Updated"; updated+=("$model"); note="$old_id -> $new_id"
            echo "  ${GREEN}✓ Updated ($old_id → $new_id)${NC}"
        else
            status="Already up to date"; n_uptodate=$((n_uptodate + 1))
            echo "  ${BLUE}✓ Already up to date${NC}"
        fi
        old_id="$new_id"
    else
        note="${result#ERR:}"
        if grep -Eqi 'does not exist|not found' <<<"$note"; then
            status="Not in registry"; n_skipped=$((n_skipped + 1))
            echo "  ${YELLOW}↷ Not in a registry (locally created?): $note${NC}"
        else
            status="Update Failed"; failed+=("$model")
            echo "  ${RED}✗ Update failed: $note${NC}"
        fi
    fi
    csv_row "$model" "$old_id" "$size_gb" "$modified" "$library" "$status" "$note"
    echo
done

echo "${BLUE}=== UPDATE SUMMARY ===${NC}"
echo "${BLUE}Completed: $(date)${NC}"
echo "${GREEN}Updated    : ${#updated[@]}${NC}"
echo "${YELLOW}Up to date : $n_uptodate${NC}"
echo "${BLUE}Skipped    : $n_skipped${NC}"
echo "${RED}Failed     : ${#failed[@]}${NC}"
for m in "${updated[@]}"; do echo "  ${GREEN}• updated: $m${NC}"; done
for m in "${failed[@]}";  do echo "  ${RED}• failed:  $m${NC}"; done
echo
echo "${BLUE}📊 Report saved to: $CSV_FILE${NC}"

if [ "$(id -u)" = 0 ]; then
    owner="${RESULTS_OWNER:-}"; [ -z "$owner" ] && [ -f /etc/unraid-version ] && owner="nobody:users"
    if [ -n "$owner" ]; then
        chown -R "$owner" "$RESULTS_DIR" 2>/dev/null
        chmod -R u=rwX,g=rwX,o=rX "$RESULTS_DIR" 2>/dev/null
    fi
fi
[ "${#failed[@]}" -eq 0 ]
