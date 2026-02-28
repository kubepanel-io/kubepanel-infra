#!/bin/bash

# ──────────────── Colours ────────────────
YELLOW='\033[1;33m'
GREEN='\033[1;32m'
BLUE='\033[1;34m'
CYAN='\033[1;36m'
RED='\033[1;31m'
NC='\033[0m' # No Colour

# Debug mode - check if KUBEPANEL_DEBUG is set to "true"
DEBUG_MODE=${KUBEPANEL_DEBUG:-false}

# Volume group and logical volume names
VG_NAME="linstorvg"
LV_NAME="linstorlv"
MIN_SIZE_BYTES=$((1 * 1024 * 1024 * 1024))  # 1 GiB in bytes

# ASCII Art and Progress Functions
print_header() {
    echo -e "${CYAN}"
    echo "╔═══════════════════════════════════════════════════════════════════════╗"
    echo "║                    KUBEPANEL STORAGE EXTENSION                        ║"
    echo "║              Extend Linstor storage with additional disks             ║"
    echo "╚═══════════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}╔═══════════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${RED}║                            ERROR                                      ║${NC}"
        echo -e "${RED}╠═══════════════════════════════════════════════════════════════════════╣${NC}"
        echo -e "${RED}║${NC} This script must be run as root or with sudo privileges          ${RED}║${NC}"
        echo -e "${RED}║${NC}                                                                   ${RED}║${NC}"
        echo -e "${RED}║${NC} Please run:                                                       ${RED}║${NC}"
        echo -e "${RED}║${NC}   ${YELLOW}sudo kubepanel-extend-storage ${NC}                             ${RED}║${NC}"
        echo -e "${RED}╚═══════════════════════════════════════════════════════════════════════╝${NC}"
        exit 1
    fi
}

print_step() {
    local step_num=$1
    local step_name=$2
    echo -e "\n${BLUE}┌─────────────────────────────────────────────────────────────────────┐${NC}"
    echo -e "${BLUE}│${NC} ${YELLOW}Step $step_num:${NC} $step_name"
    echo -e "${BLUE}└─────────────────────────────────────────────────────────────────────┘${NC}"
}

print_progress() {
    local message=$1
    echo -e "  ${GREEN}▶${NC} $message"
}

print_success() {
    local message=$1
    echo -e "  ${GREEN}✓${NC} $message"
}

print_warning() {
    local message=$1
    echo -e "  ${YELLOW}⚠${NC} $message"
}

print_error() {
    local message=$1
    echo -e "  ${RED}✗${NC} $message"
}

print_info() {
    local message=$1
    echo -e "  ${CYAN}ℹ${NC} $message"
}

# Function to run commands with optional debug output
run_cmd() {
    if [ "$DEBUG_MODE" = "true" ]; then
        "$@"
    else
        "$@" >/dev/null 2>&1
    fi
}

# Convert size string to bytes for comparison
size_to_bytes() {
    local size=$1
    local num unit

    # Extract number and unit
    num=$(echo "$size" | sed 's/[^0-9.]//g')
    unit=$(echo "$size" | sed 's/[0-9.]//g' | tr '[:lower:]' '[:upper:]')

    case "$unit" in
        "K"|"KB"|"KIB") echo "$num * 1024" | bc | cut -d'.' -f1 ;;
        "M"|"MB"|"MIB") echo "$num * 1024 * 1024" | bc | cut -d'.' -f1 ;;
        "G"|"GB"|"GIB") echo "$num * 1024 * 1024 * 1024" | bc | cut -d'.' -f1 ;;
        "T"|"TB"|"TIB") echo "$num * 1024 * 1024 * 1024 * 1024" | bc | cut -d'.' -f1 ;;
        *) echo "$num" | cut -d'.' -f1 ;;
    esac
}

# Check if volume group exists
check_vg_exists() {
    if ! vgs "$VG_NAME" >/dev/null 2>&1; then
        echo -e "${RED}╔═══════════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${RED}║                            ERROR                                      ║${NC}"
        echo -e "${RED}╠═══════════════════════════════════════════════════════════════════════╣${NC}"
        echo -e "${RED}║${NC} Volume group '${YELLOW}$VG_NAME${NC}' does not exist.                          ${RED}║${NC}"
        echo -e "${RED}║${NC}                                                                   ${RED}║${NC}"
        echo -e "${RED}║${NC} Please ensure KubePanel storage is properly configured.           ${RED}║${NC}"
        echo -e "${RED}╚═══════════════════════════════════════════════════════════════════════╝${NC}"
        exit 1
    fi
}

# Display current storage configuration
show_current_storage() {
    print_step "1" "Current Storage Configuration"

    echo -e "\n  ${CYAN}Volume Group:${NC}"
    vgs "$VG_NAME" --units g 2>/dev/null | head -2 | while read -r line; do
        echo "    $line"
    done

    echo -e "\n  ${CYAN}Physical Volumes in $VG_NAME:${NC}"
    pvs --noheadings -o pv_name,pv_size,pv_free -S vg_name="$VG_NAME" --units g 2>/dev/null | while read -r line; do
        echo "    $line"
    done

    echo -e "\n  ${CYAN}Logical Volume:${NC}"
    lvs "$VG_NAME/$LV_NAME" --units g 2>/dev/null | head -2 | while read -r line; do
        echo "    $line"
    done
}

# Find available block devices using lvmdiskscan
find_available_devices() {
    local available_devices=()

    # Use lvmdiskscan to find devices
    # Lines WITHOUT "LVM physical volume" are available
    # Format: /dev/sdd   [     100.00 GiB]
    while IFS= read -r line; do
        # Skip empty lines and summary lines
        [ -z "$line" ] && continue
        echo "$line" | grep -qE "^[[:space:]]*[0-9]+ (disk|partition)" && continue

        # Skip if already an LVM physical volume
        echo "$line" | grep -q "LVM physical volume" && continue

        # Skip loop devices and DRBD devices
        echo "$line" | grep -q "/dev/loop" && continue
        echo "$line" | grep -q "/dev/drbd" && continue

        # Extract device path
        local device
        device=$(echo "$line" | awk '{print $1}')

        [ -z "$device" ] && continue
        [ ! -b "$device" ] && continue

        # Check if it's a whole disk (not a partition) using lsblk
        local dev_type
        dev_type=$(lsblk -dno TYPE "$device" 2>/dev/null)
        [ "$dev_type" != "disk" ] && continue

        # Check size > 1 GiB
        local size_bytes
        size_bytes=$(blockdev --getsize64 "$device" 2>/dev/null)
        [ -z "$size_bytes" ] && continue
        [ "$size_bytes" -lt "$MIN_SIZE_BYTES" ] && continue

        # Get clean human-readable size
        local hr_size
        hr_size=$(lsblk -dno SIZE "$device" 2>/dev/null | tr -d ' ')

        available_devices+=("$device|$hr_size")
    done < <(lvmdiskscan 2>/dev/null)

    # Return the array (only non-empty entries)
    for entry in "${available_devices[@]}"; do
        [ -n "$entry" ] && echo "$entry"
    done
}

# Display available devices and prompt for selection
select_device() {
    print_step "2" "Available Block Devices"

    # Get available devices
    mapfile -t devices < <(find_available_devices)

    if [ ${#devices[@]} -eq 0 ]; then
        echo -e "\n${YELLOW}╔═══════════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${YELLOW}║                        NO DEVICES AVAILABLE                           ║${NC}"
        echo -e "${YELLOW}╠═══════════════════════════════════════════════════════════════════════╣${NC}"
        echo -e "${YELLOW}║${NC} No suitable block devices found. A device must be:                 ${YELLOW}║${NC}"
        echo -e "${YELLOW}║${NC}   - A whole disk (not a partition)                                 ${YELLOW}║${NC}"
        echo -e "${YELLOW}║${NC}   - At least 1 GiB in size                                         ${YELLOW}║${NC}"
        echo -e "${YELLOW}║${NC}   - Not already in use by LVM                                      ${YELLOW}║${NC}"
        echo -e "${YELLOW}║${NC}   - Not mounted or containing a filesystem                         ${YELLOW}║${NC}"
        echo -e "${YELLOW}╚═══════════════════════════════════════════════════════════════════════╝${NC}"
        exit 0
    fi

    echo -e "\n  ${CYAN}Available devices for storage extension:${NC}\n"

    local i=1
    for dev_info in "${devices[@]}"; do
        local dev size
        dev=$(echo "$dev_info" | cut -d'|' -f1)
        size=$(echo "$dev_info" | cut -d'|' -f2)
        printf "    ${GREEN}[%d]${NC} %-20s ${YELLOW}%s${NC}\n" "$i" "$dev" "$size"
        ((i++))
    done

    echo ""

    # Prompt for selection
    local selection
    while true; do
        read -rp "$(printf "${YELLOW}==> Select device number (1-%d) or 'q' to quit: ${NC}" "${#devices[@]}")" selection

        if [ "$selection" = "q" ] || [ "$selection" = "Q" ]; then
            echo -e "\n  ${CYAN}Operation cancelled.${NC}"
            exit 0
        fi

        if [[ "$selection" =~ ^[0-9]+$ ]] && [ "$selection" -ge 1 ] && [ "$selection" -le "${#devices[@]}" ]; then
            break
        fi

        print_error "Invalid selection. Please enter a number between 1 and ${#devices[@]}"
    done

    # Get selected device
    local selected_index=$((selection - 1))
    SELECTED_DEVICE=$(echo "${devices[$selected_index]}" | cut -d'|' -f1)
    SELECTED_SIZE=$(echo "${devices[$selected_index]}" | cut -d'|' -f2)
}

# Confirm device selection
confirm_selection() {
    print_step "3" "Confirm Selection"

    echo -e "\n${RED}╔═══════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║                            WARNING                                    ║${NC}"
    echo -e "${RED}╠═══════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${RED}║${NC} You are about to add the following device to Linstor storage:        ${RED}║${NC}"
    echo -e "${RED}║${NC}                                                                      ${RED}║${NC}"
    printf "${RED}║${NC}   Device: ${YELLOW}%-56s${NC}${RED}║${NC}\n" "$SELECTED_DEVICE"
    printf "${RED}║${NC}   Size:   ${YELLOW}%-56s${NC}${RED}║${NC}\n" "$SELECTED_SIZE"
    echo -e "${RED}║${NC}                                                                      ${RED}║${NC}"
    echo -e "${RED}║${NC} ${RED}ALL DATA ON THIS DEVICE WILL BE PERMANENTLY LOST!${NC}                 ${RED}║${NC}"
    echo -e "${RED}╚═══════════════════════════════════════════════════════════════════════╝${NC}"

    echo ""
    read -rp "$(printf "${YELLOW}==> Type 'yes' to confirm, or anything else to cancel: ${NC}")" confirm

    if [ "$confirm" != "yes" ]; then
        echo -e "\n  ${CYAN}Operation cancelled.${NC}"
        exit 0
    fi
}

# Extend the storage
extend_storage() {
    print_step "4" "Extending Storage"

    # Add device to volume group
    print_progress "Adding $SELECTED_DEVICE to volume group $VG_NAME..."
    if ! vgextend "$VG_NAME" "$SELECTED_DEVICE"; then
        print_error "Failed to extend volume group"
        exit 1
    fi
    print_success "Device added to volume group"

    # Extend the thin pool
    print_progress "Extending thin pool $LV_NAME..."
    if ! lvextend -l +100%FREE "$VG_NAME/$LV_NAME"; then
        print_error "Failed to extend logical volume"
        exit 1
    fi
    print_success "Thin pool extended"
}

# Show final status
show_final_status() {
    print_step "5" "Storage Extension Complete"

    echo -e "\n  ${CYAN}Updated Volume Group:${NC}"
    vgs "$VG_NAME" --units g 2>/dev/null | head -2 | while read -r line; do
        echo "    $line"
    done

    echo -e "\n  ${CYAN}Physical Volumes in $VG_NAME:${NC}"
    pvs --noheadings -o pv_name,pv_size,pv_free -S vg_name="$VG_NAME" --units g 2>/dev/null | while read -r line; do
        echo "    $line"
    done

    echo -e "\n  ${CYAN}Updated Logical Volume:${NC}"
    lvs "$VG_NAME/$LV_NAME" --units g 2>/dev/null | head -2 | while read -r line; do
        echo "    $line"
    done

    echo -e "\n${GREEN}╔═══════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                    STORAGE EXTENSION SUCCESSFUL                       ║${NC}"
    echo -e "${GREEN}╠═══════════════════════════════════════════════════════════════════════╣${NC}"
    printf "${GREEN}║${NC} Added ${YELLOW}%-20s${NC} (${YELLOW}%s${NC}) to Linstor storage.            ${GREEN}║${NC}\n" "$SELECTED_DEVICE" "$SELECTED_SIZE"
    echo -e "${GREEN}║${NC}                                                                      ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC} The additional storage is now available for new domains.            ${GREEN}║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════════════════╝${NC}"
}

main() {
    print_header

    if [ "$DEBUG_MODE" = "true" ]; then
        echo -e "${YELLOW}Debug mode enabled - showing all command output${NC}\n"
    fi

    # Check prerequisites
    check_root
    check_vg_exists

    # Show current configuration
    show_current_storage

    # Find and select device
    select_device

    # Confirm selection
    confirm_selection

    # Extend storage
    extend_storage

    # Show final status
    show_final_status
}

main "$@"
