#!/bin/bash
# KubePanel Domain Restore Script
#
# This script is executed by a K8s Job to restore a domain from backup.
# It restores the VolumeSnapshot (by recreating PVC) and the database dump.
#
# Required environment variables:
#   RESTORE_NAME        - Name of the Restore CR
#   NAMESPACE           - Domain namespace (e.g., dom-example-com)
#   DOMAIN_NAME         - Domain name (e.g., example.com)
#   BACKUP_NAME         - Name of the Backup CR we're restoring from
#   VOLUME_SNAPSHOT_NAME - Name of the VolumeSnapshot to restore
#   DATABASE_BACKUP_PATH - Path to the database dump on backup PVC
#   DB_NAME             - Database name to restore
#   DB_HOST             - Database host (default: mariadb.kubepanel.svc.cluster.local)
#   MARIADB_ROOT_PASSWORD - MariaDB root password
#   STORAGE_SIZE        - Storage size for PVC (e.g., 5Gi)

set -euo pipefail

# Configuration with defaults
DB_HOST="${DB_HOST:-mariadb.kubepanel.svc.cluster.local}"
STORAGE_SIZE="${STORAGE_SIZE:-5Gi}"

# Convert domain name to CR name format (dots to dashes)
DOMAIN_CR_NAME="${DOMAIN_NAME//\./-}"

echo "=== KubePanel Restore Script ==="
echo "Restore Name: ${RESTORE_NAME}"
echo "Namespace: ${NAMESPACE}"
echo "Domain: ${DOMAIN_NAME}"
echo "Backup Name: ${BACKUP_NAME}"
echo "VolumeSnapshot: ${VOLUME_SNAPSHOT_NAME}"
echo "Database Backup: ${DATABASE_BACKUP_PATH}"
echo "Database: ${DB_NAME}"
echo "Storage Size: ${STORAGE_SIZE}"
echo ""

# Function to update Restore CR status
update_restore_status() {
    local phase="$1"
    local message="$2"

    local now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    local patch="{\"status\":{\"phase\":\"${phase}\",\"message\":\"${message}\""

    if [ "${phase}" == "Running" ]; then
        patch="${patch},\"startedAt\":\"${now}\""
    fi

    if [ "${phase}" == "Completed" ] || [ "${phase}" == "Failed" ]; then
        patch="${patch},\"completedAt\":\"${now}\""
    fi

    patch="${patch}}}"

    kubectl patch restore "${RESTORE_NAME}" \
        --namespace "${NAMESPACE}" \
        --type merge \
        --subresource status \
        -p "${patch}" || echo "Warning: Failed to update restore status"
}

# Function to handle errors
handle_error() {
    local error_message="$1"
    echo "ERROR: ${error_message}"
    update_restore_status "Failed" "${error_message}"

    # Try to scale deployment back up on failure
    echo "Attempting to scale deployment back up..."
    kubectl scale deployment web \
        --namespace "${NAMESPACE}" \
        --replicas=1 2>/dev/null || true

    exit 1
}

# Trap errors
trap 'handle_error "Restore script failed unexpectedly"' ERR

# Update status to Running
echo "Updating Restore CR status to Running..."
update_restore_status "Running" "Restore in progress"

# Step 1: Scale down the deployment
echo ""
echo "=== Step 1: Scaling down deployment ==="
kubectl scale deployment web \
    --namespace "${NAMESPACE}" \
    --replicas=0

echo "Waiting for pods to terminate..."
kubectl wait --for=delete pod \
    --selector="kubepanel.io/domain=${DOMAIN_CR_NAME}" \
    --namespace "${NAMESPACE}" \
    --timeout=120s 2>/dev/null || true

# Step 2: Delete the existing data PVC
echo ""
echo "=== Step 2: Deleting existing data PVC ==="
kubectl delete pvc data \
    --namespace "${NAMESPACE}" \
    --wait=true

# Step 3: Create new PVC from VolumeSnapshot
echo ""
echo "=== Step 3: Creating new PVC from VolumeSnapshot ==="
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data
  namespace: ${NAMESPACE}
spec:
  storageClassName: linstor-sc
  resources:
    requests:
      storage: ${STORAGE_SIZE}
  dataSource:
    apiGroup: snapshot.storage.k8s.io
    kind: VolumeSnapshot
    name: ${VOLUME_SNAPSHOT_NAME}
  accessModes:
    - ReadWriteOnce
EOF

echo "PVC created from VolumeSnapshot (will bind when pod starts)"

# Step 4: Restore database
echo ""
echo "=== Step 4: Restoring database ==="

if [ ! -f "${DATABASE_BACKUP_PATH}" ]; then
    handle_error "Database backup file not found: ${DATABASE_BACKUP_PATH}"
fi

echo "Restoring database from: ${DATABASE_BACKUP_PATH}"

# Check if file is compressed (ends with .zst)
if [[ "${DATABASE_BACKUP_PATH}" == *.zst ]]; then
    zstd -d -c "${DATABASE_BACKUP_PATH}" | mysql \
        --host="${DB_HOST}" \
        --user=root \
        --password="${MARIADB_ROOT_PASSWORD}" \
        "${DB_NAME}"
else
    mysql \
        --host="${DB_HOST}" \
        --user=root \
        --password="${MARIADB_ROOT_PASSWORD}" \
        "${DB_NAME}" < "${DATABASE_BACKUP_PATH}"
fi

echo "Database restored successfully"

# Step 5: Scale deployment back up
echo ""
echo "=== Step 5: Scaling deployment back up ==="
kubectl scale deployment web \
    --namespace "${NAMESPACE}" \
    --replicas=1

# Step 6: Update status to Completed
echo ""
echo "=== Step 6: Updating Restore CR status ==="
update_restore_status "Completed" "Restore completed successfully"

echo ""
echo "=== Restore completed successfully ==="
echo "Domain: ${DOMAIN_NAME}"
echo "Restored from backup: ${BACKUP_NAME}"
echo "VolumeSnapshot: ${VOLUME_SNAPSHOT_NAME}"
