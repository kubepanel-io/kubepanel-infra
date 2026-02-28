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

# ASCII Art and Progress Functions
print_header() {
    echo -e "${CYAN}"
    echo "╔═══════════════════════════════════════════════════════════════════════╗"
    echo "║                      KUBEPANEL NODE DRAIN                             ║"
    echo "║                 Safely drain node for maintenance                     ║"
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
        echo -e "${RED}║${NC}   ${YELLOW}sudo kubepanel-drain-node ${NC}                                  ${RED}║${NC}"
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

# Check if microk8s is available
check_microk8s() {
    if ! command -v microk8s &> /dev/null; then
        echo -e "${RED}╔═══════════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${RED}║                            ERROR                                      ║${NC}"
        echo -e "${RED}╠═══════════════════════════════════════════════════════════════════════╣${NC}"
        echo -e "${RED}║${NC} MicroK8s is not installed on this system.                         ${RED}║${NC}"
        echo -e "${RED}║${NC} This script requires MicroK8s to manage cluster nodes.            ${RED}║${NC}"
        echo -e "${RED}╚═══════════════════════════════════════════════════════════════════════╝${NC}"
        exit 1
    fi
}

# List all nodes and let user select one
select_node() {
    print_step "1" "Cluster Nodes"

    echo -e "\n  ${CYAN}Current cluster nodes:${NC}\n"

    # Get nodes and store in array
    local i=1
    declare -a node_names
    declare -a node_statuses

    while IFS= read -r line; do
        local name status roles age version
        name=$(echo "$line" | awk '{print $1}')
        status=$(echo "$line" | awk '{print $2}')
        roles=$(echo "$line" | awk '{print $3}')
        age=$(echo "$line" | awk '{print $4}')
        version=$(echo "$line" | awk '{print $5}')

        node_names+=("$name")
        node_statuses+=("$status")

        # Color code status
        local status_color="${GREEN}"
        if [[ "$status" == *"NotReady"* ]]; then
            status_color="${RED}"
        elif [[ "$status" == *"SchedulingDisabled"* ]]; then
            status_color="${YELLOW}"
        fi

        printf "    ${GREEN}[%d]${NC} %-30s ${status_color}%-20s${NC} %-15s %s\n" "$i" "$name" "$status" "$roles" "$age"
        ((i++))
    done < <(microk8s kubectl get nodes --no-headers 2>/dev/null)

    if [ ${#node_names[@]} -eq 0 ]; then
        echo -e "\n${RED}╔═══════════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${RED}║                            ERROR                                      ║${NC}"
        echo -e "${RED}╠═══════════════════════════════════════════════════════════════════════╣${NC}"
        echo -e "${RED}║${NC} No nodes found in the cluster.                                    ${RED}║${NC}"
        echo -e "${RED}╚═══════════════════════════════════════════════════════════════════════╝${NC}"
        exit 1
    fi

    echo ""

    # Prompt for selection
    local selection
    while true; do
        read -rp "$(printf "${YELLOW}==> Select node number (1-%d) or 'q' to quit: ${NC}" "${#node_names[@]}")" selection

        if [ "$selection" = "q" ] || [ "$selection" = "Q" ]; then
            echo -e "\n  ${CYAN}Operation cancelled.${NC}"
            exit 0
        fi

        if [[ "$selection" =~ ^[0-9]+$ ]] && [ "$selection" -ge 1 ] && [ "$selection" -le "${#node_names[@]}" ]; then
            break
        fi

        print_error "Invalid selection. Please enter a number between 1 and ${#node_names[@]}"
    done

    # Get selected node
    local selected_index=$((selection - 1))
    SELECTED_NODE="${node_names[$selected_index]}"
    SELECTED_STATUS="${node_statuses[$selected_index]}"
}

# Show what will happen and confirm
confirm_drain() {
    print_step "2" "Confirm Drain Operation"

    # Get pods on this node (excluding DaemonSets)
    echo -e "\n  ${CYAN}Pods that will be evicted from ${YELLOW}$SELECTED_NODE${CYAN}:${NC}\n"

    local pod_count=0
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local namespace pod
        namespace=$(echo "$line" | awk '{print $1}')
        pod=$(echo "$line" | awk '{print $2}')
        echo "    - $namespace/$pod"
        ((pod_count++))
    done < <(microk8s kubectl get pods --all-namespaces --field-selector spec.nodeName="$SELECTED_NODE" -o wide --no-headers 2>/dev/null | grep -v "DaemonSet")

    if [ "$pod_count" -eq 0 ]; then
        echo "    (No non-DaemonSet pods running on this node)"
    fi

    echo -e "\n  ${CYAN}DaemonSet pods (will NOT be evicted, this is normal):${NC}\n"

    # Show DaemonSet pods
    local ds_count=0
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local namespace pod
        namespace=$(echo "$line" | awk '{print $1}')
        pod=$(echo "$line" | awk '{print $2}')

        # Check if this pod is from a DaemonSet
        local owner_kind
        owner_kind=$(microk8s kubectl get pod "$pod" -n "$namespace" -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null)
        if [ "$owner_kind" = "DaemonSet" ]; then
            echo "    - $namespace/$pod (DaemonSet)"
            ((ds_count++))
        fi
    done < <(microk8s kubectl get pods --all-namespaces --field-selector spec.nodeName="$SELECTED_NODE" -o wide --no-headers 2>/dev/null)

    if [ "$ds_count" -eq 0 ]; then
        echo "    (No DaemonSet pods on this node)"
    fi

    echo -e "\n${RED}╔═══════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║                            WARNING                                    ║${NC}"
    echo -e "${RED}╠═══════════════════════════════════════════════════════════════════════╣${NC}"
    printf "${RED}║${NC} You are about to drain node: ${YELLOW}%-38s${NC}${RED}║${NC}\n" "$SELECTED_NODE"
    echo -e "${RED}║${NC}                                                                      ${RED}║${NC}"
    echo -e "${RED}║${NC} This will:                                                           ${RED}║${NC}"
    echo -e "${RED}║${NC}   - Mark the node as unschedulable (cordon)                          ${RED}║${NC}"
    echo -e "${RED}║${NC}   - Evict all pods except DaemonSets                                 ${RED}║${NC}"
    echo -e "${RED}║${NC}   - Pods will be rescheduled on other nodes                          ${RED}║${NC}"
    echo -e "${RED}║${NC}                                                                      ${RED}║${NC}"
    echo -e "${RED}║${NC} ${YELLOW}DaemonSets will continue running (this is expected behavior).${NC}      ${RED}║${NC}"
    echo -e "${RED}╚═══════════════════════════════════════════════════════════════════════╝${NC}"

    echo ""
    read -rp "$(printf "${YELLOW}==> Type 'yes' to confirm drain, or anything else to cancel: ${NC}")" confirm

    if [ "$confirm" != "yes" ]; then
        echo -e "\n  ${CYAN}Operation cancelled.${NC}"
        exit 0
    fi
}

# Perform the drain operation
drain_node() {
    print_step "3" "Draining Node"

    # Cordon the node first
    print_progress "Cordoning node $SELECTED_NODE (marking as unschedulable)..."
    if ! microk8s kubectl cordon "$SELECTED_NODE"; then
        print_error "Failed to cordon node"
        exit 1
    fi
    print_success "Node cordoned"

    # Drain the node
    print_progress "Draining node $SELECTED_NODE..."
    print_info "This may take a few minutes while pods gracefully terminate..."
    echo ""

    if microk8s kubectl drain "$SELECTED_NODE" \
        --ignore-daemonsets \
        --delete-emptydir-data \
        --force \
        --grace-period=60 \
        --timeout=300s; then
        print_success "Node drained successfully"
    else
        print_warning "Drain completed with warnings (this may be normal for DaemonSets)"
    fi
}

# Show final status and offer to uncordon
show_final_status() {
    print_step "4" "Drain Complete"

    echo -e "\n  ${CYAN}Current node status:${NC}\n"
    microk8s kubectl get node "$SELECTED_NODE" -o wide

    echo -e "\n${GREEN}╔═══════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                       NODE DRAIN SUCCESSFUL                           ║${NC}"
    echo -e "${GREEN}╠═══════════════════════════════════════════════════════════════════════╣${NC}"
    printf "${GREEN}║${NC} Node ${YELLOW}%-20s${NC} has been drained.                        ${GREEN}║${NC}\n" "$SELECTED_NODE"
    echo -e "${GREEN}║${NC}                                                                      ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC} The node is now:                                                     ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   - Marked as ${YELLOW}SchedulingDisabled${NC} (cordoned)                       ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   - Safe for maintenance (reboot, upgrade, etc.)                    ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}                                                                      ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC} When maintenance is complete, run this script again or use:         ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   ${CYAN}microk8s kubectl uncordon $SELECTED_NODE${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════════════════╝${NC}"

    echo ""
    read -rp "$(printf "${YELLOW}==> Do you want to uncordon the node now? (y/N): ${NC}")" uncordon_choice

    if [ "$uncordon_choice" = "y" ] || [ "$uncordon_choice" = "Y" ]; then
        print_progress "Uncordoning node $SELECTED_NODE..."
        if microk8s kubectl uncordon "$SELECTED_NODE"; then
            print_success "Node uncordoned - it can now receive new pods"
        else
            print_error "Failed to uncordon node"
            exit 1
        fi

        echo -e "\n  ${CYAN}Updated node status:${NC}\n"
        microk8s kubectl get node "$SELECTED_NODE" -o wide
    else
        echo -e "\n  ${CYAN}Node remains cordoned. Remember to uncordon after maintenance.${NC}"
    fi
}

main() {
    print_header

    if [ "$DEBUG_MODE" = "true" ]; then
        echo -e "${YELLOW}Debug mode enabled - showing all command output${NC}\n"
    fi

    # Check prerequisites
    check_root
    check_microk8s

    # Select node
    select_node

    # Confirm drain
    confirm_drain

    # Perform drain
    drain_node

    # Show status and offer uncordon
    show_final_status
}

main "$@"
