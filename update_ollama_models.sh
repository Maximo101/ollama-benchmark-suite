#!/bin/bash

# Ollama Model Update and Report Script
# This script updates all Ollama models and creates a detailed CSV report

# --- CONFIGURATION ---
# Dynamically define the base directory (GitHub portable)
BASE_DIR="$(dirname "$(realpath "$0")")"
RESULTS_DIR="$BASE_DIR/results"
# ---------------------

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Initialize arrays and variables
declare -a updated_models=()
declare -a failed_models=()
declare -a all_models=()
total_models=0
updated_count=0
failed_count=0
uptodate_count=0 # New counter for up-to-date models

# Ensure the results directory exists and define file paths
mkdir -p "$RESULTS_DIR"

# CSV file setup
csv_file="$RESULTS_DIR/ollama_models_report_$(date +%Y%m%d_%H%M%S).csv"
temp_dir="$RESULTS_DIR/temp_ollama_update_$$"
mkdir -p "$temp_dir"

echo -e "${BLUE}=== Ollama Model Update and Report Script ===${NC}"
echo -e "${BLUE}Starting at: $(date)${NC}"
echo -e "${BLUE}Results directory: $RESULTS_DIR${NC}"
echo ""

# Find the Ollama container and IP address
echo -e "${YELLOW}Locating Ollama container...${NC}"
OLLAMA_CONTAINER=$(docker ps -q --filter "name=ollama")
if [ -z "$OLLAMA_CONTAINER" ]; then
    echo -e "${RED}❌ ERROR: Could not find Ollama container. Please ensure it is running.${NC}"
    rm -rf "$temp_dir" # Clean up on exit
    exit 1
fi

OLLAMA_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$OLLAMA_CONTAINER")
if [ -z "$OLLAMA_IP" ]; then
    echo -e "${RED}❌ ERROR: Could not get IP address for Ollama container.${NC}"
    rm -rf "$temp_dir" # Clean up on exit
    exit 1
fi

echo -e "${GREEN}✓ Ollama container found at IP: $OLLAMA_IP${NC}"
echo ""

# Function to run ollama commands in the container
run_ollama_cmd() {
    docker exec "$OLLAMA_CONTAINER" ollama "$@"
}

# Function to determine the library status based on naming convention
get_library_status() {
    local model_name="$1"

    if [[ "$model_name" == hf.co/* ]]; then
        echo "Hugging Face"
    else
        # Any third method added in the future could be placed as an 'elif' here
        echo "Ollama"
    fi
}

# Get list of models from the container
echo -e "${YELLOW}Getting list of Ollama models from container...${NC}"
model_list=$(run_ollama_cmd list | tail -n +2 | grep -v "^$")

if [ -z "$model_list" ]; then
    echo -e "${RED}No models found or unable to get model list from container${NC}"
    rm -rf "$temp_dir"
    exit 1
fi

# Parse model list and populate array (in reverse order)
temp_models=()
while IFS= read -r line; do
    if [ -n "$line" ]; then
        model_name=$(echo "$line" | awk '{print $1}')
        if [ -n "$model_name" ] && [ "$model_name" != "NAME" ]; then
            temp_models+=("$model_name")
        fi
    fi
done <<< "$model_list"

# Reverse the array so we process from bottom to top
for ((i=${#temp_models[@]}-1; i>=0; i--)); do
    all_models+=("${temp_models[i]}")
    total_models=$((total_models + 1))
done

echo -e "${GREEN}✓ Found $total_models models to check for updates${NC}"
echo ""

# Create CSV header
echo "Model Name,Model ID,Size,Local Modified Date,Library Status,Update Status" > "$csv_file"

# Update each model
for i in "${!all_models[@]}"; do
    model="${all_models[$i]}"
    echo -e "${YELLOW}[$((i+1))/$total_models] Processing model: $model${NC}"

    # Get current model info from container BEFORE pulling
    model_info=$(run_ollama_cmd list | grep "^$model " | head -1)
    old_model_id=$(echo "$model_info" | awk '{print $2}')

    # Capture size and modified date BEFORE the update touch
    model_size=$(echo "$model_info" | awk '{print $3" "$4}')
    model_modified=$(echo "$model_info" | awk '{for(i=5;i<=NF;i++) printf "%s ", $i; print ""}' | sed 's/[[:space:]]*$//')

    # Check library status via our new function
    library_status=$(get_library_status "$model")

    # Attempt to update the model
    echo "  Checking for updates..."
    pull_output_file="$temp_dir/pull_output_$i.txt"

    # Capture the pull command output using tee
    docker exec "$OLLAMA_CONTAINER" ollama pull "$model" 2>&1 | tee "$pull_output_file"
    pull_exit_code=$?

    # Get new model info from container AFTER pulling
    new_model_info=$(run_ollama_cmd list | grep "^$model " | head -1)
    new_model_id=$(echo "$new_model_info" | awk '{print $2}')

    # Determine Update Status
    update_status="Update Failed" # Default failure state

    if [ $pull_exit_code -eq 0 ]; then
        # Check if the model ID (digest) changed
        if [ "$old_model_id" != "$new_model_id" ] && [ -n "$old_model_id" ]; then
            update_status="Updated"
            updated_models+=("$model")
            updated_count=$((updated_count + 1))
            echo -e "  ${GREEN}✓ Updated successfully (Model ID changed)${NC}"
        else
            # Also check if it actually downloaded anything (pulling percentages other than 100%)
            if grep -oE '[0-9]+%' "$pull_output_file" | grep -qv '^100%'; then
                update_status="Updated"
                updated_models+=("$model")
                updated_count=$((updated_count + 1))
                echo -e "  ${GREEN}✓ Updated successfully (Downloaded new layers)${NC}"
            else
                update_status="Already up to date"
                uptodate_count=$((uptodate_count + 1))
                echo -e "  ${BLUE}✓ Already up to date (No new layers pulled)${NC}"
            fi
        fi
    else
        update_status="Update Failed"
        failed_models+=("$model")
        failed_count=$((failed_count + 1))
        echo -e "  ${RED}✗ Update failed${NC}"
    fi

    # Write to CSV (escape quotes in data)
    model_csv=$(echo "$model" | sed 's/"/"""/g')
    model_id_csv=$(echo "$new_model_id" | sed 's/"/"""/g')
    model_size_csv=$(echo "$model_size" | sed 's/"/"""/g')
    model_modified_csv=$(echo "$model_modified" | sed 's/"/"""/g')
    update_status_csv=$(echo "$update_status" | sed 's/"/"""/g')

    echo "\"$model_csv\",\"$model_id_csv\",\"$model_size_csv\",\"$model_modified_csv\",\"$library_status\",\"$update_status_csv\"" >> "$csv_file"

    # Wait 2 seconds before next model (except for the last one)
    if [ $((i + 1)) -lt $total_models ]; then
        echo "  Waiting 2 seconds before next model..."
        sleep 2
    fi
    echo ""
done

# Clean up temp directory
rm -rf "$temp_dir"

# Print summary
echo -e "${BLUE}=== UPDATE SUMMARY ===${NC}"
echo -e "${BLUE}Completed at: $(date)${NC}"
echo -e "${BLUE}Report file: $csv_file${NC}"
echo -e "${BLUE}Total models processed: $total_models${NC}"
echo -e "${GREEN}Models updated: $updated_count${NC}"
echo -e "${YELLOW}Models up-to-date: $uptodate_count${NC}"
echo -e "${RED}Models failed: $failed_count${NC}"
echo ""

if [ ${#updated_models[@]} -gt 0 ]; then
    echo -e "${GREEN}✓ Successfully updated models:${NC}"
    for model in "${updated_models[@]}"; do
        echo -e "  ${GREEN}• $model${NC}"
    done
    echo ""
fi

if [ ${#failed_models[@]} -gt 0 ]; then
    echo -e "${RED}✗ Failed to update models:${NC}"
    for model in "${failed_models[@]}"; do
        echo -e "  ${RED}• $model${NC}"
    done
    echo ""
fi

echo -e "${BLUE}📊 Detailed report saved to: $csv_file${NC}"
echo -e "${YELLOW}📝 Notes:${NC}"
echo -e "${YELLOW}  • Models processed from bottom to top to maintain list order${NC}"
echo ""
