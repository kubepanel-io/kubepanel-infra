#!/bin/bash
# KubePanel Domain Backup Script
#
# This script is executed by a K8s Job to perform a backup for a domain.
# It creates a VolumeSnapshot (filesystem) and runs mariabackup (database).
#
# Required environment variables:
#   BACKUP_NAME         - Name of the Backup CR (e.g., example-com-20240115-020000)
#   NAMESPACE           - Domain namespace (e.g., dom-example-com)
#   DOMAIN_NAME         - Domain name (e.g., example.com)
#   DB_NAME             - Database name to backup
#   DB_HOST             - Database host (default: mariadb.kubepanel.svc.cluster.local)
#   MARIADB_ROOT_PASSWORD - MariaDB root password
#   BACKUP_PVC_PATH     - Mount path for backup PVC (default: /backup)

set -euo pipefail

# Configuration with defaults
DB_HOST="${DB_HOST:-mariadb.kubepanel.svc.cluster.local}"
BACKUP_PVC_PATH="${BACKUP_PVC_PATH:-/backup}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

# Convert domain name to CR name format (dots to dashes)
DOMAIN_CR_NAME="${DOMAIN_NAME//\./-}"
SNAPSHOT_NAME="${NAMESPACE}-snapshot-${TIMESTAMP}"
BACKUP_DIR="${BACKUP_PVC_PATH}/${TIMESTAMP}"
SQL_DUMP_FILE="${BACKUP_DIR}/database.sql.zst"
LOG_FILE="${BACKUP_DIR}/backup.log"

# Calculate retention expiration (7 days from now)
RETENTION_EXPIRES_AT=$(date -u -d "+7 days" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -v+7d +"%Y-%m-%dT%H:%M:%SZ")

echo "=== KubePanel Backup Script ==="
echo "Backup Name: ${BACKUP_NAME}"
echo "Namespace: ${NAMESPACE}"
echo "Domain: ${DOMAIN_NAME}"
echo "Database: ${DB_NAME}"
echo "Timestamp: ${TIMESTAMP}"
echo "Snapshot Name: ${SNAPSHOT_NAME}"
echo "Backup Directory: ${BACKUP_DIR}"
echo ""

# Function to update Backup CR status
update_backup_status() {
    local phase="$1"
    local message="$2"
    local extra_fields="${3:-}"

    local now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    local patch="{\"status\":{\"phase\":\"${phase}\",\"message\":\"${message}\""

    if [ "${phase}" == "Running" ]; then
        patch="${patch},\"startedAt\":\"${now}\""
    fi

    if [ "${phase}" == "Completed" ] || [ "${phase}" == "Failed" ]; then
        patch="${patch},\"completedAt\":\"${now}\""
    fi

    if [ -n "${extra_fields}" ]; then
        patch="${patch},${extra_fields}"
    fi

    patch="${patch}}}"

    kubectl patch backup "${BACKUP_NAME}" \
        --namespace "${NAMESPACE}" \
        --type merge \
        --subresource status \
        -p "${patch}" || echo "Warning: Failed to update backup status"
}

# Function to handle errors
handle_error() {
    local error_message="$1"
    echo "ERROR: ${error_message}" | tee -a "${LOG_FILE}" 2>/dev/null || echo "ERROR: ${error_message}"
    update_backup_status "Failed" "${error_message}"
    exit 1
}

# Function to wait for VolumeSnapshot to be ready
wait_for_snapshot() {
    local snapshot_name="$1"
    local namespace="$2"
    local timeout_seconds="${3:-300}"  # Default 5 minutes
    local poll_interval=5
    local elapsed=0

    echo "Waiting for VolumeSnapshot ${snapshot_name} to be ready (timeout: ${timeout_seconds}s)..."

    while [ $elapsed -lt $timeout_seconds ]; do
        # Get snapshot status
        local status_json=$(kubectl get volumesnapshot "${snapshot_name}" \
            --namespace "${namespace}" \
            -o jsonpath='{.status}' 2>/dev/null || echo "")

        if [ -z "${status_json}" ]; then
            echo "  Waiting for status to appear..."
            sleep $poll_interval
            elapsed=$((elapsed + poll_interval))
            continue
        fi

        # Check if ready
        local ready_to_use=$(kubectl get volumesnapshot "${snapshot_name}" \
            --namespace "${namespace}" \
            -o jsonpath='{.status.readyToUse}' 2>/dev/null || echo "")

        if [ "${ready_to_use}" == "true" ]; then
            echo "VolumeSnapshot is ready!"
            return 0
        fi

        # Check for errors
        local error_msg=$(kubectl get volumesnapshot "${snapshot_name}" \
            --namespace "${namespace}" \
            -o jsonpath='{.status.error.message}' 2>/dev/null || echo "")

        if [ -n "${error_msg}" ]; then
            echo "VolumeSnapshot failed with error: ${error_msg}"
            return 1
        fi

        echo "  Status: readyToUse=${ready_to_use:-pending}, elapsed=${elapsed}s"
        sleep $poll_interval
        elapsed=$((elapsed + poll_interval))
    done

    echo "Timeout waiting for VolumeSnapshot to be ready"
    return 1
}

# Trap errors
trap 'handle_error "Backup script failed unexpectedly"' ERR

# Create backup directory
echo "Creating backup directory: ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"

# Start logging
exec > >(tee -a "${LOG_FILE}") 2>&1

# Update status to Running
echo "Updating Backup CR status to Running..."
update_backup_status "Running" "Backup in progress"

# Step 1: Create VolumeSnapshot for filesystem backup
echo ""
echo "=== Step 1: Creating VolumeSnapshot ==="
echo "Creating VolumeSnapshot: ${SNAPSHOT_NAME}"

cat <<EOF | kubectl apply -f -
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: ${SNAPSHOT_NAME}
  namespace: ${NAMESPACE}
  labels:
    kubepanel.io/domain: "${DOMAIN_CR_NAME}"
    kubepanel.io/backup: "${BACKUP_NAME}"
spec:
  volumeSnapshotClassName: piraeus-snapshots
  source:
    persistentVolumeClaimName: data
EOF

echo "VolumeSnapshot CR created, waiting for snapshot to complete..."

# Wait for VolumeSnapshot to be ready
if ! wait_for_snapshot "${SNAPSHOT_NAME}" "${NAMESPACE}" 300; then
    # Get the error message for the status update
    snapshot_error=$(kubectl get volumesnapshot "${SNAPSHOT_NAME}" \
        --namespace "${NAMESPACE}" \
        -o jsonpath='{.status.error.message}' 2>/dev/null || echo "Unknown error")
    handle_error "VolumeSnapshot failed: ${snapshot_error}"
fi

echo "VolumeSnapshot completed successfully"

# Step 2: Run mysqldump for database backup
echo ""
echo "=== Step 2: Running mysqldump ==="
echo "Backing up database: ${DB_NAME}"

# Run mysqldump and compress with zstd
echo "Running mysqldump..."
mysqldump \
    --host="${DB_HOST}" \
    --user=root \
    --password="${MARIADB_ROOT_PASSWORD}" \
    --single-transaction \
    --routines \
    --triggers \
    --events \
    "${DB_NAME}" | zstd -3 -o "${SQL_DUMP_FILE}"

echo "Database backup completed: ${SQL_DUMP_FILE}"

# Step 3: Calculate backup size
echo ""
echo "=== Step 3: Calculating backup size ==="
BACKUP_SIZE=$(stat -c%s "${SQL_DUMP_FILE}" 2>/dev/null || stat -f%z "${SQL_DUMP_FILE}")
echo "Backup size: ${BACKUP_SIZE} bytes"

# Step 4: Update Backup CR status to Completed
echo ""
echo "=== Step 4: Updating Backup CR status ==="

extra_fields="\"volumeSnapshotName\":\"${SNAPSHOT_NAME}\""
extra_fields="${extra_fields},\"databaseBackupPath\":\"${SQL_DUMP_FILE}\""
extra_fields="${extra_fields},\"sizeBytes\":${BACKUP_SIZE}"
extra_fields="${extra_fields},\"retentionExpiresAt\":\"${RETENTION_EXPIRES_AT}\""

update_backup_status "Completed" "Backup completed successfully" "${extra_fields}"

echo ""
echo "=== Backup completed successfully ==="
echo "VolumeSnapshot: ${SNAPSHOT_NAME}"
echo "Database backup: ${SQL_DUMP_FILE}"
echo "Size: ${BACKUP_SIZE} bytes"
echo "Retention expires: ${RETENTION_EXPIRES_AT}"
