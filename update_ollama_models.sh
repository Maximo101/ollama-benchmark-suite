#!/bin/bash

# Ollama Model Update and Report Script for UnRaid Docker
# This script updates all Ollama models and creates a detailed CSV report

# --- CONFIGURATION ---
# Define the base directory and results path based on your UnRaid User Scripts location
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

# CSV file setup - Path now points to the new RESULTS_DIR
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

OLLAMA_HOST="$OLLAMA_IP:11434" # Define host for API checks, though not used in original script's core logic
echo -e "${GREEN}✓ Ollama container found at IP: $OLLAMA_IP${NC}"
echo ""

# Function to run ollama commands in the container
run_ollama_cmd() {
    docker exec "$OLLAMA_CONTAINER" ollama "$@"
}

# Function to get model info from Ollama API using better method (Original Logic)
get_model_info_from_api() {
    local model_name="$1"
    local base_name=$(echo "$model_name" | awk -F':' '{print $1}')
    local tag=$(echo "$model_name" | awk -F':' '{print $2}')
    [ -z "$tag" ] && tag="latest"
    
    # Check for custom/HuggingFace prefix
    if echo "$base_name" | grep -q "^hf\.co/"; then
        echo "custom"
        return 1
    fi
    
    # Try multiple registry endpoints
    local registry_urls=(
        "https://registry.ollama.ai/v2/${base_name}/manifests/${tag}"
        "https://ollama.com/library/${base_name}"
    )
    
    for registry_url in "${registry_urls[@]}"; do
        if command -v curl >/dev/null 2>&1; then
            local response=$(curl -s -I --connect-timeout 5 --max-time 10 "$registry_url" 2>/dev/null | head -1)
            if echo "$response" | grep -q "200\|302"; then
                echo "found"
                return 0
            fi
        elif command -v wget >/dev/null 2>&1; then
            if wget -q --timeout=5 --spider "$registry_url" 2>/dev/null; then
                echo "found"
                return 0
            fi
        fi
    done
    
    echo "custom"
    return 1
}

# Function to get detailed model info from Ollama show command (Original Logic - REMOVED AS UNUSED)
# get_detailed_model_info() { ... } 

# Function to extract model attributes based on model name and capabilities
# REMOVED: The entire 'Fallback to known model patterns' logic
get_model_attributes() {
    local model_name="$1"
    local model_info_file="$temp_dir/model_info_${model_name//\//_}.json"
    local attributes=""
    
    # Check for vision and tools capabilities based on modelfile content (retained original check)
    if [ -f "$model_info_file" ]; then
        if grep -qi "vision\|image\|visual" "$model_info_file" 2>/dev/null; then
            attributes="${attributes}vision,"
        fi
        if grep -qi "tool\|function" "$model_info_file" 2>/dev/null; then
            attributes="${attributes}tools,"
        fi
    fi
    
    # Clean up attributes
    attributes=$(echo "$attributes" | sed 's/,$//g')
    [ -z "$attributes" ] && attributes="none"
    
    echo "$attributes"
}

# Function to estimate parameters from model size (Original Logic - REMOVED AS UNUSED)
# estimate_parameters() { ... }

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
    if [ ! -z "$line" ]; then
        model_name=$(echo "$line" | awk '{print $1}')
        if [ ! -z "$model_name" ] && [ "$model_name" != "NAME" ]; then
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
    
    # Get current model info from container (Original Logic)
    model_info=$(run_ollama_cmd list | grep "^$model " | head -1)
    model_id=$(echo "$model_info" | awk '{print $2}')
    model_size=$(echo "$model_info" | awk '{print $3" "$4}')
    model_modified=$(echo "$model_info" | awk '{for(i=5;i<=NF;i++) printf "%s ", $i; print ""}' | sed 's/[[:space:]]*$//')
    
    # Check if model exists in Ollama registry (Original Logic)
    echo "  Checking registry availability..."
    library_status=$(get_model_info_from_api "$model")
    
    # Attempt to update the model
    echo "  Checking for updates..."
    pull_output_file="$temp_dir/pull_output_$i.txt"
    
    # Capture the pull command output using tee, which is necessary for the robust check
    docker exec "$OLLAMA_CONTAINER" ollama pull "$model" 2>&1 | tee "$pull_output_file"
    pull_exit_code=$?
    
    # Determine Update Status - IMPLEMENTING ROBUST CHECK
    update_status="Update Failed" # Default failure state
    
    if [ $pull_exit_code -eq 0 ]; then
        # Check the pull output for signs of a real update (i.e., pulling new layers)
        # We look for the pattern: "pulling [12-char hex ID]..."
        if grep -qE '^pulling [0-9a-f]{12}\.\.\.' "$pull_output_file"; then
            # Found lines indicating a layer pull = actual update
            update_status="Updated Successfully"
            updated_models+=("$model")
            updated_count=$((updated_count + 1))
            echo -e "  ${GREEN}✓ Updated successfully (New layers pulled)${NC}"
        else
            # Pull was successful, but no new layers were pulled = up to date.
            update_status="Already up to date"
            uptodate_count=$((uptodate_count + 1))
            echo -e "  ${BLUE}✓ Already up to date (No new layers pulled)${NC}"
        fi
    else
        update_status="Update Failed"
        failed_models+=("$model")
        failed_count=$((failed_count + 1))
        echo -e "  ${RED}✗ Update failed${NC}"
    fi
    
    # Write to CSV (escape quotes in data - Original Logic)
    model_csv=$(echo "$model" | sed 's/"/"""/g')
    model_id_csv=$(echo "$model_id" | sed 's/"/"""/g')
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
echo -e "${YELLOW}  • 'found' = Available in official Ollama registry${NC}"
echo -e "${YELLOW}  • 'custom' = Custom model or HuggingFace import${NC}"
echo ""