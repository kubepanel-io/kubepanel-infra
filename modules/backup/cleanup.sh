#!/bin/bash
# KubePanel Backup Cleanup Script
#
# This script is executed by a K8s CronJob to clean up expired backups.
# It deletes Backup CRs that have passed their retention expiration date,
# which triggers the operator to clean up associated resources.
#
# Required environment variables:
#   None - uses in-cluster service account

set -euo pipefail

echo "=== KubePanel Backup Cleanup Script ==="
echo "Timestamp: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo ""

# Get current timestamp for comparison
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "Current time: ${NOW}"
echo ""

# Get all namespaces that start with dom- (domain namespaces)
echo "Scanning domain namespaces for expired backups..."
NAMESPACES=$(kubectl get namespaces -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | grep '^dom-' || true)

if [ -z "${NAMESPACES}" ]; then
    echo "No domain namespaces found."
    exit 0
fi

TOTAL_DELETED=0
TOTAL_ERRORS=0

for NS in ${NAMESPACES}; do
    echo ""
    echo "--- Checking namespace: ${NS} ---"

    # Get all completed backups in this namespace
    BACKUPS=$(kubectl get backups -n "${NS}" -o json 2>/dev/null || echo '{"items":[]}')

    # Process each backup
    echo "${BACKUPS}" | jq -r '.items[] | select(.status.phase == "Completed") | "\(.metadata.name)|\(.status.retentionExpiresAt // "")"' | while IFS='|' read -r BACKUP_NAME EXPIRES_AT; do
        if [ -z "${EXPIRES_AT}" ]; then
            echo "  Backup ${BACKUP_NAME}: No expiration set, skipping"
            continue
        fi

        # Compare dates (works because ISO 8601 format sorts lexicographically)
        if [[ "${EXPIRES_AT}" < "${NOW}" ]]; then
            echo "  Backup ${BACKUP_NAME}: EXPIRED (${EXPIRES_AT}), deleting..."

            if kubectl delete backup "${BACKUP_NAME}" -n "${NS}" 2>/dev/null; then
                echo "    Deleted successfully"
                TOTAL_DELETED=$((TOTAL_DELETED + 1))
            else
                echo "    ERROR: Failed to delete"
                TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
            fi
        else
            echo "  Backup ${BACKUP_NAME}: Valid until ${EXPIRES_AT}"
        fi
    done

    # Also clean up Failed backups older than 1 day
    echo "${BACKUPS}" | jq -r '.items[] | select(.status.phase == "Failed") | "\(.metadata.name)|\(.status.completedAt // .metadata.creationTimestamp)"' | while IFS='|' read -r BACKUP_NAME FAILED_AT; do
        if [ -z "${FAILED_AT}" ]; then
            continue
        fi

        # Calculate 1 day ago
        ONE_DAY_AGO=$(date -u -d "-1 day" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -v-1d +"%Y-%m-%dT%H:%M:%SZ")

        if [[ "${FAILED_AT}" < "${ONE_DAY_AGO}" ]]; then
            echo "  Failed backup ${BACKUP_NAME}: older than 1 day, deleting..."

            if kubectl delete backup "${BACKUP_NAME}" -n "${NS}" 2>/dev/null; then
                echo "    Deleted successfully"
                TOTAL_DELETED=$((TOTAL_DELETED + 1))
            else
                echo "    ERROR: Failed to delete"
                TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
            fi
        fi
    done
done

echo ""
echo "=== Cleanup Summary ==="
echo "Total backups deleted: ${TOTAL_DELETED}"
echo "Errors: ${TOTAL_ERRORS}"
echo "Cleanup completed at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
