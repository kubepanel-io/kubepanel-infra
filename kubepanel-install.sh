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

# ──────────────── Environment Variables for Non-Interactive Mode ────────────────
# These can be set to run the installer without prompts (for SaaS automation)
STORAGE_DEVICE=${STORAGE_DEVICE:-}
DJANGO_SUPERUSER_EMAIL=${DJANGO_SUPERUSER_EMAIL:-}
DJANGO_SUPERUSER_USERNAME=${DJANGO_SUPERUSER_USERNAME:-}
DJANGO_SUPERUSER_PASSWORD=${DJANGO_SUPERUSER_PASSWORD:-}
KUBEPANEL_DOMAIN=${KUBEPANEL_DOMAIN:-}

# ASCII Art and Progress Functions
print_header() {
    echo -e "${CYAN}"
    echo "╔═══════════════════════════════════════════════════════════════════════╗"
    echo "║                         KUBEPANEL INSTALLER                           ║"
    echo "║              Kubernetes based web hosting control panel               ║"
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
        echo -e "${RED}║${NC}   ${YELLOW}sudo su - ${NC}                                              ${RED}║${NC}"
        echo -e "${RED}╚═══════════════════════════════════════════════════════════════════════╝${NC}"
        exit 1
    fi
}

prompt_password() {
    local prompt_message=$1
    local var_name=$2
    local password
    local password_confirm
    
    while true; do
        printf "${YELLOW}==> %s: ${NC}" "$prompt_message"
        read -s password
        echo ""
        
        printf "${YELLOW}==> Re-enter password to confirm: ${NC}"
        read -s password_confirm
        echo ""
        
        if [ "$password" = "$password_confirm" ]; then
            eval "$var_name='$password'"
            print_success "Password confirmed"
            break
        else
            echo -e "  ${RED}✗${NC} Passwords do not match. Please try again."
            echo ""
        fi
    done
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

print_waiting() {
    local message=$1
    echo -e "  ${YELLOW}⏳${NC} $message"
}

print_success() {
    local message=$1
    echo -e "  ${GREEN}✓${NC} $message"
}

print_spinner() {
    local pid=$1
    local message=$2
    local spin='-\|/'
    local i=0
    while kill -0 $pid 2>/dev/null; do
        i=$(( (i+1) %4 ))
        printf "\r  ${YELLOW}${spin:$i:1}${NC} $message"
        sleep .1
    done
    printf "\r  ${GREEN}✓${NC} $message\n"
}

# Function to run commands with optional debug output
run_cmd() {
    if [ "$DEBUG_MODE" = "true" ]; then
        "$@"
    else
        "$@" >/dev/null 2>&1
    fi
}

# Function to run critical commands that must succeed
run_cmd_critical() {
    if [ "$DEBUG_MODE" = "true" ]; then
        "$@"
    else
        "$@" >/dev/null 2>&1
    fi
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo -e "\n  ${RED}✗${NC} Critical command failed with exit code $exit_code: $*"
        exit 1
    fi
    return 0
}

# Function to run commands and capture exit status while hiding output
run_cmd_check() {
    if [ "$DEBUG_MODE" = "true" ]; then
        "$@"
    else
        "$@" >/dev/null 2>&1
    fi
    return $?
}

# Version parameter handling
# Usage:
#   kubepanel-install.sh           # installs latest release
#   kubepanel-install.sh v1.2.0    # installs specific version
#   kubepanel-install.sh dev       # installs from main branch
GITHUB_REPO="kubepanel-io/kubepanel-infra"
VERSION_PARAM="$1"

if [ "$VERSION_PARAM" == "dev" ]; then
    # Development mode: use main branch
    INSTALL_VERSION="main"
    GITHUB_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/refs/heads/main/kubepanel-install.yaml"
    LICENSE_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/refs/heads/main/kubepanel-license-crd.yaml"
    echo -e "${YELLOW}Installing from development branch (main)${NC}"
elif [ -n "$VERSION_PARAM" ]; then
    # Specific version provided
    INSTALL_VERSION="$VERSION_PARAM"
    GITHUB_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/refs/tags/${VERSION_PARAM}/kubepanel-install.yaml"
    LICENSE_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/refs/heads/main/kubepanel-license-crd.yaml"
    echo -e "${GREEN}Installing version: ${VERSION_PARAM}${NC}"
else
    # No version specified: fetch latest release from GitHub API
    echo -e "${CYAN}Fetching latest release version...${NC}"
    LATEST_VERSION=$(curl -s "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
    if [ -z "$LATEST_VERSION" ]; then
        echo -e "${YELLOW}Could not fetch latest release, using main branch${NC}"
        INSTALL_VERSION="main"
        GITHUB_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/refs/heads/main/kubepanel-install.yaml"
        LICENSE_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/refs/heads/main/kubepanel-license-crd.yaml"
    else
        INSTALL_VERSION="$LATEST_VERSION"
        GITHUB_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/refs/tags/${LATEST_VERSION}/kubepanel-install.yaml"
        LICENSE_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/refs/heads/main/kubepanel-license-crd.yaml"
        echo -e "${GREEN}Installing latest release: ${LATEST_VERSION}${NC}"
    fi
fi

prompt_user_input() {
    local prompt_message=$1
    local var_name=$2
    read -rp "$(printf "${YELLOW}==> %s: ${NC}" "$prompt_message")" $var_name
}

download_yaml() {
    local url=$1
    local output_file=$2
    run_cmd_check curl -o "$output_file" "$url"

    if [ $? -ne 0 ]; then
        echo "Failed to download the file from $url."
        exit 1
    fi
}

get_external_ips() {
    # Wait a moment for ConfigMap to be populated
    sleep 5
    
    # Get external IPs from ConfigMap
    EXTERNAL_IPS=()
    if [ "$DEBUG_MODE" = "true" ]; then
        mapfile -t EXTERNAL_IPS < <(microk8s kubectl get configmap node-public-ips -n kubepanel -o jsonpath='{.data}' | grep -oE '"[^"]+":"[^"]+"' | sed 's/"//g' | sed 's/:/: /')
    else
        mapfile -t EXTERNAL_IPS < <(microk8s kubectl get configmap node-public-ips -n kubepanel -o jsonpath='{.data}' 2>/dev/null | grep -oE '"[^"]+":"[^"]+"' | sed 's/"//g' | sed 's/:/: /')
    fi
}


replace_placeholders() {
    local file=$1
    local email=$2
    local username=$3
    local password=$4
    local domain=$5
    local mariadbpass=$(openssl rand -base64 15)
    local mariadbpass_rc=$(openssl rand -base64 15)
    local djangosecretkey=$(openssl rand -base64 45)

    echo "Waiting for 3 nodes to report InternalIP…"
    while true; do
      sleep 5
      if [ "$DEBUG_MODE" = "true" ]; then
          mapfile -t node_ips < <(microk8s kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' | head -n 3)
      else
          mapfile -t node_ips < <(microk8s kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' 2>/dev/null | head -n 3)
      fi
      if [[ -n "${node_ips[0]}" && -n "${node_ips[1]}" && -n "${node_ips[2]}" ]]; then
        break
      fi
      echo "  still waiting…"
    done
    local node1_ip=${node_ips[0]}
    local node2_ip=${node_ips[1]}
    local node3_ip=${node_ips[2]}

    # Label first node for SMTP pod affinity (mx0 = primary MX endpoint)
    # Note: kubectl jsonpath doesn't support nested filters, so we use a loop approach
    local node1_name=""
    while read -r name ip; do
      if [[ "$ip" == "$node1_ip" ]]; then
        node1_name="$name"
        break
      fi
    done < <(microk8s kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}')

    if [[ -n "$node1_name" ]]; then
      run_cmd microk8s kubectl label node "$node1_name" mx0=true --overwrite
    fi

    run_cmd sed -i "s,<DJANGO_SUPERUSER_EMAIL>,$email,g" "$file"
    run_cmd sed -i "s,<DJANGO_SUPERUSER_USERNAME>,$username,g" "$file"
    run_cmd sed -i "s,<DJANGO_SUPERUSER_PASSWORD>,$password,g" "$file"
    run_cmd sed -i "s,<KUBEPANEL_DOMAIN>,$domain,g" "$file"
    run_cmd sed -i "s,<MARIADB_ROOT_PASSWORD>,$mariadbpass,g" "$file"
    run_cmd sed -i "s,<MARIADB_ROOT_PASSWORD_RC>,$mariadbpass_rc,g" "$file"
    run_cmd sed -i "s,<DJANGO_SECRET_KEY>,$djangosecretkey,g" "$file"
    run_cmd sed -i "s,<NODE_1_IP>,$node1_ip,g" "$file"
    run_cmd sed -i "s,<NODE_2_IP>,$node2_ip,g" "$file"
    run_cmd sed -i "s,<NODE_3_IP>,$node3_ip,g" "$file"
    run_cmd sed -i "s,<KUBEPANEL_ALERT_EMAIL>,$email,g" "$file"
}

check_deployment_status() {
    DEPLOYMENT="kubepanel"
    NAMESPACE="kubepanel"
    print_waiting "Waiting for Kubepanel deployment to be ready (15-20 minutes)..."
    while true; do
        if [ "$DEBUG_MODE" = "true" ]; then
            STATUS=$(microk8s kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Available")].status}')
        else
            STATUS=$(microk8s kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null)
        fi
        if [ "$STATUS" == "True" ]; then
            print_success "Deployment $DEPLOYMENT is ready"
            break
        else
            echo -e "    ${YELLOW}⏳${NC} $(date): Deployment still starting up..."
        fi
        sleep 15
    done
}

generate_join_command() {
    print_progress "Generating cluster join command..."
    # Generate a token with a longer TTL (e.g., 1 hour) so multiple nodes can join using the same token
    if [ "$DEBUG_MODE" = "true" ]; then
        JOIN_COMMAND=$(microk8s add-node --token-ttl 3600 | grep "microk8s join" | head -n 1)
    else
        JOIN_COMMAND=$(microk8s add-node --token-ttl 3600 2>/dev/null | grep "microk8s join" | head -n 1)
    fi

    # Write to file for local reference
    echo "$JOIN_COMMAND" > /tmp/kubepanel_join_command.txt

    # Output with parseable prefix so automation (Python/SSH) can capture it
    echo "### KUBEPANEL_JOIN_COMMAND: $JOIN_COMMAND"

    # Also display in pretty format for interactive users
    echo -e "\n${GREEN}╔════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                     CLUSTER JOIN COMMAND                           ║${NC}"
    echo -e "${GREEN}╠════════════════════════════════════════════════════════════════════╣${NC}"
    printf "${GREEN}║${NC} ${YELLOW}%s${NC}\n" "$JOIN_COMMAND"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════════════╝${NC}\n"
}

wait_for_ha_status() {
    print_waiting "Waiting for the cluster to achieve high availability..."
    while true; do
        if [ "$DEBUG_MODE" = "true" ]; then
            HA_STATUS=$(microk8s status | grep 'high-availability' | awk '{print $2}')
        else
            HA_STATUS=$(microk8s status 2>/dev/null | grep 'high-availability' | awk '{print $2}')
        fi
        if [ "$HA_STATUS" == "yes" ]; then
            print_success "High Availability is enabled"
            break
        else
            echo -e "    ${YELLOW}⏳${NC} $(date): Waiting for additional nodes to join..."
        fi
        sleep 15
    done
}

main() {
    print_header
    
    if [ "$DEBUG_MODE" = "true" ]; then
        echo -e "${YELLOW}🐛 Debug mode enabled - showing all command output${NC}\n"
    fi
    
    print_step "0" "Privilege Check"
    print_progress "Verifying root privileges..."
    check_root
    print_success "Running with root privileges"

    # ──────────────── Configuration Collection ────────────────
    # If env vars are not set, fall back to interactive prompts (manual installs)
    if [ -z "$DJANGO_SUPERUSER_EMAIL" ] || [ -z "$DJANGO_SUPERUSER_USERNAME" ] || \
       [ -z "$DJANGO_SUPERUSER_PASSWORD" ] || [ -z "$KUBEPANEL_DOMAIN" ]; then
        if [ -t 0 ]; then
            # Running in a terminal — use interactive prompts
            echo -e "\n${CYAN}Please provide the following configuration details:${NC}"
            prompt_user_input "Enter Superuser email address" DJANGO_SUPERUSER_EMAIL
            prompt_user_input "Enter Superuser username" DJANGO_SUPERUSER_USERNAME
            prompt_password "Enter Superuser password" DJANGO_SUPERUSER_PASSWORD
            prompt_user_input "Enter Kubepanel domain name" KUBEPANEL_DOMAIN
            prompt_user_input "Enter storage device name [default: /dev/sdb]" _STORAGE_INPUT
            STORAGE_DEVICE=${_STORAGE_INPUT:-/dev/sdb}
        else
            echo -e "${RED}ERROR: Required environment variables not set for non-interactive mode.${NC}"
            echo -e "${YELLOW}Required variables:${NC}"
            echo "  DJANGO_SUPERUSER_EMAIL"
            echo "  DJANGO_SUPERUSER_USERNAME"
            echo "  DJANGO_SUPERUSER_PASSWORD"
            echo "  KUBEPANEL_DOMAIN"
            echo "  STORAGE_DEVICE (optional, defaults to /dev/sdb)"
            exit 1
        fi
    else
        # Non-interactive mode - use env vars
        STORAGE_DEVICE=${STORAGE_DEVICE:-/dev/sdb}
        print_success "Using configuration from environment variables"
        echo -e "  Domain: ${GREEN}$KUBEPANEL_DOMAIN${NC}"
        echo -e "  Admin email: ${GREEN}$DJANGO_SUPERUSER_EMAIL${NC}"
        echo -e "  Storage device: ${GREEN}$STORAGE_DEVICE${NC}"
    fi

    print_step "1" "System Preparation"
    print_progress "Stopping and disabling multipathd..."
    run_cmd sudo systemctl stop multipathd
    run_cmd sudo systemctl disable multipathd
    print_success "System services configured"
    
    print_progress "Downloading kubectl..."
    run_cmd_check curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
    run_cmd chmod +x kubectl
    run_cmd sudo mv kubectl /bin
    print_success "kubectl installed"
    
    print_step "2" "Package Installation"
    print_progress "Updating package repositories..."
    run_cmd sudo apt update
    print_progress "Installing dependencies (git, lvm2)..."
    run_cmd sudo apt install git lvm2 linux-headers-$(uname -r) -y
    print_success "Dependencies installed"
    
    print_step "3" "MicroK8S Installation"
    print_progress "Installing MicroK8S (this may take a few minutes)..."
    run_cmd sudo snap install microk8s --classic --channel=1.31
    print_success "MicroK8S installed"
    
    print_waiting "Waiting for MicroK8S to be ready..."
    run_cmd sudo microk8s status --wait-ready
    print_success "MicroK8S is ready"
    
    print_step "4" "Kubernetes Configuration"
    print_progress "Enabling ingress addon..."
    run_cmd sudo microk8s enable ingress
    print_progress "Enabling cert-manager addon..."
    run_cmd sudo microk8s enable cert-manager
    print_progress "Enabling metrics-server addon..."
    run_cmd sudo microk8s enable metrics-server
    print_progress "Configuring kubectl access..."
    sudo microk8s config > ~/.kube/config
    print_success "Kubernetes addons enabled"

    print_step "5" "High Availability Setup"
    generate_join_command
    wait_for_ha_status
    
    print_step "6" "Storage Configuration"
    print_progress "Setting up LVM storage on $STORAGE_DEVICE..."
    if [ ! -b "$STORAGE_DEVICE" ]; then
        echo -e "  ${RED}✗${NC} Error: Device $STORAGE_DEVICE does not exist or is not a block device"
        echo -e "  ${YELLOW}Available block devices:${NC}"
        lsblk -d -o NAME,SIZE,TYPE | grep disk
        exit 1
    fi
    if vgs linstorvg >/dev/null 2>&1; then
        echo -e "  ${YELLOW}⚠${NC} Volume group 'linstorvg' already exists, skipping creation"
    else
        run_cmd_critical vgcreate linstorvg "$STORAGE_DEVICE"
    fi
    if lvs linstorvg/linstorlv >/dev/null 2>&1; then
        echo -e "  ${YELLOW}⚠${NC} Logical volume 'linstorlv' already exists, skipping creation"
    else
        run_cmd_critical lvcreate -l100%FREE -T linstorvg/linstorlv
    fi
    print_success "Storage configured"
    
    print_step "7" "Kubernetes Operators"
    print_progress "Installing Piraeus storage operator..."
    run_cmd_critical kubectl apply --server-side -k "https://github.com/piraeusdatastore/piraeus-operator//config/default?ref=v2.9.0"
    print_progress "Installing snapshot controller..."
    run_cmd_critical kubectl apply -k https://github.com/kubernetes-csi/external-snapshotter//client/config/crd
    run_cmd_critical kubectl apply -k https://github.com/kubernetes-csi/external-snapshotter//deploy/kubernetes/snapshot-controller
    print_success "Kubernetes operators installed"
    
    print_step "8" "Kubepanel Configuration"
    YAML_FILE="kubepanel-install.yaml"
    LICENSE_CRD="kubepanel-license-crd.yaml"
    print_progress "Downloading Kubepanel configuration..."
    download_yaml "$GITHUB_URL" "$YAML_FILE"
    download_yaml "$LICENSE_URL" "$LICENSE_CRD"
    print_progress "Customizing configuration..."
    replace_placeholders "$YAML_FILE" "$DJANGO_SUPERUSER_EMAIL" "$DJANGO_SUPERUSER_USERNAME" "$DJANGO_SUPERUSER_PASSWORD" "$KUBEPANEL_DOMAIN"
    print_success "Configuration prepared"
    
    print_step "9" "Deployment"
    print_waiting "Waiting for Piraeus operator to be ready (up to 3 minutes)..."
    run_cmd microk8s kubectl wait pod --for=condition=Ready --timeout=180s -n piraeus-datastore -l app.kubernetes.io/component=piraeus-operator
    print_success "Piraeus operator ready"
    
    print_progress "Deploying Kubepanel..."
    
    run_cmd_critical kubectl apply -f $LICENSE_CRD
    run_cmd microk8s kubectl wait --for=condition=Established crd/licenses.kubepanel.io --timeout=30s
    run_cmd_critical kubectl apply -f $YAML_FILE
    
    check_deployment_status
    
    print_waiting "Finalizing storage setup (10-15 minutes)..."
    run_cmd microk8s kubectl delete daemonset node-ip-updater -n kubepanel

    print_progress "Retrieving external IP addresses..."
    get_external_ips

    print_step "10" "Installing KubePanel Aliases"
    print_progress "Adding management aliases to /root/.bashrc..."

    # Add KubePanel aliases if not already present
    if ! grep -q "# KubePanel management aliases" /root/.bashrc 2>/dev/null; then
        cat >> /root/.bashrc << 'EOF'

# KubePanel management aliases
alias kp-restart='microk8s kubectl rollout restart deployment/kubepanel -n kubepanel'
alias kp-logs='microk8s kubectl logs -f deployment/kubepanel -n kubepanel'
alias kp-status='microk8s kubectl get pods -n kubepanel -l app=kubepanel'
EOF
        print_success "Aliases added to /root/.bashrc"
    else
        print_success "Aliases already configured"
    fi

    print_step "11" "Installing Management Tools"
    print_progress "Installing KubePanel management scripts..."

    # Use the same version/branch as the main install
    if [ "$INSTALL_VERSION" == "main" ]; then
        SCRIPT_BASE_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/refs/heads/main"
    else
        SCRIPT_BASE_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/refs/tags/${INSTALL_VERSION}"
    fi

    run_cmd curl -sSL "$SCRIPT_BASE_URL/kubepanel-extend-storage.sh" -o /usr/local/bin/kubepanel-extend-storage
    run_cmd curl -sSL "$SCRIPT_BASE_URL/kubepanel-drain-node.sh" -o /usr/local/bin/kubepanel-drain-node
    run_cmd chmod +x /usr/local/bin/kubepanel-extend-storage
    run_cmd chmod +x /usr/local/bin/kubepanel-drain-node
    print_success "Management tools installed"

    # Final success message with DNS instructions
    echo -e "\n${GREEN}"
    echo "╔═══════════════════════════════════════════════════════════════════════╗"
    echo "║                    🎉 INSTALLATION COMPLETED! 🎉                      ║"
    echo "║                                                                       ║"
    echo "║  Kubepanel is now ready to use at: https://$KUBEPANEL_DOMAIN"
    echo "║  Login with your configured credentials                               ║"
    echo "╚═══════════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"

    # Display node IPs and DNS instructions
    echo -e "\n${BLUE}╔════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║                          DNS CONFIGURATION                         ║${NC}"
    echo -e "${BLUE}╠════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${BLUE}║${NC}"

    # Display external IPs if available
    if [ ${#EXTERNAL_IPS[@]} -gt 0 ]; then
        echo -e "${BLUE}║${NC} ${YELLOW}External Node IPs:${NC}"
        for ip_mapping in "${EXTERNAL_IPS[@]}"; do
            printf "${BLUE}║${NC}   ${GREEN}%-60s${NC}${BLUE}║${NC}\n" "$ip_mapping"
        done
        echo -e "${BLUE}║${NC}"
        echo -e "${BLUE}║${NC} ${YELLOW}DNS Setup Required:${NC}"
        echo -e "${BLUE}║${NC}   Create an A record for: ${GREEN}$KUBEPANEL_DOMAIN${NC}"
        echo -e "${BLUE}║${NC}   Create an A record for: ${GREEN}webmail.$KUBEPANEL_DOMAIN${NC}"
        echo -e "${BLUE}║${NC}   Create an A record for: ${GREEN}phpmyadmin.$KUBEPANEL_DOMAIN${NC}"
        echo -e "${BLUE}║${NC}   Point it to at least one of the ${YELLOW}EXTERNAL${NC} IP addresses above"
    else
        echo -e "${BLUE}║${NC} ${YELLOW}DNS Setup Required:${NC}"
        echo -e "${BLUE}║${NC}   Create an A record for: ${GREEN}$KUBEPANEL_DOMAIN${NC}"
        echo -e "${BLUE}║${NC}   Create an A record for: ${GREEN}webmail.$KUBEPANEL_DOMAIN${NC}"
        echo -e "${BLUE}║${NC}   Create an A record for: ${GREEN}phpmyadmin.$KUBEPANEL_DOMAIN${NC}"
        echo -e "${BLUE}║${NC}   Point it to at least one of the internal IP addresses above"
        echo -e "${BLUE}║${NC}   ${YELLOW}Note:${NC} External IPs not yet available in ConfigMap"
    fi

    echo -e "${BLUE}║${NC}"
    echo -e "${BLUE}║${NC} ${RED}⚠️  Important:${NC} Kubepanel will not be accessible until"
    echo -e "${BLUE}║${NC}   the DNS record is configured correctly!"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════════════╝${NC}"

    # Display management aliases info
    echo -e "\n${BLUE}╔════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║                       MANAGEMENT COMMANDS                          ║${NC}"
    echo -e "${BLUE}╠════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${BLUE}║${NC} ${YELLOW}Available aliases (run 'source ~/.bashrc' to activate):${NC}"
    echo -e "${BLUE}║${NC}   ${GREEN}kp-restart${NC}  - Restart the KubePanel dashboard"
    echo -e "${BLUE}║${NC}   ${GREEN}kp-logs${NC}     - Follow KubePanel dashboard logs"
    echo -e "${BLUE}║${NC}   ${GREEN}kp-status${NC}   - Check KubePanel pod status"
    echo -e "${BLUE}║${NC}"
    echo -e "${BLUE}║${NC} ${YELLOW}Storage management:${NC}"
    echo -e "${BLUE}║${NC}   ${GREEN}kubepanel-extend-storage${NC}  - Add disk to Linstor storage"
    echo -e "${BLUE}║${NC}"
    echo -e "${BLUE}║${NC} ${YELLOW}Node management:${NC}"
    echo -e "${BLUE}║${NC}   ${GREEN}kubepanel-drain-node${NC}      - Drain node for maintenance"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════════════╝${NC}"

    echo -e "\n${CYAN}Please configure your DNS and then access Kubepanel at:${NC}"
    echo -e "${GREEN}https://$KUBEPANEL_DOMAIN${NC}\n"

    # Create completion marker for automated provisioning systems
    touch /var/log/kubepanel-install-complete
}

main
