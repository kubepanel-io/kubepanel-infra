#!/bin/bash
set -uo pipefail

DIR="/kubepanel"
DKIMDIR="/dkim-privkeys/$KUBEPANEL_DOMAIN"
INIT_MARKER="$DIR/.init-complete"
INIT_LOG="$DIR/.init.log"

# Logging function - writes to both stdout and log file (if it exists)
log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    [ -f "$INIT_LOG" ] && echo "$msg" >> "$INIT_LOG"
    return 0
}

log_error() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*"
    echo "$msg" >&2
    [ -f "$INIT_LOG" ] && echo "$msg" >> "$INIT_LOG"
    return 0
}

# Check if initialization was already completed successfully
if [ -f "$INIT_MARKER" ]; then
    log "Initialization already completed (marker file exists). Skipping."
    exit 0
fi

log "Starting KubePanel initialization..."

# Step 1: Create databases
log "Step 1: Creating databases..."
if ! mysql -h mariadb.kubepanel.svc.cluster.local -uroot -p"$MARIADB_ROOT_PASSWORD" \
    -e "CREATE DATABASE IF NOT EXISTS $DBNAME; GRANT ALL PRIVILEGES ON $DBNAME.* TO '$DBNAME'@'%' IDENTIFIED BY '$MARIADB_ROOT_PASSWORD'"; then
    log_error "Failed to create database $DBNAME"
    exit 1
fi

if ! mysql -h mariadb.kubepanel.svc.cluster.local -uroot -p"$MARIADB_ROOT_PASSWORD" \
    -e "CREATE DATABASE IF NOT EXISTS $DBNAME_RC; GRANT ALL PRIVILEGES ON $DBNAME_RC.* TO '$DBNAME_RC'@'%' IDENTIFIED BY '$MARIADB_ROOT_PASSWORD_RC'"; then
    log_error "Failed to create database $DBNAME_RC"
    exit 1
fi
log "Databases created successfully."

# Check if directory needs initialization
if [ ! -d "$DIR" ]; then
    log_error "Directory $DIR does not exist!"
    exit 1
fi

entries=$(ls -A "$DIR" 2>/dev/null | grep -v "^\.init" || true)
if [ -z "$entries" ] || [ "$entries" = "lost+found" ]; then
    log "Directory is empty or only has lost+found. Starting fresh initialization..."

    # Clean up lost+found if present
    if [ -d "$DIR/lost+found" ]; then
        rmdir "$DIR/lost+found" 2>/dev/null || true
    fi

    # Debug: show what's in the directory
    log "Directory contents before clone:"
    ls -la "$DIR" 2>&1 | while read line; do log "  $line"; done

    # Step 2: Clone repository
    # Use git clone into current directory since $DIR already exists (it's a PVC mount)
    log "Step 2: Cloning repository..."
    cd "$DIR"
    if ! git clone https://github.com/kubepanel-io/kubepanel.git .; then
        log_error "Failed to clone repository"
        # Clean up partial clone
        rm -rf "$DIR"/* "$DIR"/.[!.]* 2>/dev/null || true
        exit 1
    fi
    # Now that directory has content, create log file for persistent logging
    touch "$INIT_LOG"
    log "Repository cloned successfully."

    # Step 3: Checkout version (uses KUBEPANEL_VERSION env var, defaults to v1.0.0)
    CHECKOUT_VERSION="${KUBEPANEL_VERSION:-v1.0.0}"
    log "Step 3: Checking out $CHECKOUT_VERSION..."
    if ! git checkout "$CHECKOUT_VERSION"; then
        log_error "Failed to checkout $CHECKOUT_VERSION"
        exit 1
    fi

    # Create yaml_templates directory
    mkdir -p "$DIR/yaml_templates"

    # Step 4: Configure settings
    log "Step 4: Configuring settings.py..."
    if ! sed -i "s;<KUBEPANEL_DOMAIN>;$KUBEPANEL_DOMAIN;g" "$DIR/kubepanel/settings.py"; then
        log_error "Failed to configure KUBEPANEL_DOMAIN in settings.py"
        exit 1
    fi
    if ! sed -i "s;<MARIADB_ROOT_PASSWORD>;$MARIADB_ROOT_PASSWORD;g" "$DIR/kubepanel/settings.py"; then
        log_error "Failed to configure MARIADB_ROOT_PASSWORD in settings.py"
        exit 1
    fi
    log "Settings configured successfully."

    # Step 5: Run migrations
    log "Step 5: Running makemigrations..."
    if ! /usr/local/bin/python "$DIR/manage.py" makemigrations dashboard; then
        log_error "Failed to run makemigrations"
        exit 1
    fi

    log "Step 5b: Running migrate..."
    if ! /usr/local/bin/python "$DIR/manage.py" migrate; then
        log_error "Failed to run migrate"
        exit 1
    fi
    log "Migrations completed successfully."

    # Step 6: Create superuser
    log "Step 6: Creating superuser..."
    if ! /usr/local/bin/python "$DIR/manage.py" createsuperuser --noinput; then
        log_error "Failed to create superuser"
        exit 1
    fi
    log "Superuser created successfully."

    # Step 7: Get node IPs
    log "Step 7: Gathering NODE_*_IP values from ConfigMap 'node-public-ips'..."
    mapfile -t node_ips < <(
        kubectl get configmap node-public-ips -n kubepanel \
            -o go-template='{{range $k, $v := .data}}{{println $v}}{{end}}' \
            | head -n3
    )

    if [ "${#node_ips[@]}" -lt 3 ]; then
        log "Warning: expected 3 IPs in ConfigMap; found ${#node_ips[@]}"
    fi

    export NODE_1_IP="${node_ips[0]:-}"
    export NODE_2_IP="${node_ips[1]:-}"
    export NODE_3_IP="${node_ips[2]:-}"

    log "  NODE_1_IP=$NODE_1_IP"
    log "  NODE_2_IP=$NODE_2_IP"
    log "  NODE_3_IP=$NODE_3_IP"

    # Step 8: Load fixtures
    log "Step 8: Loading workloads fixture..."
    if ! /usr/local/bin/python "$DIR/manage.py" loaddata "$DIR/dashboard/fixtures/workloads.json"; then
        log_error "Failed to load workloads fixture"
        exit 1
    fi
    log "Fixtures loaded successfully."

    # Step 9: Clean up DKIM directory if exists
    if [ -d "$DKIMDIR" ]; then
        log "Step 9: Cleaning up existing DKIM directory..."
        rm -f "$DKIMDIR/$KUBEPANEL_DOMAIN.key" 2>/dev/null || true
        rmdir "$DKIMDIR" 2>/dev/null || true
    fi

    # Step 10: Run firstrun
    log "Step 10: Running firstrun command..."
    if ! /usr/local/bin/python "$DIR/manage.py" firstrun; then
        log_error "Failed to run firstrun"
        exit 1
    fi
    log "Firstrun completed successfully."

    # Mark initialization as complete
    log "All initialization steps completed successfully!"
    date > "$INIT_MARKER"

else
    # Create log file if it doesn't exist
    touch "$INIT_LOG" 2>/dev/null || true

    log "Directory is not empty (contains: $entries)"
    log "Checking if this is a partial initialization that needs to continue..."

    # Directory has content but no marker - might be a failed previous run
    # Try to complete any missing steps
    cd "$DIR"

    log "Running migrate to ensure database is up to date..."
    if ! /usr/local/bin/python "$DIR/manage.py" migrate --noinput 2>&1 | tee -a "$INIT_LOG"; then
        log_error "Migration failed"
        exit 1
    fi

    log "Migration completed. Marking initialization as complete."
    date > "$INIT_MARKER"
fi

log "Initialization finished successfully."
exit 0
