#!/bin/bash
# KubePanel Nightly Backup Script
#
# This script is executed by a K8s CronJob to trigger nightly backups.
# It creates a Backup CR for each domain, one at a time with a delay.
#
# Required environment variables:
#   BACKUP_DELAY - Delay between backups in seconds (default: 60)

set -euo pipefail

BACKUP_DELAY="${BACKUP_DELAY:-60}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

echo "=== KubePanel Nightly Backup Script ==="
echo "Timestamp: ${TIMESTAMP}"
echo "Delay between backups: ${BACKUP_DELAY}s"
echo ""

# Get all Domain CRs across all namespaces
echo "Fetching all domains..."
DOMAINS=$(kubectl get domains.kubepanel.io -A -o json)

DOMAIN_COUNT=$(echo "${DOMAINS}" | jq '.items | length')
echo "Found ${DOMAIN_COUNT} domains"
echo ""

if [ "${DOMAIN_COUNT}" -eq 0 ]; then
    echo "No domains found. Exiting."
    exit 0
fi

BACKUP_COUNT=0
ERROR_COUNT=0

# Process each domain using process substitution (to avoid subshell variable scope issues)
while IFS='|' read -r CR_NAME DOMAIN_NAME; do
    echo "--- Processing domain: ${DOMAIN_NAME} ---"
    echo "  CR Name: ${CR_NAME}"

    # Derive namespace from domain name (same logic as Django: dom-{domain.replace('.', '-')})
    NAMESPACE="dom-$(echo "${DOMAIN_NAME}" | tr '.' '-' | tr -cd 'a-z0-9-')"
    echo "  Namespace: ${NAMESPACE}"

    # Generate backup name
    BACKUP_NAME="${CR_NAME}-${TIMESTAMP}"

    # Create Backup CR in the domain's namespace
    if cat <<EOF | kubectl apply -f -
apiVersion: kubepanel.io/v1alpha1
kind: Backup
metadata:
  name: ${BACKUP_NAME}
  namespace: ${NAMESPACE}
  labels:
    kubepanel.io/domain: "${CR_NAME}"
    kubepanel.io/backup-type: scheduled
spec:
  domainName: "${DOMAIN_NAME}"
  type: scheduled
EOF
    then
        echo "  Created Backup CR: ${BACKUP_NAME}"
        BACKUP_COUNT=$((BACKUP_COUNT + 1))
    else
        echo "  ERROR: Failed to create Backup CR"
        ERROR_COUNT=$((ERROR_COUNT + 1))
    fi

    # Delay before next backup
    echo "  Waiting ${BACKUP_DELAY}s before next backup..."
    sleep "${BACKUP_DELAY}"

    echo ""
done < <(echo "${DOMAINS}" | jq -r '.items[] | "\(.metadata.name)|\(.spec.domainName)"')

echo "=== Nightly Backup Summary ==="
echo "Total domains: ${DOMAIN_COUNT}"
echo "Backups created: ${BACKUP_COUNT}"
echo "Errors: ${ERROR_COUNT}"
echo "Completed at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
