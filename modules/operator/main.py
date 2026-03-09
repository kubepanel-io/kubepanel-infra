"""
KubePanel Operator

A Kopf-based operator that watches Domain CRs and reconciles
the necessary Kubernetes resources.
"""

import kopf
import kubernetes
from kubernetes import client
from kubernetes.client.rest import ApiException
import logging
import sys
import re
import base64
from datetime import datetime, timezone

import pymysql
from pymysql import MySQLError

from cloudflare import Cloudflare
from cloudflare._exceptions import APIError as CloudflareAPIError

from resources import (
    build_namespace,
    build_pvc,
    get_pvc_status,
    build_sftp_secret,
    build_database_secret,
    build_dkim_secret,
    build_nginx_configmap,
    build_app_configmap,
    build_php_configmap,  # Deprecated alias, kept for compatibility
    build_deployment,
    get_deployment_status,
    build_service,
    build_sftp_service,
    build_ingress,
    get_ingress_status,
    build_backup_pvc,
    build_backup_service_account,
    build_backup_role_binding,
    build_backup_credentials_secret,
    SFTP_IMAGE,
    SSHGIT_IMAGE,
)

# Configure logging for stdout (container-friendly)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
# Ensure logs are flushed immediately (no buffering)
for handler in logging.root.handlers:
    handler.flush = sys.stdout.flush

logger = logging.getLogger('kubepanel-operator')
logger.setLevel(logging.DEBUG)

# Also configure kopf and kubernetes client loggers
logging.getLogger('kopf').setLevel(logging.INFO)
logging.getLogger('kubernetes').setLevel(logging.WARNING)

# Constants
DOMAIN_GROUP = 'kubepanel.io'
DOMAIN_VERSION = 'v1alpha1'
DOMAIN_PLURAL = 'domains'

# Backup CRD constants
BACKUP_GROUP = 'kubepanel.io'
BACKUP_VERSION = 'v1alpha1'
BACKUP_PLURAL = 'backups'

# Backup configuration
BACKUP_IMAGE = 'docker.io/kubepanel/backup:v1.0'
RESTORE_IMAGE = 'docker.io/kubepanel/restore:v1.0'
BACKUP_SERVICE_ACCOUNT = 'kubepanel-backup'

# MariaDB configuration
MARIADB_HOST = 'mariadb.kubepanel.svc.cluster.local'
MARIADB_PORT = 3306
MARIADB_SECRET_NAME = 'mariadb-auth'
MARIADB_SECRET_NAMESPACE = 'kubepanel'

# DKIM Central Configuration
DKIM_NAMESPACE = 'kubepanel'
DKIM_KEYS_SECRET = 'dkim-keys'
DKIM_KEYTABLE_CM = 'dkim-keytable'
DKIM_SIGNINGTABLE_CM = 'dkim-signingtable'

# License CRD constants
LICENSE_GROUP = 'kubepanel.io'
LICENSE_VERSION = 'v1alpha1'
LICENSE_PLURAL = 'licenses'
LICENSE_CR_NAME = 'kubepanel-license'

# License phone-home configuration
PHONE_HOME_URL = 'https://kubepanel.io/v1/heartbeat'
GRACE_PERIOD_DAYS = 14
COMMUNITY_MAX_DOMAINS = 5

# KubePanel version (for phone-home)
KUBEPANEL_VERSION = '1.0.0'


# =============================================================================
# Helper Functions
# =============================================================================

from collections import namedtuple

# Named tuple for API clients - adding new clients won't break existing code
K8sClients = namedtuple('K8sClients', ['core', 'custom', 'apps', 'networking', 'rbac'])


def get_api_clients() -> K8sClients:
    """Get Kubernetes API clients as a named tuple."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()

    return K8sClients(
        core=client.CoreV1Api(),
        custom=client.CustomObjectsApi(),
        apps=client.AppsV1Api(),
        networking=client.NetworkingV1Api(),
        rbac=client.RbacAuthorizationV1Api(),
    )


def parse_cpu_millicores(value: str) -> int:
    """
    Parse a Kubernetes CPU value and return millicores.

    Examples:
        "2" -> 2000 (2 cores = 2000 millicores)
        "2000m" -> 2000 (2000 millicores)
        "500m" -> 500 (500 millicores)
        "0.5" -> 500 (0.5 cores = 500 millicores)
    """
    if value is None:
        return 0
    value = str(value).strip()
    if value.endswith('m'):
        return int(value[:-1])
    else:
        # Could be integer cores ("2") or decimal ("0.5")
        return int(float(value) * 1000)


def parse_memory_bytes(value: str) -> int:
    """
    Parse a Kubernetes memory value and return bytes.

    Supports:
        - Ki, Mi, Gi, Ti (binary: 1024-based)
        - K, M, G, T (decimal: 1000-based, deprecated but still used)
        - Plain numbers (bytes)

    Examples:
        "2Gi" -> 2147483648 (2 * 1024^3)
        "2048Mi" -> 2147483648 (2048 * 1024^2)
        "512M" -> 512000000 (512 * 1000^2)
    """
    if value is None:
        return 0
    value = str(value).strip()

    # Binary suffixes (1024-based)
    binary_suffixes = {
        'Ki': 1024,
        'Mi': 1024 ** 2,
        'Gi': 1024 ** 3,
        'Ti': 1024 ** 4,
    }

    # Decimal suffixes (1000-based)
    decimal_suffixes = {
        'K': 1000,
        'M': 1000 ** 2,
        'G': 1000 ** 3,
        'T': 1000 ** 4,
    }

    # Check binary suffixes first (they're two characters)
    for suffix, multiplier in binary_suffixes.items():
        if value.endswith(suffix):
            return int(value[:-2]) * multiplier

    # Then check decimal suffixes (one character)
    for suffix, multiplier in decimal_suffixes.items():
        if value.endswith(suffix):
            return int(value[:-1]) * multiplier

    # Plain number (bytes)
    return int(value)


def parse_storage_size_gb(size_str: str) -> int:
    """
    Parse Kubernetes storage size string to GB (integer).

    Used for comparing storage sizes to determine if expansion is needed.

    Examples:
        "5Gi" -> 5
        "10Gi" -> 10
        "1Ti" -> 1024
        "512Mi" -> 0 (less than 1GB)
    """
    if size_str is None:
        return 0
    size_str = str(size_str).strip()

    if size_str.endswith('Ti'):
        return int(size_str[:-2]) * 1024
    elif size_str.endswith('Gi'):
        return int(size_str[:-2])
    elif size_str.endswith('Mi'):
        # Convert Mi to Gi (round down)
        return int(size_str[:-2]) // 1024
    elif size_str.endswith('Ki'):
        # Convert Ki to Gi (round down)
        return int(size_str[:-2]) // (1024 * 1024)
    else:
        # Assume bytes, convert to GB (round down)
        try:
            return int(size_str) // (1024 ** 3)
        except ValueError:
            return 0


def cpu_equal(a: str, b: str) -> bool:
    """
    Compare two CPU values semantically.
    Returns True if they represent the same amount.

    Examples:
        cpu_equal("2", "2000m") -> True
        cpu_equal("500m", "0.5") -> True
    """
    return parse_cpu_millicores(a) == parse_cpu_millicores(b)


def memory_equal(a: str, b: str) -> bool:
    """
    Compare two memory values semantically.
    Returns True if they represent the same amount.

    Examples:
        memory_equal("2Gi", "2048Mi") -> True
        memory_equal("1Gi", "1073741824") -> True
    """
    return parse_memory_bytes(a) == parse_memory_bytes(b)


def sanitize_name(domain_name: str) -> str:
    """
    Convert domain name to a valid Kubernetes namespace name.
    e.g., 'example.com' -> 'dom-example-com'
    """
    sanitized = re.sub(r'[^a-z0-9-]', '-', domain_name.lower())
    sanitized = re.sub(r'-+', '-', sanitized)  # collapse multiple dashes
    sanitized = sanitized.strip('-')
    return f"dom-{sanitized}"


def set_condition(
    conditions: list,
    condition_type: str,
    status: str,
    reason: str,
    message: str
) -> list:
    """Set or update a condition in the conditions list."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    # Find existing condition
    for i, cond in enumerate(conditions):
        if cond['type'] == condition_type:
            # Only update if status changed
            if cond['status'] != status:
                conditions[i] = {
                    'type': condition_type,
                    'status': status,
                    'reason': reason,
                    'message': message,
                    'lastTransitionTime': now
                }
            else:
                # Update reason/message but keep lastTransitionTime
                conditions[i]['reason'] = reason
                conditions[i]['message'] = message
            return conditions
    
    # Add new condition
    conditions.append({
        'type': condition_type,
        'status': status,
        'reason': reason,
        'message': message,
        'lastTransitionTime': now
    })
    return conditions


def determine_overall_phase(conditions: list, suspended: bool = False) -> tuple[str, str]:
    """
    Determine overall phase based on conditions.
    
    Returns:
        Tuple of (phase, message)
    """
    if suspended:
        return ('Suspended', 'Domain is suspended')
    
    if not conditions:
        return ('Pending', 'Waiting for reconciliation')
    
    # Check for any failed conditions
    failed = [c for c in conditions if c['status'] == 'False']
    if failed:
        return ('Degraded', f"{failed[0]['type']}: {failed[0]['message']}")
    
    # Check for any unknown conditions
    unknown = [c for c in conditions if c['status'] == 'Unknown']
    if unknown:
        return ('Provisioning', f"Waiting for {unknown[0]['type']}")
    
    # All conditions are True
    return ('Ready', 'All resources are healthy')


# =============================================================================
# Resource Reconciliation Functions
# =============================================================================

def ensure_namespace(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    owner: str,
    conditions: list
) -> list:
    """
    Ensure namespace exists, create if missing.
    
    Returns:
        Updated conditions list
    """
    try:
        core_api.read_namespace(name=namespace_name)
        logger.debug(f"Namespace '{namespace_name}' exists")
        conditions = set_condition(
            conditions, 'NamespaceReady', 'True', 'Exists',
            f"Namespace {namespace_name} exists"
        )
    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating namespace '{namespace_name}'...")
            ns = build_namespace(namespace_name, domain_cr_name, domain_name, owner)
            try:
                core_api.create_namespace(body=ns)
                logger.info(f"Created namespace '{namespace_name}'")
                conditions = set_condition(
                    conditions, 'NamespaceReady', 'True', 'Created',
                    f"Namespace {namespace_name} created"
                )
            except ApiException as create_err:
                if create_err.status == 409:
                    # Race condition, namespace was created by another process
                    conditions = set_condition(
                        conditions, 'NamespaceReady', 'True', 'Exists',
                        f"Namespace {namespace_name} exists"
                    )
                else:
                    logger.error(f"Failed to create namespace: {create_err}")
                    conditions = set_condition(
                        conditions, 'NamespaceReady', 'False', 'CreateFailed',
                        f"Failed to create namespace: {create_err.reason}"
                    )
                    raise
        else:
            logger.error(f"Failed to read namespace: {e}")
            raise
    
    return conditions


def ensure_pvc(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    storage_size: str,
    conditions: list
) -> list:
    """
    Ensure PVC exists, create if missing.
    
    Returns:
        Updated conditions list
    """
    pvc_name = 'data'
    
    try:
        pvc = core_api.read_namespaced_persistent_volume_claim(
            name=pvc_name,
            namespace=namespace_name
        )
        logger.debug(f"PVC '{pvc_name}' exists in '{namespace_name}'")
        
        # Check PVC status
        status, reason, message = get_pvc_status(pvc)
        conditions = set_condition(conditions, 'PVCReady', status, reason, message)
        
        # Check if resize is needed (PVC expansion)
        current_size = pvc.spec.resources.requests.get('storage', '0')
        if current_size != storage_size:
            # Parse sizes for comparison
            current_gb = parse_storage_size_gb(current_size)
            desired_gb = parse_storage_size_gb(storage_size)

            if desired_gb < current_gb:
                # Safeguard: PVC shrinking is not supported
                logger.warning(
                    f"PVC shrink not allowed: current={current_size}, desired={storage_size}. "
                    "Shrinking a PVC is not supported and would cause data loss."
                )
            elif desired_gb > current_gb:
                # Expand the PVC
                logger.info(f"Expanding PVC from {current_size} to {storage_size}")
                try:
                    patch_body = {
                        'spec': {
                            'resources': {
                                'requests': {
                                    'storage': storage_size
                                }
                            }
                        }
                    }
                    core_api.patch_namespaced_persistent_volume_claim(
                        name=pvc_name,
                        namespace=namespace_name,
                        body=patch_body
                    )
                    logger.info(f"PVC expansion initiated: {current_size} -> {storage_size}")
                    conditions = set_condition(
                        conditions, 'PVCReady', 'Unknown', 'Expanding',
                        f"PVC expansion in progress: {current_size} -> {storage_size}"
                    )
                except ApiException as expand_err:
                    logger.error(f"Failed to expand PVC: {expand_err}")
                    conditions = set_condition(
                        conditions, 'PVCReady', 'False', 'ExpansionFailed',
                        f"PVC expansion failed: {expand_err.reason}"
                    )
            # else: sizes are equal when parsed to GB, no action needed

    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating PVC '{pvc_name}' in '{namespace_name}'...")
            pvc = build_pvc(
                namespace_name=namespace_name,
                domain_cr_name=domain_cr_name,
                storage_size=storage_size
            )
            try:
                core_api.create_namespaced_persistent_volume_claim(
                    namespace=namespace_name,
                    body=pvc
                )
                logger.info(f"Created PVC '{pvc_name}' with size {storage_size}")
                conditions = set_condition(
                    conditions, 'PVCReady', 'Unknown', 'Created',
                    f"PVC created, waiting for binding"
                )
            except ApiException as create_err:
                if create_err.status == 409:
                    # Race condition
                    conditions = set_condition(
                        conditions, 'PVCReady', 'Unknown', 'Exists',
                        f"PVC exists, checking status"
                    )
                else:
                    logger.error(f"Failed to create PVC: {create_err}")
                    conditions = set_condition(
                        conditions, 'PVCReady', 'False', 'CreateFailed',
                        f"Failed to create PVC: {create_err.reason}"
                    )
                    raise
        else:
            logger.error(f"Failed to read PVC: {e}")
            raise
    
    return conditions


def ensure_sftp_secret(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    conditions: list,
    status_patch: dict
) -> list:
    """
    Ensure SFTP secret exists, create if missing.
    
    Returns:
        Updated conditions list
    """
    secret_name = 'sftp-credentials'
    
    try:
        secret = core_api.read_namespaced_secret(
            name=secret_name,
            namespace=namespace_name
        )
        logger.debug(f"SFTP secret exists in '{namespace_name}'")
        
        # Update status with secret reference
        status_patch['sftp'] = {
            'username': namespace_name,  # Use namespace as username
            'passwordSecretRef': {
                'name': secret_name,
                'namespace': namespace_name,
                'key': 'password'
            },
            'publicKeySecretRef': {
                'name': secret_name,
                'namespace': namespace_name,
                'key': 'ssh-publickey'
            }
        }
        
        conditions = set_condition(
            conditions, 'SFTPReady', 'True', 'SecretExists',
            'SFTP credentials secret exists'
        )
        
    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating SFTP secret in '{namespace_name}'...")
            secret, status_info = build_sftp_secret(
                namespace_name=namespace_name,
                domain_cr_name=domain_cr_name
            )
            try:
                core_api.create_namespaced_secret(
                    namespace=namespace_name,
                    body=secret
                )
                logger.info(f"Created SFTP secret in '{namespace_name}'")
                
                # Update status with secret reference
                status_patch['sftp'] = {
                    'username': namespace_name,
                    'passwordSecretRef': {
                        'name': secret_name,
                        'namespace': namespace_name,
                        'key': 'password'
                    },
                    'publicKeySecretRef': {
                        'name': secret_name,
                        'namespace': namespace_name,
                        'key': 'ssh-publickey'
                    }
                }
                
                conditions = set_condition(
                    conditions, 'SFTPReady', 'True', 'SecretCreated',
                    'SFTP credentials secret created'
                )
            except ApiException as create_err:
                if create_err.status == 409:
                    conditions = set_condition(
                        conditions, 'SFTPReady', 'True', 'SecretExists',
                        'SFTP credentials secret exists'
                    )
                else:
                    logger.error(f"Failed to create SFTP secret: {create_err}")
                    conditions = set_condition(
                        conditions, 'SFTPReady', 'False', 'SecretCreateFailed',
                        f"Failed to create SFTP secret: {create_err.reason}"
                    )
                    raise
        else:
            logger.error(f"Failed to read SFTP secret: {e}")
            raise
    
    return conditions


def ensure_database_secret(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    conditions: list,
    status_patch: dict
) -> list:
    """
    Ensure database secret exists, create if missing.
    
    Returns:
        Updated conditions list
    """
    secret_name = 'db-credentials'
    
    try:
        secret = core_api.read_namespaced_secret(
            name=secret_name,
            namespace=namespace_name
        )
        logger.debug(f"Database secret exists in '{namespace_name}'")
        
        # Read values from existing secret for status
        import base64
        db_host = base64.b64decode(secret.data.get('host', '')).decode('utf-8')
        db_port = int(base64.b64decode(secret.data.get('port', 'MzMwNg==')).decode('utf-8'))
        db_name = base64.b64decode(secret.data.get('database', '')).decode('utf-8')
        db_user = base64.b64decode(secret.data.get('username', '')).decode('utf-8')
        
        status_patch['database'] = {
            'host': db_host,
            'port': db_port,
            'name': db_name,
            'username': db_user,
            'passwordSecretRef': {
                'name': secret_name,
                'namespace': namespace_name,
                'key': 'password'
            }
        }
        
        conditions = set_condition(
            conditions, 'DatabaseReady', 'True', 'SecretExists',
            'Database credentials secret exists'
        )
        
    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating database secret in '{namespace_name}'...")
            secret, status_info = build_database_secret(
                namespace_name=namespace_name,
                domain_cr_name=domain_cr_name,
                domain_name=domain_name
            )
            try:
                core_api.create_namespaced_secret(
                    namespace=namespace_name,
                    body=secret
                )
                logger.info(f"Created database secret in '{namespace_name}'")
                
                status_patch['database'] = {
                    'host': status_info['host'],
                    'port': status_info['port'],
                    'name': status_info['database'],
                    'username': status_info['username'],
                    'passwordSecretRef': {
                        'name': secret_name,
                        'namespace': namespace_name,
                        'key': 'password'
                    }
                }
                
                conditions = set_condition(
                    conditions, 'DatabaseReady', 'True', 'SecretCreated',
                    'Database credentials secret created'
                )
            except ApiException as create_err:
                if create_err.status == 409:
                    conditions = set_condition(
                        conditions, 'DatabaseReady', 'True', 'SecretExists',
                        'Database credentials secret exists'
                    )
                else:
                    logger.error(f"Failed to create database secret: {create_err}")
                    conditions = set_condition(
                        conditions, 'DatabaseReady', 'False', 'SecretCreateFailed',
                        f"Failed to create database secret: {create_err.reason}"
                    )
                    raise
        else:
            logger.error(f"Failed to read database secret: {e}")
            raise
    
    return conditions


def ensure_dkim_secret(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    dkim_selector: str,
    dkim_secret_ref: dict,
    conditions: list,
    status_patch: dict
) -> list:
    """
    Ensure DKIM configuration is set up.

    New pattern: Django creates DKIM secret in kubepanel namespace, operator reads it.
    Legacy pattern: Operator creates DKIM secret in domain namespace.

    If dkim_secret_ref is provided (new pattern), read from that secret.
    Otherwise fall back to legacy pattern for backward compatibility.

    Also updates central DKIM configuration (Secret and ConfigMaps in kubepanel namespace).

    Returns:
        Updated conditions list
    """
    private_key = None
    selector = dkim_selector
    public_key = None
    dns_txt = None
    secret_name = None
    secret_namespace = None

    # New pattern: Django-created DKIM secret
    if dkim_secret_ref:
        secret_name = dkim_secret_ref.get('name')
        secret_namespace = dkim_secret_ref.get('namespace', 'kubepanel')

        try:
            secret = core_api.read_namespaced_secret(
                name=secret_name,
                namespace=secret_namespace
            )
            logger.debug(f"DKIM secret '{secret_namespace}/{secret_name}' exists (Django-created)")

            # Read values from Django-created secret
            selector = base64.b64decode(secret.data.get('selector', '')).decode('utf-8') or dkim_selector
            public_key = base64.b64decode(secret.data.get('public-key', '')).decode('utf-8')
            dns_txt = base64.b64decode(secret.data.get('dns-txt-record', '')).decode('utf-8')
            private_key = base64.b64decode(secret.data.get('private-key', '')).decode('utf-8')

            status_patch['email'] = {
                'dkimSelector': selector,
                'dkimPublicKey': public_key,
                'dkimDnsRecord': dns_txt,
                'dkimPrivateKeySecretRef': {
                    'name': secret_name,
                    'namespace': secret_namespace,
                    'key': 'private-key'
                }
            }

            conditions = set_condition(
                conditions, 'DKIMReady', 'True', 'SecretExists',
                f"DKIM secret '{secret_namespace}/{secret_name}' exists"
            )

        except ApiException as e:
            if e.status == 404:
                logger.error(f"DKIM secret '{secret_namespace}/{secret_name}' not found (should be created by Django)")
                conditions = set_condition(
                    conditions, 'DKIMReady', 'False', 'SecretNotFound',
                    f"DKIM secret '{secret_namespace}/{secret_name}' not found"
                )
                return conditions
            else:
                logger.error(f"Failed to read DKIM secret: {e}")
                raise

    else:
        # Legacy pattern: Operator creates/manages DKIM secret in domain namespace
        secret_name = 'dkim-credentials'
        secret_namespace = namespace_name

        try:
            secret = core_api.read_namespaced_secret(
                name=secret_name,
                namespace=namespace_name
            )
            logger.debug(f"DKIM secret exists in '{namespace_name}' (legacy pattern)")

            # Read values from existing secret
            selector = base64.b64decode(secret.data.get('selector', '')).decode('utf-8') or dkim_selector
            public_key = base64.b64decode(secret.data.get('public-key', '')).decode('utf-8')
            dns_txt = base64.b64decode(secret.data.get('dns-txt-record', '')).decode('utf-8')
            private_key = base64.b64decode(secret.data.get('private-key', '')).decode('utf-8')

            status_patch['email'] = {
                'dkimSelector': selector,
                'dkimPublicKey': public_key,
                'dkimDnsRecord': dns_txt,
                'dkimPrivateKeySecretRef': {
                    'name': secret_name,
                    'namespace': namespace_name,
                    'key': 'private-key'
                }
            }

            conditions = set_condition(
                conditions, 'DKIMReady', 'True', 'SecretExists',
                'DKIM credentials secret exists'
            )

        except ApiException as e:
            if e.status == 404:
                logger.info(f"Creating DKIM secret in '{namespace_name}' (legacy pattern)...")
                secret, status_info = build_dkim_secret(
                    namespace_name=namespace_name,
                    domain_cr_name=domain_cr_name,
                    selector=dkim_selector
                )
                # Get private key from the secret data for central config
                private_key = base64.b64decode(secret.data.get('private-key', '')).decode('utf-8')
                selector = status_info['selector']
                public_key = status_info['public_key']
                dns_txt = status_info['dns_txt_record']

                try:
                    core_api.create_namespaced_secret(
                        namespace=namespace_name,
                        body=secret
                    )
                    logger.info(f"Created DKIM secret in '{namespace_name}'")

                    status_patch['email'] = {
                        'dkimSelector': selector,
                        'dkimPublicKey': public_key,
                        'dkimDnsRecord': dns_txt,
                        'dkimPrivateKeySecretRef': {
                            'name': secret_name,
                            'namespace': namespace_name,
                            'key': 'private-key'
                        }
                    }

                    conditions = set_condition(
                        conditions, 'DKIMReady', 'True', 'SecretCreated',
                        'DKIM credentials secret created'
                    )
                except ApiException as create_err:
                    if create_err.status == 409:
                        conditions = set_condition(
                            conditions, 'DKIMReady', 'True', 'SecretExists',
                            'DKIM credentials secret exists'
                        )
                    else:
                        logger.error(f"Failed to create DKIM secret: {create_err}")
                        conditions = set_condition(
                            conditions, 'DKIMReady', 'False', 'SecretCreateFailed',
                            f"Failed to create DKIM secret: {create_err.reason}"
                        )
                        raise
            else:
                logger.error(f"Failed to read DKIM secret: {e}")
                raise

    # Update central DKIM configuration (Secret and ConfigMaps in kubepanel namespace)
    if private_key:
        try:
            secret_updated = update_central_dkim_secret(core_api, domain_name, private_key)
            configmaps_updated = update_central_dkim_configmaps(core_api, domain_name, selector)

            # Only restart OpenDKIM if something actually changed
            if secret_updated or configmaps_updated:
                restart_opendkim_deployment()
            else:
                logger.debug(f"DKIM config for '{domain_name}' already up-to-date, skipping OpenDKIM restart")
        except ApiException as central_err:
            logger.error(f"Failed to update central DKIM config for '{domain_name}': {central_err}")
            # Don't fail the reconciliation, just log the error
            # The domain-specific secret is already created

    return conditions


def ensure_nginx_configmap(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    aliases: list,
    webserver_config: dict,
    conditions: list,
    # New workload parameters
    workload_type: str = 'php',
    workload_port: int = 9001,
    proxy_mode: str = 'fastcgi',
    # ModSecurity/DomainWAF parameter
    modsec_enabled: bool = False,
) -> tuple[list, bool]:
    """
    Ensure nginx ConfigMap exists, create or update if needed.

    Returns:
        Tuple of (conditions, configmap_updated)
    """
    configmap_name = 'nginx-config'
    configmap_updated = False

    # Extract webserver settings
    document_root = webserver_config.get('documentRoot', '/usr/share/nginx/html')
    client_max_body_size = webserver_config.get('clientMaxBodySize', '64m')
    custom_config = webserver_config.get('customConfig', '')
    www_redirect = webserver_config.get('wwwRedirect', 'none')

    # Extract cache settings
    cache_config = webserver_config.get('cache', {})
    cache_enabled = cache_config.get('enabled', False)
    cache_inactive_time = cache_config.get('inactiveTime', '60m')
    cache_valid_time = cache_config.get('validTime', '10m')
    cache_bypass_uris = cache_config.get('bypassUris', [])

    # Build desired ConfigMap with workload-aware proxy configuration
    desired_configmap = build_nginx_configmap(
        namespace_name=namespace_name,
        domain_cr_name=domain_cr_name,
        domain_name=domain_name,
        aliases=aliases,
        document_root=document_root,
        client_max_body_size=client_max_body_size,
        custom_config=custom_config,
        www_redirect=www_redirect,
        proxy_mode=proxy_mode,
        app_port=workload_port,
        workload_type=workload_type,
        modsec_enabled=modsec_enabled,
        cache_enabled=cache_enabled,
        cache_inactive_time=cache_inactive_time,
        cache_valid_time=cache_valid_time,
        cache_bypass_uris=cache_bypass_uris,
    )

    try:
        existing = core_api.read_namespaced_config_map(
            name=configmap_name,
            namespace=namespace_name
        )

        # Check if config changed and update if needed
        if existing.data != desired_configmap.data:
            logger.info(f"Updating nginx ConfigMap in '{namespace_name}' (config changed)")
            # Debug: show what keys differ
            for key in set(existing.data.keys()) | set(desired_configmap.data.keys()):
                existing_val = existing.data.get(key, '')
                desired_val = desired_configmap.data.get(key, '')
                if existing_val != desired_val:
                    logger.debug(f"  Key '{key}' differs: existing={len(existing_val)} chars, desired={len(desired_val)} chars")
            core_api.replace_namespaced_config_map(
                name=configmap_name,
                namespace=namespace_name,
                body=desired_configmap
            )
            configmap_updated = True
            conditions = set_condition(
                conditions, 'ConfigMapReady', 'True', 'Updated',
                'Nginx ConfigMap updated'
            )
        else:
            logger.debug(f"ConfigMap '{configmap_name}' unchanged in '{namespace_name}'")
            conditions = set_condition(
                conditions, 'ConfigMapReady', 'True', 'Exists',
                'Nginx ConfigMap exists'
            )

    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating nginx ConfigMap in '{namespace_name}'...")
            try:
                core_api.create_namespaced_config_map(
                    namespace=namespace_name,
                    body=desired_configmap
                )
                logger.info(f"Created nginx ConfigMap in '{namespace_name}'")
                configmap_updated = True
                conditions = set_condition(
                    conditions, 'ConfigMapReady', 'True', 'Created',
                    'Nginx ConfigMap created'
                )
            except ApiException as create_err:
                if create_err.status == 409:
                    conditions = set_condition(
                        conditions, 'ConfigMapReady', 'True', 'Exists',
                        'Nginx ConfigMap exists'
                    )
                else:
                    logger.error(f"Failed to create ConfigMap: {create_err}")
                    conditions = set_condition(
                        conditions, 'ConfigMapReady', 'False', 'CreateFailed',
                        f"Failed to create ConfigMap: {create_err.reason}"
                    )
                    raise
        else:
            logger.error(f"Failed to read ConfigMap: {e}")
            raise

    return conditions, configmap_updated


def ensure_app_configmap(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    workload_type: str,
    workload_settings: dict,
    custom_config: str = '',
) -> bool:
    """
    Ensure app ConfigMap exists, create or update if needed.

    For PHP: Creates php.ini configuration
    For other types: Creates generic app configuration

    Note: Does not set a CR condition (CRD doesn't have AppConfigMapReady).
    The nginx ConfigMapReady condition covers overall config readiness.

    Returns:
        True if ConfigMap was created or updated, False if unchanged
    """
    configmap_name = 'app-config'
    configmap_updated = False

    # Extract settings based on workload type
    if workload_type == 'php':
        memory_limit = workload_settings.get('memoryLimit', '256M')
        max_execution_time = workload_settings.get('maxExecutionTime', 30)
        upload_max_filesize = workload_settings.get('uploadMaxFilesize', '64M')
        post_max_size = workload_settings.get('postMaxSize', '64M')
        # PHP-FPM pool settings
        fpm_max_children = workload_settings.get('fpmMaxChildren', 25)
        fpm_process_idle_timeout = workload_settings.get('fpmProcessIdleTimeout', 30)
    else:
        memory_limit = '256M'
        max_execution_time = 30
        upload_max_filesize = '64M'
        post_max_size = '64M'
        fpm_max_children = 25
        fpm_process_idle_timeout = 30

    # Build desired ConfigMap
    desired_configmap = build_app_configmap(
        namespace_name=namespace_name,
        domain_cr_name=domain_cr_name,
        workload_type=workload_type,
        memory_limit=memory_limit,
        max_execution_time=max_execution_time,
        upload_max_filesize=upload_max_filesize,
        post_max_size=post_max_size,
        custom_config=custom_config,
        fpm_max_children=fpm_max_children,
        fpm_process_idle_timeout=fpm_process_idle_timeout,
    )

    try:
        existing = core_api.read_namespaced_config_map(
            name=configmap_name,
            namespace=namespace_name
        )

        # Check if config changed and update if needed
        if existing.data != desired_configmap.data:
            logger.info(f"Updating app ConfigMap in '{namespace_name}' (config changed)")
            # Debug: show what keys differ
            for key in set(existing.data.keys()) | set(desired_configmap.data.keys()):
                existing_val = existing.data.get(key, '')
                desired_val = desired_configmap.data.get(key, '')
                if existing_val != desired_val:
                    logger.debug(f"  Key '{key}' differs: existing={len(existing_val)} chars, desired={len(desired_val)} chars")
            core_api.replace_namespaced_config_map(
                name=configmap_name,
                namespace=namespace_name,
                body=desired_configmap
            )
            configmap_updated = True
        else:
            logger.debug(f"App ConfigMap unchanged in '{namespace_name}'")

    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating app ConfigMap in '{namespace_name}'...")
            try:
                core_api.create_namespaced_config_map(
                    namespace=namespace_name,
                    body=desired_configmap
                )
                logger.info(f"Created app ConfigMap in '{namespace_name}'")
                configmap_updated = True
            except ApiException as create_err:
                if create_err.status == 409:
                    # Already exists, that's fine
                    pass
                else:
                    logger.error(f"Failed to create app ConfigMap: {create_err}")
                    raise
        else:
            logger.error(f"Failed to read app ConfigMap: {e}")
            raise

    return configmap_updated


# DEPRECATED: Keep for backward compatibility
def ensure_php_configmap(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    php_config: dict,
) -> bool:
    """DEPRECATED: Use ensure_app_configmap instead."""
    php_settings = php_config.get('settings', {})
    custom_config = php_config.get('customConfig', '')
    return ensure_app_configmap(
        core_api, namespace_name, domain_cr_name,
        workload_type='php',
        workload_settings=php_settings,
        custom_config=custom_config,
    )


def ensure_deployment(
    apps_api: client.AppsV1Api,
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    # Workload configuration (replaces php_version)
    workload_type: str,
    workload_version: str,
    workload_image: str,
    workload_port: int,
    workload_command: list,
    workload_args: list,
    workload_env: list,
    # Resource limits
    cpu_limit: str,
    memory_limit: str,
    cpu_request: str,
    memory_request: str,
    wp_preinstall: bool,
    conditions: list,
    force_restart: bool = False,
    # ModSecurity/DomainWAF
    modsec_enabled: bool = False,
    # FastCGI cache
    cache_enabled: bool = False,
    cache_size: str = '512Mi',
    # Node scheduling preferences
    preferred_nodes: list = None,
    # Container timezone
    domain_timezone: str = 'UTC',
    # Container options
    sftp_type: str = 'standard',
    redis_enabled: bool = False,
) -> list:
    """
    Ensure Deployment exists, create if missing.

    Args:
        force_restart: If True, force pod restart even if deployment spec unchanged
                       (used when ConfigMaps change)
    """
    deployment_name = 'web'

    try:
        deployment = apps_api.read_namespaced_deployment(
            name=deployment_name,
            namespace=namespace_name
        )
        logger.debug(f"Deployment '{deployment_name}' exists in '{namespace_name}'")

        # Check if spec changed and update deployment
        needs_update = False
        containers = deployment.spec.template.spec.containers

        # Find the app container and check resources
        # Note: Container name is 'app' for new deployments, 'php' for legacy ones
        for container in containers:
            if container.name in ('app', 'php'):
                current_limits = container.resources.limits or {}
                current_requests = container.resources.requests or {}

                # Compare CPU and memory limits (semantic comparison handles unit normalization)
                if not cpu_equal(current_limits.get('cpu'), cpu_limit):
                    logger.info(f"CPU limit changed: {current_limits.get('cpu')} -> {cpu_limit}")
                    needs_update = True
                if not memory_equal(current_limits.get('memory'), memory_limit):
                    logger.info(f"Memory limit changed: {current_limits.get('memory')} -> {memory_limit}")
                    needs_update = True
                if not cpu_equal(current_requests.get('cpu'), cpu_request):
                    logger.info(f"CPU request changed: {current_requests.get('cpu')} -> {cpu_request}")
                    needs_update = True
                if not memory_equal(current_requests.get('memory'), memory_request):
                    logger.info(f"Memory request changed: {current_requests.get('memory')} -> {memory_request}")
                    needs_update = True
                break

        # Check if workload image changed
        for container in containers:
            if container.name in ('app', 'php'):
                current_image = container.image or ''
                if current_image != workload_image:
                    logger.info(f"Workload image changed: {current_image} -> {workload_image}")
                    needs_update = True
                break

        # Check if timezone changed (check TZ env var on app container)
        for container in containers:
            if container.name in ('app', 'php'):
                current_tz = 'UTC'  # Default if not set
                if container.env:
                    for env_var in container.env:
                        if env_var.name == 'TZ':
                            current_tz = env_var.value or 'UTC'
                            break
                if current_tz != domain_timezone:
                    logger.info(f"Timezone changed: {current_tz} -> {domain_timezone}")
                    needs_update = True
                break

        # Check if SFTP container image changed (standard vs sshgit)
        expected_sftp_image = SSHGIT_IMAGE if sftp_type == 'sshgit' else SFTP_IMAGE
        for container in containers:
            if container.name == 'sftp':
                current_sftp_image = container.image or ''
                if current_sftp_image != expected_sftp_image:
                    logger.info(f"SFTP image changed: {current_sftp_image} -> {expected_sftp_image}")
                    needs_update = True
                break

        # Check if Redis container needs to be added or removed
        has_redis_container = any(c.name == 'redis' for c in containers)
        if redis_enabled and not has_redis_container:
            logger.info(f"Redis enabled but container missing, adding Redis container")
            needs_update = True
        elif not redis_enabled and has_redis_container:
            logger.info(f"Redis disabled but container exists, removing Redis container")
            needs_update = True

        # Force restart if ConfigMaps changed
        if force_restart:
            logger.info(f"ConfigMap changed, forcing deployment restart in '{namespace_name}'")
            needs_update = True

        # Check if replicas need to be scaled up (e.g., after unsuspend)
        current_replicas = deployment.spec.replicas or 0
        if current_replicas == 0:
            logger.info(f"Deployment in '{namespace_name}' has 0 replicas, scaling up to 1")
            needs_update = True

        if needs_update:
            logger.info(f"Updating deployment in '{namespace_name}' with new specs...")
            new_deployment = build_deployment(
                namespace_name=namespace_name,
                domain_cr_name=domain_cr_name,
                domain_name=domain_name,
                workload_type=workload_type,
                workload_version=workload_version,
                workload_image=workload_image,
                workload_port=workload_port,
                workload_command=workload_command,
                workload_args=workload_args,
                workload_env=workload_env,
                cpu_limit=cpu_limit,
                memory_limit=memory_limit,
                cpu_request=cpu_request,
                memory_request=memory_request,
                wp_preinstall=wp_preinstall,
                modsec_enabled=modsec_enabled,
                cache_enabled=cache_enabled,
                cache_size=cache_size,
                preferred_nodes=preferred_nodes,
                timezone=domain_timezone,
                sftp_type=sftp_type,
                redis_enabled=redis_enabled,
            )

            # Add restart annotation if forcing restart due to ConfigMap changes
            if force_restart:
                restart_timestamp = datetime.now().isoformat()
                if new_deployment.spec.template.metadata.annotations is None:
                    new_deployment.spec.template.metadata.annotations = {}
                new_deployment.spec.template.metadata.annotations['kubepanel.io/restart-trigger'] = restart_timestamp
                logger.info(f"Added restart trigger annotation: {restart_timestamp}")

            apps_api.replace_namespaced_deployment(
                name=deployment_name,
                namespace=namespace_name,
                body=new_deployment
            )
            logger.info(f"Updated deployment in '{namespace_name}'")
            conditions = set_condition(
                conditions, 'DeploymentReady', 'Unknown', 'Updated',
                'Deployment updated, waiting for rollout'
            )
        else:
            # Check deployment status
            status, reason, message = get_deployment_status(deployment)
            conditions = set_condition(conditions, 'DeploymentReady', status, reason, message)

    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating deployment in '{namespace_name}'...")
            deployment = build_deployment(
                namespace_name=namespace_name,
                domain_cr_name=domain_cr_name,
                domain_name=domain_name,
                workload_type=workload_type,
                workload_version=workload_version,
                workload_image=workload_image,
                workload_port=workload_port,
                workload_command=workload_command,
                workload_args=workload_args,
                workload_env=workload_env,
                cpu_limit=cpu_limit,
                memory_limit=memory_limit,
                cpu_request=cpu_request,
                memory_request=memory_request,
                wp_preinstall=wp_preinstall,
                modsec_enabled=modsec_enabled,
                cache_enabled=cache_enabled,
                cache_size=cache_size,
                preferred_nodes=preferred_nodes,
                timezone=domain_timezone,
                sftp_type=sftp_type,
                redis_enabled=redis_enabled,
            )
            try:
                apps_api.create_namespaced_deployment(
                    namespace=namespace_name,
                    body=deployment
                )
                logger.info(f"Created deployment in '{namespace_name}'")
                conditions = set_condition(
                    conditions, 'DeploymentReady', 'Unknown', 'Created',
                    'Deployment created, waiting for pods'
                )
            except ApiException as create_err:
                if create_err.status == 409:
                    conditions = set_condition(
                        conditions, 'DeploymentReady', 'Unknown', 'Exists',
                        'Deployment exists, checking status'
                    )
                else:
                    logger.error(f"Failed to create deployment: {create_err}")
                    conditions = set_condition(
                        conditions, 'DeploymentReady', 'False', 'CreateFailed',
                        f"Failed to create deployment: {create_err.reason}"
                    )
                    raise
        else:
            logger.error(f"Failed to read deployment: {e}")
            raise
    
    return conditions


def ensure_service(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    conditions: list
) -> list:
    """
    Ensure web Service exists, create if missing.
    """
    service_name = 'web'
    
    try:
        core_api.read_namespaced_service(
            name=service_name,
            namespace=namespace_name
        )
        logger.debug(f"Service '{service_name}' exists in '{namespace_name}'")
        conditions = set_condition(
            conditions, 'ServiceReady', 'True', 'Exists',
            'Web service exists'
        )
        
    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating web service in '{namespace_name}'...")
            service = build_service(
                namespace_name=namespace_name,
                domain_cr_name=domain_cr_name,
            )
            try:
                core_api.create_namespaced_service(
                    namespace=namespace_name,
                    body=service
                )
                logger.info(f"Created web service in '{namespace_name}'")
                conditions = set_condition(
                    conditions, 'ServiceReady', 'True', 'Created',
                    'Web service created'
                )
            except ApiException as create_err:
                if create_err.status == 409:
                    conditions = set_condition(
                        conditions, 'ServiceReady', 'True', 'Exists',
                        'Web service exists'
                    )
                else:
                    logger.error(f"Failed to create service: {create_err}")
                    conditions = set_condition(
                        conditions, 'ServiceReady', 'False', 'CreateFailed',
                        f"Failed to create service: {create_err.reason}"
                    )
                    raise
        else:
            logger.error(f"Failed to read service: {e}")
            raise
    
    return conditions


def ensure_sftp_service(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    conditions: list,
    status_patch: dict
) -> list:
    """
    Ensure SFTP Service (NodePort) exists, create if missing.
    """
    service_name = 'sftp'
    
    try:
        service = core_api.read_namespaced_service(
            name=service_name,
            namespace=namespace_name
        )
        logger.debug(f"SFTP service exists in '{namespace_name}'")
        
        # Get the assigned NodePort
        node_port = None
        for port in service.spec.ports or []:
            if port.name == 'sftp':
                node_port = port.node_port
                break
        
        if node_port:
            status_patch.setdefault('sftp', {})['port'] = node_port
        
        conditions = set_condition(
            conditions, 'SFTPServiceReady', 'True', 'Exists',
            f'SFTP service exists on port {node_port}'
        )
        
    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating SFTP service in '{namespace_name}'...")
            service = build_sftp_service(
                namespace_name=namespace_name,
                domain_cr_name=domain_cr_name,
            )
            try:
                created = core_api.create_namespaced_service(
                    namespace=namespace_name,
                    body=service
                )
                
                # Get assigned NodePort
                node_port = None
                for port in created.spec.ports or []:
                    if port.name == 'sftp':
                        node_port = port.node_port
                        break
                
                logger.info(f"Created SFTP service in '{namespace_name}' on port {node_port}")
                
                if node_port:
                    status_patch.setdefault('sftp', {})['port'] = node_port
                
                conditions = set_condition(
                    conditions, 'SFTPServiceReady', 'True', 'Created',
                    f'SFTP service created on port {node_port}'
                )
            except ApiException as create_err:
                if create_err.status == 409:
                    conditions = set_condition(
                        conditions, 'SFTPServiceReady', 'True', 'Exists',
                        'SFTP service exists'
                    )
                else:
                    logger.error(f"Failed to create SFTP service: {create_err}")
                    conditions = set_condition(
                        conditions, 'SFTPServiceReady', 'False', 'CreateFailed',
                        f"Failed to create SFTP service: {create_err.reason}"
                    )
                    raise
        else:
            logger.error(f"Failed to read SFTP service: {e}")
            raise
    
    return conditions


def ensure_ingress(
    networking_api: client.NetworkingV1Api,
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    aliases: list,
    ssl_redirect: bool,
    www_redirect: str,
    conditions: list,
    status_patch: dict
) -> list:
    """
    Ensure Ingress exists with correct hosts, create or update as needed.
    """
    ingress_name = 'web'

    # Build desired ingress to compare
    desired_ingress = build_ingress(
        namespace_name=namespace_name,
        domain_cr_name=domain_cr_name,
        domain_name=domain_name,
        aliases=aliases,
        ssl_redirect=ssl_redirect,
        www_redirect=www_redirect,
    )

    # Extract desired hosts from rules
    desired_hosts = set()
    for rule in desired_ingress.spec.rules:
        if rule.host:
            desired_hosts.add(rule.host)

    try:
        existing_ingress = networking_api.read_namespaced_ingress(
            name=ingress_name,
            namespace=namespace_name
        )
        logger.debug(f"Ingress '{ingress_name}' exists in '{namespace_name}'")

        # Extract current hosts from existing ingress
        current_hosts = set()
        if existing_ingress.spec.rules:
            for rule in existing_ingress.spec.rules:
                if rule.host:
                    current_hosts.add(rule.host)

        # Check if hosts changed
        if current_hosts != desired_hosts:
            logger.info(f"Updating ingress in '{namespace_name}' - hosts changed: {current_hosts} -> {desired_hosts}")
            networking_api.replace_namespaced_ingress(
                name=ingress_name,
                namespace=namespace_name,
                body=desired_ingress
            )
            conditions = set_condition(
                conditions, 'IngressReady', 'Unknown', 'Updated',
                'Ingress updated with new hosts'
            )
        else:
            # Check ingress status
            status, reason, message = get_ingress_status(existing_ingress)
            conditions = set_condition(conditions, 'IngressReady', status, reason, message)

        # Update status with endpoints
        status_patch['endpoints'] = {
            'http': f'http://{domain_name}',
            'https': f'https://{domain_name}',
        }

    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating ingress in '{namespace_name}'...")
            try:
                networking_api.create_namespaced_ingress(
                    namespace=namespace_name,
                    body=desired_ingress
                )
                logger.info(f"Created ingress for '{domain_name}'")

                status_patch['endpoints'] = {
                    'http': f'http://{domain_name}',
                    'https': f'https://{domain_name}',
                }

                conditions = set_condition(
                    conditions, 'IngressReady', 'Unknown', 'Created',
                    'Ingress created, waiting for certificate'
                )
            except ApiException as create_err:
                if create_err.status == 409:
                    conditions = set_condition(
                        conditions, 'IngressReady', 'Unknown', 'Exists',
                        'Ingress exists'
                    )
                else:
                    logger.error(f"Failed to create ingress: {create_err}")
                    conditions = set_condition(
                        conditions, 'IngressReady', 'False', 'CreateFailed',
                        f"Failed to create ingress: {create_err.reason}"
                    )
                    raise
        else:
            logger.error(f"Failed to read ingress: {e}")
            raise

    return conditions


# =============================================================================
# Backup Resource Reconciliation
# =============================================================================

def ensure_backup_pvc(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    backup_storage_size: str,
    conditions: list
) -> list:
    """
    Ensure backup PVC exists, create if missing.
    """
    pvc_name = 'backup'

    try:
        core_api.read_namespaced_persistent_volume_claim(
            name=pvc_name,
            namespace=namespace_name
        )
        logger.debug(f"Backup PVC exists in '{namespace_name}'")
        conditions = set_condition(
            conditions, 'BackupPVCReady', 'True', 'Exists',
            'Backup PVC exists'
        )

    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating backup PVC in '{namespace_name}'...")
            pvc = build_backup_pvc(
                namespace_name=namespace_name,
                domain_cr_name=domain_cr_name,
                storage_size=backup_storage_size
            )
            try:
                core_api.create_namespaced_persistent_volume_claim(
                    namespace=namespace_name,
                    body=pvc
                )
                logger.info(f"Created backup PVC in '{namespace_name}'")
                conditions = set_condition(
                    conditions, 'BackupPVCReady', 'True', 'Created',
                    'Backup PVC created'
                )
            except ApiException as create_err:
                if create_err.status == 409:
                    conditions = set_condition(
                        conditions, 'BackupPVCReady', 'True', 'Exists',
                        'Backup PVC exists'
                    )
                else:
                    logger.error(f"Failed to create backup PVC: {create_err}")
                    conditions = set_condition(
                        conditions, 'BackupPVCReady', 'False', 'CreateFailed',
                        f"Failed to create backup PVC: {create_err.reason}"
                    )
        else:
            logger.error(f"Failed to read backup PVC: {e}")

    return conditions


def ensure_backup_service_account(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    conditions: list
) -> list:
    """
    Ensure backup ServiceAccount exists, create if missing.
    """
    sa_name = 'kubepanel-backup'

    try:
        core_api.read_namespaced_service_account(
            name=sa_name,
            namespace=namespace_name
        )
        logger.debug(f"Backup ServiceAccount exists in '{namespace_name}'")

    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating backup ServiceAccount in '{namespace_name}'...")
            sa = build_backup_service_account(
                namespace_name=namespace_name,
                domain_cr_name=domain_cr_name
            )
            try:
                core_api.create_namespaced_service_account(
                    namespace=namespace_name,
                    body=sa
                )
                logger.info(f"Created backup ServiceAccount in '{namespace_name}'")
            except ApiException as create_err:
                if create_err.status != 409:
                    logger.error(f"Failed to create backup ServiceAccount: {create_err}")
        else:
            logger.error(f"Failed to read backup ServiceAccount: {e}")

    return conditions


def ensure_backup_role_binding(
    rbac_api: client.RbacAuthorizationV1Api,
    namespace_name: str,
    domain_cr_name: str,
    conditions: list
) -> list:
    """
    Ensure backup RoleBinding exists, create if missing.
    """
    rb_name = 'kubepanel-backup'

    try:
        rbac_api.read_namespaced_role_binding(
            name=rb_name,
            namespace=namespace_name
        )
        logger.debug(f"Backup RoleBinding exists in '{namespace_name}'")

    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating backup RoleBinding in '{namespace_name}'...")
            rb = build_backup_role_binding(
                namespace_name=namespace_name,
                domain_cr_name=domain_cr_name
            )
            try:
                rbac_api.create_namespaced_role_binding(
                    namespace=namespace_name,
                    body=rb
                )
                logger.info(f"Created backup RoleBinding in '{namespace_name}'")
            except ApiException as create_err:
                if create_err.status != 409:
                    logger.error(f"Failed to create backup RoleBinding: {create_err}")
        else:
            logger.error(f"Failed to read backup RoleBinding: {e}")

    return conditions


def ensure_backup_credentials_secret(
    core_api: client.CoreV1Api,
    namespace_name: str,
    domain_cr_name: str,
    conditions: list
) -> list:
    """
    Ensure backup credentials secret exists, create if missing.
    This copies the MariaDB root password to the domain namespace for backup job access.
    """
    secret_name = 'backup-credentials'

    try:
        core_api.read_namespaced_secret(
            name=secret_name,
            namespace=namespace_name
        )
        logger.debug(f"Backup credentials secret exists in '{namespace_name}'")

    except ApiException as e:
        if e.status == 404:
            logger.info(f"Creating backup credentials secret in '{namespace_name}'...")
            try:
                # Get MariaDB root password from kubepanel namespace
                _, mariadb_password = get_mariadb_root_credentials(core_api)

                secret = build_backup_credentials_secret(
                    namespace_name=namespace_name,
                    domain_cr_name=domain_cr_name,
                    mariadb_root_password=mariadb_password
                )
                core_api.create_namespaced_secret(
                    namespace=namespace_name,
                    body=secret
                )
                logger.info(f"Created backup credentials secret in '{namespace_name}'")
            except ApiException as create_err:
                if create_err.status != 409:
                    logger.error(f"Failed to create backup credentials secret: {create_err}")
        else:
            logger.error(f"Failed to read backup credentials secret: {e}")

    return conditions


# =============================================================================
# Database Provisioning
# =============================================================================

def get_mariadb_root_credentials(core_api: client.CoreV1Api) -> tuple[str, str]:
    """
    Get MariaDB root credentials from the mariadb-auth Secret.
    
    Returns:
        Tuple of (username, password)
    """
    try:
        secret = core_api.read_namespaced_secret(
            name=MARIADB_SECRET_NAME,
            namespace=MARIADB_SECRET_NAMESPACE
        )
        username = base64.b64decode(secret.data.get('username', '')).decode('utf-8')
        password = base64.b64decode(secret.data.get('password', '')).decode('utf-8')
        return username, password
    except ApiException as e:
        logger.error(f"Failed to read MariaDB credentials: {e}")
        raise


def ensure_database_provisioned(
    core_api: client.CoreV1Api,
    db_name: str,
    db_user: str,
    db_password: str,
    conditions: list
) -> list:
    """
    Ensure database and user exist in MariaDB.
    
    Creates the database and user if they don't exist, grants permissions.
    """
    try:
        # Get root credentials
        root_user, root_password = get_mariadb_root_credentials(core_api)
        
        # Connect to MariaDB
        conn = pymysql.connect(
            host=MARIADB_HOST,
            port=MARIADB_PORT,
            user=root_user,
            password=root_password,
            connect_timeout=10
        )
        cursor = conn.cursor()

        try:
            # Create database if not exists
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}`")
            logger.debug(f"Ensured database '{db_name}' exists")
            
            # Create user if not exists (MariaDB 10.1.3+)
            # Note: %% escapes the literal % for PyMySQL's parameter substitution
            cursor.execute(
                f"CREATE USER IF NOT EXISTS '{db_user}'@'%%' IDENTIFIED BY %s",
                (db_password,)
            )
            logger.debug(f"Ensured user '{db_user}' exists")
            
            # Update password in case it changed
            cursor.execute(
                f"ALTER USER '{db_user}'@'%%' IDENTIFIED BY %s",
                (db_password,)
            )
            
            # Grant all privileges on the database
            cursor.execute(f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'%'")
            cursor.execute("FLUSH PRIVILEGES")
            
            conn.commit()
            logger.info(f"Database '{db_name}' and user '{db_user}' provisioned successfully")
            
            conditions = set_condition(
                conditions, 'DatabaseProvisioned', 'True', 'Provisioned',
                f"Database '{db_name}' and user '{db_user}' ready"
            )
            
        finally:
            cursor.close()
            conn.close()
            
    except MySQLError as e:
        logger.error(f"Failed to provision database: {e}")
        conditions = set_condition(
            conditions, 'DatabaseProvisioned', 'False', 'ProvisioningFailed',
            f"Failed to provision database: {str(e)}"
        )
        # Don't raise - let reconciliation continue, will retry
        
    except ApiException as e:
        logger.error(f"Failed to get MariaDB credentials: {e}")
        conditions = set_condition(
            conditions, 'DatabaseProvisioned', 'False', 'CredentialsNotFound',
            f"Failed to get MariaDB root credentials"
        )
    
    return conditions


def delete_database_and_user(
    core_api: client.CoreV1Api,
    db_name: str,
    db_user: str
) -> bool:
    """
    Delete database and user from MariaDB.

    Returns:
        True if successful, False otherwise
    """
    try:
        root_user, root_password = get_mariadb_root_credentials(core_api)

        conn = pymysql.connect(
            host=MARIADB_HOST,
            port=MARIADB_PORT,
            user=root_user,
            password=root_password,
            connect_timeout=10
        )
        cursor = conn.cursor()

        try:
            # Drop user first (to revoke all privileges)
            cursor.execute(f"DROP USER IF EXISTS '{db_user}'@'%'")
            logger.debug(f"Dropped user '{db_user}'")

            # Drop database
            cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
            logger.debug(f"Dropped database '{db_name}'")

            conn.commit()
            logger.info(f"Database '{db_name}' and user '{db_user}' deleted successfully")
            return True

        finally:
            cursor.close()
            conn.close()

    except MySQLError as e:
        logger.error(f"Failed to delete database: {e}")
        return False

    except ApiException as e:
        logger.error(f"Failed to get MariaDB credentials for deletion: {e}")
        return False


# =============================================================================
# DKIM Central Configuration Management
# =============================================================================

def update_central_dkim_secret(
    core_api: client.CoreV1Api,
    domain_name: str,
    private_key: str,
    max_retries: int = 5,
) -> bool:
    """
    Add/update domain's DKIM private key in central secret.

    Creates the secret if it doesn't exist.
    Uses retry logic to handle concurrent updates (409 Conflict).

    Returns:
        True if secret was created/updated, False if already up-to-date
    """
    secret_key = f"{domain_name}.key"
    encoded_key = base64.b64encode(private_key.encode()).decode()

    for attempt in range(max_retries):
        try:
            # Read existing secret
            secret = core_api.read_namespaced_secret(
                name=DKIM_KEYS_SECRET,
                namespace=DKIM_NAMESPACE
            )
            if secret.data is None:
                secret.data = {}

            # Check if key already exists with same value (idempotency)
            if secret.data.get(secret_key) == encoded_key:
                logger.debug(f"DKIM key for '{domain_name}' already up-to-date in central secret")
                return False

            # Add/update key
            secret.data[secret_key] = encoded_key
            core_api.replace_namespaced_secret(
                name=DKIM_KEYS_SECRET,
                namespace=DKIM_NAMESPACE,
                body=secret
            )
            logger.info(f"Updated DKIM key for '{domain_name}' in central secret")
            return True
        except ApiException as e:
            if e.status == 404:
                # Create new secret
                try:
                    secret = client.V1Secret(
                        metadata=client.V1ObjectMeta(
                            name=DKIM_KEYS_SECRET,
                            namespace=DKIM_NAMESPACE,
                            labels={
                                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                                'app.kubernetes.io/component': 'dkim'
                            }
                        ),
                        data={secret_key: encoded_key}
                    )
                    core_api.create_namespaced_secret(
                        namespace=DKIM_NAMESPACE,
                        body=secret
                    )
                    logger.info(f"Created central DKIM secret with key for '{domain_name}'")
                    return True
                except ApiException as create_err:
                    if create_err.status == 409:
                        # Secret was created by another process, retry update
                        logger.debug(f"Secret created concurrently, retrying update for '{domain_name}'")
                        continue
                    raise
            elif e.status == 409:
                # Conflict - secret was modified, retry with fresh read
                logger.debug(f"Conflict updating DKIM secret for '{domain_name}', retry {attempt + 1}/{max_retries}")
                continue
            else:
                logger.error(f"Failed to update central DKIM secret: {e}")
                raise

    logger.error(f"Failed to update central DKIM secret for '{domain_name}' after {max_retries} retries")
    return False


def _update_dkim_configmap_entry(
    core_api: client.CoreV1Api,
    cm_name: str,
    data_key: str,
    domain_name: str,
    new_entry: str,
    max_retries: int = 5,
) -> bool:
    """
    Helper to update a single ConfigMap with a domain entry.

    Removes any existing entry for the domain and adds the new one.
    Uses retry logic to handle concurrent updates (409 Conflict).

    Returns:
        True if ConfigMap was updated, False if entry already exists
    """
    for attempt in range(max_retries):
        try:
            cm = core_api.read_namespaced_config_map(
                name=cm_name,
                namespace=DKIM_NAMESPACE
            )
            current_content = cm.data.get(data_key, '') if cm.data else ''

            # Check if entry already exists (idempotency)
            if new_entry in current_content:
                logger.debug(f"Entry for '{domain_name}' already exists in {cm_name}")
                return False

            # Parse lines, remove existing entry for this domain, add new
            lines = [l for l in current_content.strip().split('\n') if l and domain_name not in l]
            lines.append(new_entry)

            if cm.data is None:
                cm.data = {}
            cm.data[data_key] = '\n'.join(lines) + '\n'

            core_api.replace_namespaced_config_map(
                name=cm_name,
                namespace=DKIM_NAMESPACE,
                body=cm
            )
            logger.debug(f"Updated {cm_name} ConfigMap with entry for '{domain_name}'")
            return True
        except ApiException as e:
            if e.status == 409:
                logger.debug(f"Conflict updating {cm_name} for '{domain_name}', retry {attempt + 1}/{max_retries}")
                continue
            logger.error(f"Failed to update {cm_name} ConfigMap: {e}")
            raise

    logger.error(f"Failed to update {cm_name} ConfigMap for '{domain_name}' after {max_retries} retries")
    return False


def update_central_dkim_configmaps(
    core_api: client.CoreV1Api,
    domain_name: str,
    selector: str = 'default',
) -> bool:
    """
    Add/update domain entries in KeyTable and SigningTable ConfigMaps.

    Returns:
        True if any ConfigMap was updated, False if all already up-to-date
    """
    # KeyTable entry format: selector._domainkey.domain domain:selector:/path/to/key
    keytable_entry = f"{selector}._domainkey.{domain_name} {domain_name}:{selector}:/etc/opendkim/keys/{domain_name}.key"

    # SigningTable entry format: *@domain selector._domainkey.domain
    signingtable_entry = f"*@{domain_name} {selector}._domainkey.{domain_name}"

    # Update KeyTable
    keytable_updated = _update_dkim_configmap_entry(
        core_api, DKIM_KEYTABLE_CM, 'KeyTable', domain_name, keytable_entry
    )
    if keytable_updated:
        logger.info(f"Added KeyTable entry for '{domain_name}'")

    # Update SigningTable
    signingtable_updated = _update_dkim_configmap_entry(
        core_api, DKIM_SIGNINGTABLE_CM, 'SigningTable', domain_name, signingtable_entry
    )
    if signingtable_updated:
        logger.info(f"Added SigningTable entry for '{domain_name}'")

    return keytable_updated or signingtable_updated


def restart_opendkim_deployment() -> None:
    """
    Trigger a rolling restart of the OpenDKIM deployment.

    This is called after updating DKIM configuration so that
    OpenDKIM picks up the new keys and config.
    """
    try:
        apps_api = client.AppsV1Api()
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubepanel.io/restart-trigger": datetime.now(timezone.utc).isoformat()
                        }
                    }
                }
            }
        }
        apps_api.patch_namespaced_deployment(
            name="opendkim",
            namespace=DKIM_NAMESPACE,
            body=patch
        )
        logger.info("Triggered OpenDKIM deployment restart to pick up new DKIM config")
    except ApiException as e:
        logger.warning(f"Failed to restart OpenDKIM deployment: {e}")
        # Don't fail the reconciliation - the config is already updated
        # OpenDKIM will pick it up on next natural restart


def remove_domain_from_central_dkim(
    core_api: client.CoreV1Api,
    domain_name: str,
) -> None:
    """
    Remove domain from central DKIM configuration on deletion.

    Removes the key from the secret and entries from ConfigMaps.
    """
    secret_key = f"{domain_name}.key"

    # Remove from Secret
    try:
        secret = core_api.read_namespaced_secret(
            name=DKIM_KEYS_SECRET,
            namespace=DKIM_NAMESPACE
        )
        if secret.data and secret_key in secret.data:
            del secret.data[secret_key]
            core_api.replace_namespaced_secret(
                name=DKIM_KEYS_SECRET,
                namespace=DKIM_NAMESPACE,
                body=secret
            )
            logger.info(f"Removed DKIM key for '{domain_name}' from central secret")
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to remove DKIM key from secret: {e}")

    # Remove from ConfigMaps
    for cm_name, data_key in [(DKIM_KEYTABLE_CM, 'KeyTable'), (DKIM_SIGNINGTABLE_CM, 'SigningTable')]:
        try:
            cm = core_api.read_namespaced_config_map(
                name=cm_name,
                namespace=DKIM_NAMESPACE
            )
            current = cm.data.get(data_key, '') if cm.data else ''
            lines = [l for l in current.strip().split('\n') if l and domain_name not in l]

            if cm.data is None:
                cm.data = {}
            cm.data[data_key] = '\n'.join(lines) + '\n' if lines else ''

            core_api.replace_namespaced_config_map(
                name=cm_name,
                namespace=DKIM_NAMESPACE,
                body=cm
            )
            logger.info(f"Removed '{domain_name}' entry from {cm_name}")
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to remove DKIM entry from {cm_name}: {e}")


# =============================================================================
# DNS Management (Cloudflare)
# =============================================================================

def get_cloudflare_client(
    core_api: client.CoreV1Api,
    credential_ref: dict,
) -> Cloudflare:
    """
    Get Cloudflare client from K8s Secret reference.

    Args:
        core_api: Kubernetes Core API client
        credential_ref: Dict with 'name' and 'namespace' pointing to secret

    Returns:
        Configured Cloudflare client
    """
    secret_name = credential_ref.get('name')
    secret_namespace = credential_ref.get('namespace', 'kubepanel')

    logger.info(f"Reading Cloudflare credential from secret '{secret_namespace}/{secret_name}'")

    secret = core_api.read_namespaced_secret(
        name=secret_name,
        namespace=secret_namespace
    )

    api_token = base64.b64decode(secret.data.get('api_token', '')).decode('utf-8')
    if not api_token:
        logger.error(f"No api_token found in secret '{secret_namespace}/{secret_name}'")
        raise ValueError(f"No api_token found in secret '{secret_namespace}/{secret_name}'")

    logger.info(f"Successfully loaded Cloudflare API token (length: {len(api_token)})")
    return Cloudflare(api_token=api_token)


def get_cluster_ips(core_api: client.CoreV1Api) -> list:
    """
    Get public IPs of cluster nodes from ConfigMap.

    Returns:
        List of public IP addresses
    """
    try:
        cm = core_api.read_namespaced_config_map(
            name='node-public-ips',
            namespace='kubepanel'
        )
        if cm.data:
            return list(cm.data.values())
        return []
    except ApiException as e:
        logger.warning(f"Failed to read node-public-ips ConfigMap: {e}")
        return []


def ensure_dns_record(
    cf_client: Cloudflare,
    zone_id: str,
    record_type: str,
    name: str,
    content: str,
    ttl: int = 1,
    proxied: bool = False,
    priority: int = None,
    existing_record_id: str = None,
    existing_content: str = None,
) -> dict:
    """
    Create or update a single DNS record in Cloudflare.

    Args:
        cf_client: Cloudflare client
        zone_id: Zone ID
        record_type: Record type (A, AAAA, CNAME, MX, TXT, etc.)
        name: Record name
        content: Record content/value
        ttl: TTL (1 = auto)
        proxied: Whether to proxy through Cloudflare
        priority: Priority for MX/SRV records
        existing_record_id: If provided from status, use this ID directly (skip API lookup)
        existing_content: Content from status - if matches, skip entirely

    Returns:
        Dict with record status info
    """
    def _get_attr(obj, key):
        """Helper to get attribute from dict or object."""
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    try:
        # If we have an existing record ID from status
        if existing_record_id:
            # Check if content matches - if so, record is already synced
            if existing_content == content:
                logger.debug(f"DNS record already synced: {record_type} {name}")
                return {
                    'type': record_type,
                    'name': name,
                    'content': content,
                    'ttl': ttl,
                    'proxied': proxied,
                    'priority': priority,
                    'recordId': existing_record_id,
                    'status': 'Ready',
                }

            # Content differs - UPDATE the existing record directly (no API lookup)
            params = {
                'zone_id': zone_id,
                'type': record_type,
                'name': name,
                'content': content,
                'ttl': ttl,
                'proxied': proxied,
            }
            if priority is not None:
                params['priority'] = priority
            cf_client.dns.records.update(existing_record_id, **params)
            logger.info(f"Updated DNS record: {record_type} {name} -> {content[:50] if len(content) > 50 else content}")

            return {
                'type': record_type,
                'name': name,
                'content': content,
                'ttl': ttl,
                'proxied': proxied,
                'priority': priority,
                'recordId': existing_record_id,
                'status': 'Ready',
            }

        # No existing record ID - need to query Cloudflare API
        existing = cf_client.dns.records.list(
            zone_id=zone_id,
            type=record_type,
            name=name,
        )

        record_id = None
        if existing.result:
            # Check if content matches any existing record
            for rec in existing.result:
                rec_content = _get_attr(rec, 'content')
                if rec_content == content:
                    record_id = _get_attr(rec, 'id')
                    logger.debug(f"Found existing DNS record: {record_type} {name} with matching content")
                    return {
                        'type': record_type,
                        'name': name,
                        'content': content,
                        'ttl': ttl,
                        'proxied': proxied,
                        'priority': priority,
                        'recordId': record_id,
                        'status': 'Ready',
                    }

            # Content differs - update existing record
            if len(existing.result) > 0:
                record_id = _get_attr(existing.result[0], 'id')
                params = {
                    'zone_id': zone_id,
                    'type': record_type,
                    'name': name,
                    'content': content,
                    'ttl': ttl,
                    'proxied': proxied,
                }
                if priority is not None:
                    params['priority'] = priority
                cf_client.dns.records.update(record_id, **params)
                logger.info(f"Updated DNS record: {record_type} {name} -> {content[:50] if len(content) > 50 else content}")

                return {
                    'type': record_type,
                    'name': name,
                    'content': content,
                    'ttl': ttl,
                    'proxied': proxied,
                    'priority': priority,
                    'recordId': record_id,
                    'status': 'Ready',
                }

        # No existing record found - create new one
        if not record_id:
            # Create new record
            params = {
                'zone_id': zone_id,
                'type': record_type,
                'name': name,
                'content': content,
                'ttl': ttl,
                'proxied': proxied,
            }
            if priority is not None:
                params['priority'] = priority
            try:
                result = cf_client.dns.records.create(**params)
                record_id = _get_attr(result, 'id')
                logger.debug(f"Created DNS record: {record_type} {name} -> {content[:50]}...")
            except CloudflareAPIError as create_error:
                # Handle "identical record already exists" error
                error_str = str(create_error)
                if '81058' in error_str or 'identical record already exists' in error_str.lower():
                    # Record exists - try to find it again with broader search
                    logger.debug(f"Record already exists, fetching existing record ID for {record_type} {name}")
                    all_records = cf_client.dns.records.list(zone_id=zone_id, type=record_type)
                    for rec in all_records.result or []:
                        rec_name = _get_attr(rec, 'name')
                        rec_content = _get_attr(rec, 'content')
                        # Match by name suffix and content
                        if rec_name and (rec_name == name or rec_name.startswith(name + '.') or rec_name.endswith('.' + name)):
                            if rec_content == content:
                                record_id = _get_attr(rec, 'id')
                                logger.debug(f"Found existing record ID: {record_id}")
                                break
                    if not record_id:
                        # Still can't find it, but it exists - use a placeholder
                        record_id = 'exists-not-tracked'
                        logger.debug(f"Record exists but couldn't retrieve ID for {record_type} {name}")
                else:
                    raise  # Re-raise if it's a different error

        return {
            'type': record_type,
            'name': name,
            'content': content,
            'recordId': record_id,
            'status': 'Ready',
        }

    except CloudflareAPIError as e:
        logger.warning(f"Failed to create/update DNS record {record_type} {name}: {e}")
        return {
            'type': record_type,
            'name': name,
            'content': content,
            'status': 'Failed',
            'error': str(e),
        }


def ensure_dns(
    core_api: client.CoreV1Api,
    domain_name: str,
    dns_config: dict,
    dkim_dns_record: str,
    conditions: list,
    status_patch: dict,
    current_status: dict = None,
) -> list:
    """
    Ensure DNS zone and records exist in Cloudflare.

    This is called during domain reconciliation if DNS is enabled.
    Implements idempotency - skips API calls if DNS is already configured.

    Args:
        core_api: Kubernetes Core API client
        domain_name: The domain name
        dns_config: DNS configuration from Domain CR spec
        dkim_dns_record: DKIM TXT record value (from email config)
        conditions: Current conditions list
        status_patch: Status patch dict to update
        current_status: Current CR status (for idempotency check)

    Returns:
        Updated conditions list
    """
    if not dns_config.get('enabled'):
        logger.debug(f"DNS not enabled for '{domain_name}', skipping")
        return conditions

    credential_ref = dns_config.get('credentialSecretRef')
    if not credential_ref:
        logger.warning(f"DNS enabled but no credentialSecretRef for '{domain_name}'")
        conditions = set_condition(
            conditions, 'DNSReady', 'False', 'NoCredentials',
            'DNS enabled but no Cloudflare credentials configured'
        )
        return conditions

    # Idempotency check: skip if DNS is already fully configured AND spec hasn't changed
    current_dns = (current_status or {}).get('dns', {})
    spec_records = dns_config.get('records', [])

    if current_dns.get('phase') == 'Ready':
        current_records = current_dns.get('records', [])
        # Check all records have recordId (meaning they were created in Cloudflare)
        all_records_ready = all(
            r.get('recordId') and r.get('status') == 'Ready'
            for r in current_records
        )

        # Check if spec records match what's in status (detect new/changed records)
        def normalize_record(r):
            return (r.get('type'), r.get('name'), r.get('content'))

        status_record_set = {normalize_record(r) for r in current_records}
        spec_record_set = {normalize_record(r) for r in spec_records}

        # If spec has records not in status, we need to reconcile
        new_records_in_spec = spec_record_set - status_record_set

        # Also check for edit operations: spec records with recordId
        # where content differs from status (pending updates)
        status_by_id = {r.get('recordId'): r for r in current_records if r.get('recordId')}
        pending_edits = []
        for spec_rec in spec_records:
            spec_record_id = spec_rec.get('recordId')
            if spec_record_id and spec_record_id in status_by_id:
                status_rec = status_by_id[spec_record_id]
                if spec_rec.get('content') != status_rec.get('content'):
                    pending_edits.append(spec_record_id)

        if new_records_in_spec:
            logger.info(f"Detected {len(new_records_in_spec)} new records in spec for '{domain_name}', reconciling DNS")
        elif pending_edits:
            logger.info(f"Detected {len(pending_edits)} pending edits in spec for '{domain_name}', reconciling DNS")
        elif current_dns.get('zone', {}).get('id') and all_records_ready:
            logger.debug(f"DNS already configured for '{domain_name}', skipping Cloudflare API calls")
            # Preserve current DNS status
            status_patch['dns'] = current_dns
            conditions = set_condition(
                conditions, 'DNSReady', 'True', 'Configured',
                f"DNS configured with {len(current_records)} records"
            )
            return conditions

    logger.info(f"Configuring DNS for '{domain_name}'...")

    # Initialize DNS status
    status_patch.setdefault('dns', {})['phase'] = 'Provisioning'

    # Build lookup map from status records: (type, name) -> {recordId, content}
    # This allows us to find existing recordIds for updates without API calls
    status_lookup = {}
    for sr in current_dns.get('records', []):
        key = (sr.get('type'), sr.get('name'))
        # For multiple records with same (type, name), keep all of them
        if key not in status_lookup:
            status_lookup[key] = []
        status_lookup[key].append({
            'recordId': sr.get('recordId'),
            'content': sr.get('content'),
        })

    def find_status_record(record_type, name, content=None):
        """Find existing status record by (type, name), optionally matching content."""
        key = (record_type, name)
        records = status_lookup.get(key, [])
        if not records:
            return None, None

        # If content specified, try to find exact match first
        if content:
            for r in records:
                if r['content'] == content:
                    return r['recordId'], r['content']

        # Return first record (for updates when content changed)
        return records[0]['recordId'], records[0]['content']

    try:
        # Get Cloudflare client
        cf_client = get_cloudflare_client(core_api, credential_ref)

        # Get or create zone - reuse from status if available
        zone_config = dns_config.get('zone', {})
        zone_name = zone_config.get('name', domain_name)

        # Try to reuse zone_id from status first (skip API call)
        zone_id = current_dns.get('zone', {}).get('id')
        zones = None
        if zone_id:
            logger.debug(f"Reusing zone ID '{zone_id}' from status for '{zone_name}'")
        else:
            # Look for existing zone in Cloudflare
            logger.info(f"Looking for zone '{zone_name}' in Cloudflare...")
            zones = cf_client.zones.list(name=zone_name)

        if not zone_id and zones and zones.result:
            # Handle both dict and object response formats
            first_zone = zones.result[0]
            if isinstance(first_zone, dict):
                zone_id = first_zone['id']
            else:
                zone_id = first_zone.id
            logger.info(f"Found existing zone '{zone_name}' with ID '{zone_id}'")
        elif not zone_id and zone_config.get('create', True):
            # Try to create the zone (only if we don't have zone_id from status or API)
            logger.info(f"Zone '{zone_name}' not found, attempting to create...")
            try:
                # Get account_id from the API token (first account)
                accounts = cf_client.accounts.list()
                if not accounts.result:
                    logger.error(f"No Cloudflare accounts found for this API token")
                    conditions = set_condition(
                        conditions, 'DNSReady', 'False', 'NoAccount',
                        'No Cloudflare account found for this API token'
                    )
                    status_patch['dns']['phase'] = 'Failed'
                    status_patch['dns']['message'] = 'No Cloudflare account found'
                    return conditions

                # Handle both dict and object response formats
                first_account = accounts.result[0]
                if isinstance(first_account, dict):
                    account_id = first_account['id']
                else:
                    account_id = first_account.id
                logger.info(f"Using Cloudflare account ID: {account_id}")

                # Create the zone
                new_zone = cf_client.zones.create(
                    name=zone_name,
                    account={"id": account_id},
                    type="full"
                )
                # Handle both dict and object response formats
                if isinstance(new_zone, dict):
                    zone_id = new_zone['id']
                else:
                    zone_id = new_zone.id
                logger.info(f"Created zone '{zone_name}' with ID '{zone_id}'")

            except CloudflareAPIError as e:
                logger.error(f"Failed to create zone '{zone_name}': {e}")
                conditions = set_condition(
                    conditions, 'DNSReady', 'False', 'ZoneCreateFailed',
                    f"Failed to create zone: {str(e)}"
                )
                status_patch['dns']['phase'] = 'Failed'
                status_patch['dns']['message'] = f"Failed to create zone: {str(e)}"
                return conditions
        elif not zone_id:
            # No zone_id from status, API lookup failed/empty, and create=false
            logger.warning(f"Zone '{zone_name}' not found and create=false")
            conditions = set_condition(
                conditions, 'DNSReady', 'False', 'ZoneNotFound',
                f"Zone '{zone_name}' not found in Cloudflare"
            )
            status_patch['dns']['phase'] = 'Failed'
            status_patch['dns']['message'] = f"Zone '{zone_name}' not found"
            return conditions

        if not zone_id:
            conditions = set_condition(
                conditions, 'DNSReady', 'False', 'ZoneNotFound',
                f"Zone '{zone_name}' not found in Cloudflare"
            )
            status_patch['dns']['phase'] = 'Failed'
            return conditions

        # Store zone info in status
        status_patch['dns']['zone'] = {
            'id': zone_id,
            'name': zone_name,
        }

        records_status = []

        # Create auto records if enabled
        if dns_config.get('autoCreateRecords', True):
            cluster_ips = get_cluster_ips(core_api)

            if cluster_ips:
                # A records for root and www
                for ip in cluster_ips:
                    existing_id, existing_content = find_status_record('A', '@', ip)
                    record = ensure_dns_record(
                        cf_client, zone_id, 'A', '@', ip,
                        existing_record_id=existing_id,
                        existing_content=existing_content,
                    )
                    records_status.append(record)

                    existing_id, existing_content = find_status_record('A', 'www', ip)
                    record = ensure_dns_record(
                        cf_client, zone_id, 'A', 'www', ip,
                        existing_record_id=existing_id,
                        existing_content=existing_content,
                    )
                    records_status.append(record)

                # MX records
                for i, ip in enumerate(cluster_ips):
                    # Create A record for mail server
                    mx_hostname = f'mx{i}'
                    existing_id, existing_content = find_status_record('A', mx_hostname, ip)
                    record = ensure_dns_record(
                        cf_client, zone_id, 'A', mx_hostname, ip,
                        existing_record_id=existing_id,
                        existing_content=existing_content,
                    )
                    records_status.append(record)

                    # Create MX record
                    mx_content = f'mx{i}.{zone_name}'
                    existing_id, existing_content = find_status_record('MX', '@', mx_content)
                    record = ensure_dns_record(
                        cf_client, zone_id, 'MX', '@',
                        mx_content, priority=i * 10,
                        existing_record_id=existing_id,
                        existing_content=existing_content,
                    )
                    records_status.append(record)

                # SPF record
                spf_content = f"v=spf1 {' '.join(f'ip4:{ip}' for ip in cluster_ips)} -all"
                # For TXT records on @, there can be multiple - find by content
                existing_id, existing_content = find_status_record('TXT', '@', spf_content)
                record = ensure_dns_record(
                    cf_client, zone_id, 'TXT', '@', spf_content,
                    existing_record_id=existing_id,
                    existing_content=existing_content,
                )
                records_status.append(record)

                # DMARC record
                dmarc_content = 'v=DMARC1; p=none;'
                existing_id, existing_content = find_status_record('TXT', '_dmarc', dmarc_content)
                record = ensure_dns_record(
                    cf_client, zone_id, 'TXT', '_dmarc', dmarc_content,
                    existing_record_id=existing_id,
                    existing_content=existing_content,
                )
                records_status.append(record)

                # DKIM record (if available)
                if dkim_dns_record:
                    existing_id, existing_content = find_status_record('TXT', 'default._domainkey', dkim_dns_record)
                    record = ensure_dns_record(
                        cf_client, zone_id, 'TXT', 'default._domainkey',
                        dkim_dns_record,
                        existing_record_id=existing_id,
                        existing_content=existing_content,
                    )
                    records_status.append(record)

        # Create manually specified records from spec
        for record_spec in dns_config.get('records', []):
            rec_type = record_spec.get('type')
            rec_name = record_spec.get('name')
            rec_content = record_spec.get('content')

            # Check if spec has recordId (from edit operation)
            # This takes precedence over status lookup
            spec_record_id = record_spec.get('recordId')
            if spec_record_id:
                # Use recordId from spec - this is an edit operation
                # Find the old content from status for comparison
                _, existing_content = find_status_record(rec_type, rec_name)
                existing_id = spec_record_id
            else:
                # No recordId in spec - try to find by (type, name, content)
                existing_id, existing_content = find_status_record(rec_type, rec_name, rec_content)

            record = ensure_dns_record(
                cf_client,
                zone_id,
                rec_type,
                rec_name,
                rec_content,
                ttl=record_spec.get('ttl', 1),
                proxied=record_spec.get('proxied', False),
                priority=record_spec.get('priority'),
                existing_record_id=existing_id,
                existing_content=existing_content,
            )
            records_status.append(record)

        # Update status
        status_patch['dns']['records'] = records_status
        status_patch['dns']['phase'] = 'Ready'
        status_patch['dns']['message'] = f"DNS configured with {len(records_status)} records"

        # Check if any records failed
        failed_records = [r for r in records_status if r.get('status') == 'Failed']
        if failed_records:
            conditions = set_condition(
                conditions, 'DNSReady', 'False', 'RecordsFailed',
                f"{len(failed_records)} DNS records failed to create"
            )
            status_patch['dns']['phase'] = 'Failed'
        else:
            conditions = set_condition(
                conditions, 'DNSReady', 'True', 'Configured',
                f"DNS configured with {len(records_status)} records"
            )

        logger.info(f"DNS reconciliation complete for '{domain_name}': {len(records_status)} records")

    except ApiException as e:
        logger.error(f"Failed to read Cloudflare credentials for '{domain_name}': {e}")
        conditions = set_condition(
            conditions, 'DNSReady', 'False', 'CredentialsError',
            f"Failed to read Cloudflare credentials: {e.reason}"
        )
        status_patch['dns']['phase'] = 'Failed'
        status_patch['dns']['message'] = f"Failed to read credentials: {e.reason}"

    except CloudflareAPIError as e:
        logger.error(f"Cloudflare API error for '{domain_name}': {e}")
        conditions = set_condition(
            conditions, 'DNSReady', 'False', 'CloudflareError',
            f"Cloudflare API error: {str(e)}"
        )
        status_patch['dns']['phase'] = 'Failed'
        status_patch['dns']['message'] = str(e)

    return conditions


def delete_dns_records(
    core_api: client.CoreV1Api,
    dns_config: dict,
    zone_id: str,
    records_status: list,
) -> None:
    """
    Delete DNS records from Cloudflare on domain deletion.

    Only deletes records that were tracked in the Domain CR status.
    """
    if not dns_config.get('enabled') or not zone_id:
        return

    credential_ref = dns_config.get('credentialSecretRef')
    if not credential_ref:
        return

    try:
        cf_client = get_cloudflare_client(core_api, credential_ref)

        for record in records_status or []:
            record_id = record.get('recordId')
            if record_id:
                try:
                    cf_client.dns.records.delete(zone_id, record_id)
                    logger.debug(f"Deleted DNS record '{record_id}'")
                except CloudflareAPIError as e:
                    logger.warning(f"Failed to delete DNS record '{record_id}': {e}")

        logger.info(f"Deleted {len(records_status or [])} DNS records")

    except Exception as e:
        logger.warning(f"Failed to delete DNS records: {e}")


def reconcile_domain(
    spec: dict,
    name: str,
    meta: dict,
    status: dict,
    patch: kopf.Patch
) -> dict:
    """
    Main reconciliation logic for a Domain.
    
    This is called by create, update, resume, and timer handlers.
    
    Returns:
        Dict with reconciliation result message
    """
    k8s = get_api_clients()
    
    domain_name = spec.get('domainName')
    namespace_name = sanitize_name(domain_name)
    owner = meta.get('labels', {}).get('kubepanel.io/owner', 'unknown')
    suspended = spec.get('suspended', False)
    
    # Get required fields (no defaults - CRD enforces these)
    resources = spec['resources']
    storage_size = resources['storage']
    cpu_limit = resources['limits']['cpu']
    memory_limit = resources['limits']['memory']
    
    # Get optional resource requests
    requests = resources.get('requests', {})
    cpu_request = requests.get('cpu', '32m')
    memory_request = requests.get('memory', '64Mi')
    
    # Get workload configuration (new format) or fall back to PHP (legacy)
    workload_config = spec.get('workload', {})
    if workload_config:
        # New workload format
        workload_type = workload_config.get('type', 'php')
        workload_version = workload_config.get('version', '8.2')
        workload_image = workload_config.get('image', '')
        workload_port = workload_config.get('port', 9001 if workload_type == 'php' else 9000)
        workload_proxy_mode = workload_config.get('proxyMode', 'fastcgi' if workload_type == 'php' else 'http')
        workload_command = workload_config.get('command')
        workload_args = workload_config.get('args')
        workload_env = workload_config.get('env')
        workload_settings = workload_config.get('settings', {})
        workload_custom_config = workload_config.get('customConfig', '')
    else:
        # Legacy PHP format for backward compatibility
        php_config = spec.get('php', {})
        workload_type = 'php'
        workload_version = php_config.get('version', '8.2')
        # For legacy format, construct image URL from version
        workload_image = f"docker.io/kubepanel/php{workload_version.replace('.', '')}:v1.0"
        workload_port = 9001
        workload_proxy_mode = 'fastcgi'
        workload_command = None
        workload_args = None
        workload_env = None
        workload_settings = php_config.get('settings', {})
        workload_custom_config = php_config.get('customConfig', '')

    # Get WordPress config
    wordpress_config = spec.get('wordpress', {})
    wp_preinstall = wordpress_config.get('preinstall', False)
    
    # Get optional fields with defaults
    # Parse aliases: support both legacy string format and new object format
    raw_aliases = spec.get('aliases', [])
    aliases = []           # List of alias domain name strings (for nginx/ingress)
    alias_email_configs = {}  # {alias_name: email_config_dict} for DKIM setup
    for alias_entry in raw_aliases:
        if isinstance(alias_entry, str):
            # Legacy string format
            aliases.append(alias_entry)
        elif isinstance(alias_entry, dict):
            alias_name = alias_entry.get('name', '')
            if alias_name:
                aliases.append(alias_name)
                alias_email = alias_entry.get('email', {})
                if alias_email.get('enabled'):
                    alias_email_configs[alias_name] = alias_email

    preferred_nodes = spec.get('preferredNodes', [])
    domain_timezone = spec.get('timezone', 'UTC')
    webserver_config = spec.get('webserver', {})
    ssl_redirect = webserver_config.get('sslRedirect', True)
    www_redirect = webserver_config.get('wwwRedirect', 'none')

    # Extract cache settings from webserver config
    cache_config = webserver_config.get('cache', {})
    cache_enabled = cache_config.get('enabled', False)
    cache_size = cache_config.get('size', '512Mi')

    email_config = spec.get('email', {})
    email_enabled = email_config.get('enabled', False)
    dkim_selector = email_config.get('dkimSelector', 'default')
    
    sftp_config = spec.get('sftp', {})
    sftp_enabled = sftp_config.get('enabled', True)
    sftp_type = sftp_config.get('type', 'standard')

    redis_config = spec.get('redis', {})
    redis_enabled = redis_config.get('enabled', False)

    database_config = spec.get('database', {})
    database_enabled = database_config.get('enabled', True)
    
    # Start with existing conditions or empty list
    conditions = list(status.get('conditions', [])) if status else []
    
    # Set basic status fields
    patch.status['namespace'] = namespace_name
    patch.status['observedGeneration'] = meta.get('generation', 1)
    
    # Handle suspended state
    if suspended:
        # Scale deployment to 0
        try:
            deployment = k8s.apps.read_namespaced_deployment(
                name='web',
                namespace=namespace_name
            )
            if deployment.spec.replicas != 0:
                logger.info(f"Scaling down deployment in '{namespace_name}' (suspended)")
                k8s.apps.patch_namespaced_deployment_scale(
                    name='web',
                    namespace=namespace_name,
                    body={'spec': {'replicas': 0}}
                )
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to scale down deployment: {e}")

        patch.status['phase'] = 'Suspended'
        patch.status['message'] = 'Domain is suspended (deployment scaled to 0)'
        logger.info(f"Domain '{name}' is suspended")
        return {'message': f"Domain {domain_name} is suspended"}
    
    # Reconcile resources in order
    try:
        # 1. Namespace (must exist before anything else)
        conditions = ensure_namespace(
            k8s.core, namespace_name, name, domain_name, owner, conditions
        )
        
        # 2. PVC (storage for the domain)
        conditions = ensure_pvc(
            k8s.core, namespace_name, name, storage_size, conditions
        )
        
        # 3. Secrets
        # 3a. SFTP secret (always create - needed for file access)
        if sftp_enabled:
            conditions = ensure_sftp_secret(
                k8s.core, namespace_name, name, conditions, patch.status
            )
        
        # 3b. Database secret and provisioning (if enabled)
        if database_enabled:
            conditions = ensure_database_secret(
                k8s.core, namespace_name, name, domain_name, conditions, patch.status
            )
            
            # Get the database credentials from status to provision the actual DB
            db_status = patch.status.get('database', {})
            if db_status:
                db_name = db_status.get('name')
                db_user = db_status.get('username')
                # Read password from secret
                try:
                    db_secret = k8s.core.read_namespaced_secret(
                        name='db-credentials',
                        namespace=namespace_name
                    )
                    db_password = base64.b64decode(
                        db_secret.data.get('password', '')
                    ).decode('utf-8')
                    
                    # Provision the actual database in MariaDB
                    conditions = ensure_database_provisioned(
                        k8s.core, db_name, db_user, db_password, conditions
                    )
                except ApiException as e:
                    if e.status != 404:
                        logger.error(f"Failed to read db-credentials secret: {e}")
        
        # 3c. DKIM secret (if email enabled for primary domain)
        if email_enabled:
            dkim_secret_ref = email_config.get('dkimSecretRef')
            conditions = ensure_dkim_secret(
                k8s.core, namespace_name, name, domain_name, dkim_selector,
                dkim_secret_ref, conditions, patch.status
            )

        # 3d. DKIM for email-enabled aliases
        for alias_domain_name, alias_email_cfg in alias_email_configs.items():
            alias_dkim_selector = alias_email_cfg.get('dkimSelector', 'default')
            alias_dkim_secret_ref = alias_email_cfg.get('dkimSecretRef')
            if alias_dkim_secret_ref:
                try:
                    # Reuse ensure_dkim_secret for each alias domain
                    # We pass alias_domain_name instead of domain_name so DKIM
                    # entries are created under the alias domain in central config
                    alias_conditions_dummy = []
                    alias_status_dummy = {}
                    ensure_dkim_secret(
                        k8s.core, namespace_name, name, alias_domain_name,
                        alias_dkim_selector, alias_dkim_secret_ref,
                        alias_conditions_dummy, alias_status_dummy
                    )
                    logger.info(f"Configured DKIM for alias '{alias_domain_name}' of domain '{domain_name}'")
                except Exception as e:
                    logger.error(f"Failed to set up DKIM for alias '{alias_domain_name}': {e}")

        # Check if DomainWAF exists and is enabled for this domain
        modsec_enabled = False
        try:
            domainwaf = k8s.custom.get_namespaced_custom_object(
                group=DOMAINWAF_GROUP,
                version=DOMAINWAF_VERSION,
                namespace=namespace_name,
                plural=DOMAINWAF_PLURAL,
                name='default'  # DomainWAF uses name 'default' by convention
            )
            modsec_enabled = domainwaf.get('spec', {}).get('enabled', True)
            logger.debug(f"DomainWAF found in '{namespace_name}', modsec_enabled={modsec_enabled}")
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to check DomainWAF in '{namespace_name}': {e}")
            # 404 means no DomainWAF, modsec_enabled remains False

        # 4a. ConfigMap (nginx config) - now with workload-aware proxy configuration
        conditions, nginx_configmap_updated = ensure_nginx_configmap(
            k8s.core, namespace_name, name, domain_name, aliases, webserver_config,
            conditions, workload_type=workload_type, workload_port=workload_port,
            proxy_mode=workload_proxy_mode, modsec_enabled=modsec_enabled
        )

        # 4b. ConfigMap (app config) - PHP ini or generic config based on workload type
        app_configmap_updated = ensure_app_configmap(
            k8s.core, namespace_name, name, workload_type, workload_settings,
            workload_custom_config
        )

        # Track if any ConfigMap changed (triggers deployment restart)
        configmap_updated = nginx_configmap_updated or app_configmap_updated

        # 5. Deployment (App + Nginx + SFTP + optional Redis) - now supports any workload type
        conditions = ensure_deployment(
            k8s.apps, namespace_name, name, domain_name,
            workload_type=workload_type,
            workload_version=workload_version,
            workload_image=workload_image,
            workload_port=workload_port,
            workload_command=workload_command,
            workload_args=workload_args,
            workload_env=workload_env,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            cpu_request=cpu_request,
            memory_request=memory_request,
            wp_preinstall=wp_preinstall,
            conditions=conditions,
            force_restart=configmap_updated,
            modsec_enabled=modsec_enabled,
            cache_enabled=cache_enabled,
            cache_size=cache_size,
            preferred_nodes=preferred_nodes,
            domain_timezone=domain_timezone,
            sftp_type=sftp_type,
            redis_enabled=redis_enabled,
        )
        
        # 6. Web Service (ClusterIP for Ingress)
        conditions = ensure_service(
            k8s.core, namespace_name, name, conditions
        )
        
        # 7. SFTP Service (NodePort for external access)
        if sftp_enabled:
            conditions = ensure_sftp_service(
                k8s.core, namespace_name, name, conditions, patch.status
            )
        
        # 8. Ingress (HTTP/HTTPS access with TLS)
        conditions = ensure_ingress(
            k8s.networking, namespace_name, name, domain_name,
            aliases, ssl_redirect, www_redirect, conditions, patch.status
        )

        # 9. Backup resources (PVC, ServiceAccount, RoleBinding, credentials)
        backup_storage_size = spec.get('backup', {}).get('storageSize', '10Gi')
        conditions = ensure_backup_pvc(
            k8s.core, namespace_name, name, backup_storage_size, conditions
        )
        conditions = ensure_backup_service_account(
            k8s.core, namespace_name, name, conditions
        )
        conditions = ensure_backup_role_binding(
            k8s.rbac, namespace_name, name, conditions
        )
        conditions = ensure_backup_credentials_secret(
            k8s.core, namespace_name, name, conditions
        )

        # 10. DNS Management (Cloudflare)
        dns_config = spec.get('dns', {})
        if dns_config.get('enabled'):
            # Get DKIM DNS record from email status if available
            dkim_dns_record = patch.status.get('email', {}).get('dkimDnsRecord', '')
            conditions = ensure_dns(
                k8s.core, domain_name, dns_config, dkim_dns_record, conditions, patch.status,
                current_status=status
            )

    except ApiException as e:
        logger.error(f"Reconciliation failed for '{name}': {e}")
        patch.status['phase'] = 'Failed'
        patch.status['message'] = f"Reconciliation failed: {e.reason}"
        patch.status['conditions'] = conditions
        raise kopf.TemporaryError(f"Reconciliation failed: {e}", delay=60)
    
    # Determine overall phase from conditions
    phase, message = determine_overall_phase(conditions, suspended)
    patch.status['phase'] = phase
    patch.status['message'] = message
    patch.status['conditions'] = conditions
    
    logger.info(f"Domain '{name}' reconciliation complete: {phase}")
    return {'message': f"Domain {domain_name}: {message}"}


# =============================================================================
# Kopf Handlers
# =============================================================================

@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Configure operator settings."""
    settings.posting.level = logging.WARNING
    settings.watching.connect_timeout = 60
    settings.watching.server_timeout = 300
    logger.info("KubePanel operator starting...")


@kopf.on.create(DOMAIN_GROUP, DOMAIN_VERSION, DOMAIN_PLURAL)
def on_domain_create(spec, name, meta, status, patch, **kwargs):
    """Handle Domain CR creation."""
    logger.info(f"Domain '{name}' created, starting reconciliation...")
    patch.status['phase'] = 'Provisioning'
    patch.status['message'] = 'Creating resources...'
    return reconcile_domain(spec, name, meta, status, patch)


@kopf.on.update(DOMAIN_GROUP, DOMAIN_VERSION, DOMAIN_PLURAL)
def on_domain_update(spec, name, meta, status, patch, **kwargs):
    """Handle Domain CR updates."""
    logger.info(f"Domain '{name}' updated, reconciling changes...")
    return reconcile_domain(spec, name, meta, status, patch)


@kopf.on.delete(DOMAIN_GROUP, DOMAIN_VERSION, DOMAIN_PLURAL)
def on_domain_delete(spec, name, meta, **kwargs):
    """Handle Domain CR deletion."""
    logger.info(f"Domain '{name}' being deleted, cleaning up...")
    
    k8s = get_api_clients()
    domain_name = spec.get('domainName')
    namespace_name = sanitize_name(domain_name)
    
    # Check if database was enabled and clean it up
    database_config = spec.get('database', {})
    database_enabled = database_config.get('enabled', True)
    
    if database_enabled:
        # Derive database name and user from domain (same logic as in build_database_secret)
        db_name = domain_name.replace('.', '_').replace('-', '_')[:32]
        db_user = db_name[:32]

        # Delete database and user from MariaDB
        if delete_database_and_user(k8s.core, db_name, db_user):
            logger.info(f"Cleaned up database '{db_name}' and user '{db_user}'")
        else:
            logger.warning(f"Failed to clean up database '{db_name}' - may need manual cleanup")

    # Clean up DKIM config and collect mailbox names for cleanup
    email_config = spec.get('email', {})
    mailbox_names = []  # Collect all mailbox directories to clean up

    if email_config.get('enabled', False):
        remove_domain_from_central_dkim(k8s.core, domain_name)
        logger.info(f"Cleaned up DKIM config for '{domain_name}'")
        mailbox_names.append(domain_name)  # Add main domain

    # Clean up DKIM for email-enabled aliases
    raw_aliases = spec.get('aliases', [])
    for alias_entry in raw_aliases:
        if isinstance(alias_entry, dict):
            alias_email = alias_entry.get('email', {})
            if alias_email.get('enabled'):
                alias_name = alias_entry.get('name', '')
                if alias_name:
                    try:
                        remove_domain_from_central_dkim(k8s.core, alias_name)
                        logger.info(f"Cleaned up DKIM config for alias '{alias_name}'")
                    except Exception as e:
                        logger.warning(f"Failed to clean up DKIM for alias '{alias_name}': {e}")
                    mailbox_names.append(alias_name)  # Add alias mailbox

    # Clean up mailbox files on SMTP PVC (one job for domain + all aliases)
    if mailbox_names:
        try:
            batch_api = client.BatchV1Api()
            cleanup_job = build_mailbox_cleanup_job(domain_name, mailbox_names)
            batch_api.create_namespaced_job(
                namespace=DKIM_NAMESPACE,
                body=cleanup_job,
            )
            logger.info(f"Created mailbox cleanup job for: {mailbox_names}")
        except ApiException as e:
            if e.status == 409:
                logger.info(f"Mailbox cleanup job already exists for '{domain_name}'")
            else:
                logger.warning(f"Failed to create mailbox cleanup job: {e}")
                # Non-fatal - continue with deletion

    # Clean up DNS records if DNS was enabled
    dns_config = spec.get('dns', {})
    if dns_config.get('enabled', False):
        # Get zone ID and records from status
        dns_status = (kwargs.get('status') or {}).get('dns', {})
        zone_id = dns_status.get('zone', {}).get('id')
        records_status = dns_status.get('records', [])
        delete_dns_records(k8s.core, dns_config, zone_id, records_status)
        logger.info(f"Cleaned up DNS records for '{domain_name}'")

    # Delete namespace (cascades to all resources within)
    try:
        k8s.core.delete_namespace(
            name=namespace_name,
            body=client.V1DeleteOptions(propagation_policy='Foreground')
        )
        logger.info(f"Deleted namespace '{namespace_name}'")
    except ApiException as e:
        if e.status == 404:
            logger.info(f"Namespace '{namespace_name}' already deleted")
        else:
            logger.error(f"Failed to delete namespace: {e}")
            raise kopf.TemporaryError(f"Failed to delete namespace: {e}", delay=30)
    
    logger.info(f"Domain '{name}' cleanup complete")
    return {'message': f"Domain {domain_name} deleted"}


@kopf.on.resume(DOMAIN_GROUP, DOMAIN_VERSION, DOMAIN_PLURAL)
def on_domain_resume(spec, name, meta, status, patch, **kwargs):
    """Handle operator restart - reconcile existing domains."""
    logger.info(f"Resuming Domain '{name}'...")

    # Skip full reconciliation if domain is already Ready
    # This prevents unnecessary deployment restarts on operator restart
    current_phase = status.get('phase') if status else None
    if current_phase == 'Ready':
        k8s = get_api_clients()
        namespace_name = sanitize_name(spec.get('domainName'))

        try:
            # Quick health check: verify namespace and deployment exist
            k8s.core.read_namespace(name=namespace_name)
            k8s.apps.read_namespaced_deployment(name='web', namespace=namespace_name)
            logger.info(f"Domain '{name}' is Ready, skipping full reconciliation")
            return {'message': f"Domain {spec.get('domainName')} resumed (already Ready)"}
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Resource missing for '{name}', triggering full reconciliation")
            else:
                raise

    # Full reconciliation for non-Ready domains or missing resources
    return reconcile_domain(spec, name, meta, status, patch)


@kopf.timer(DOMAIN_GROUP, DOMAIN_VERSION, DOMAIN_PLURAL, interval=300, initial_delay=300)
def on_domain_timer(spec, name, meta, status, patch, **kwargs):
    """Periodic reconciliation every 5 minutes (first run delayed to avoid overlap with resume)."""
    logger.debug(f"Periodic reconciliation for Domain '{name}'")
    
    # Skip full reconciliation if everything looks healthy
    current_phase = status.get('phase') if status else None
    if current_phase == 'Ready':
        # Quick health check only
        k8s = get_api_clients()
        namespace_name = sanitize_name(spec.get('domainName'))
        
        try:
            k8s.core.read_namespace(name=namespace_name)
            # Namespace exists, skip full reconciliation
            return
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Namespace '{namespace_name}' missing, triggering reconciliation")
            else:
                raise
    
    # Full reconciliation needed
    return reconcile_domain(spec, name, meta, status, patch)


# =============================================================================
# Backup Handlers
# =============================================================================

def build_backup_job(
    backup_name: str,
    namespace: str,
    domain_name: str,
    db_name: str,
) -> client.V1Job:
    """
    Build a Kubernetes Job to run a backup.

    The Job runs the backup container which:
    1. Creates a VolumeSnapshot for filesystem backup
    2. Runs mariabackup for database backup
    3. Updates the Backup CR status
    """
    job_name = f"backup-{backup_name}"

    # Labels
    labels = {
        'kubepanel.io/backup': backup_name,
        'kubepanel.io/domain': domain_name.replace('.', '-'),
        'app.kubernetes.io/managed-by': 'kubepanel-operator',
        'app.kubernetes.io/component': 'backup',
    }

    # Environment variables for the backup script
    env = [
        client.V1EnvVar(name='BACKUP_NAME', value=backup_name),
        client.V1EnvVar(name='NAMESPACE', value=namespace),
        client.V1EnvVar(name='DOMAIN_NAME', value=domain_name),
        client.V1EnvVar(name='DB_NAME', value=db_name),
        client.V1EnvVar(name='DB_HOST', value=MARIADB_HOST),
        client.V1EnvVar(
            name='MARIADB_ROOT_PASSWORD',
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name='backup-credentials',  # Local secret in domain namespace
                    key='mariadb-root-password',
                )
            )
        ),
    ]

    # Container
    container = client.V1Container(
        name='backup',
        image=BACKUP_IMAGE,
        image_pull_policy='Always',
        env=env,
        volume_mounts=[
            client.V1VolumeMount(
                name='backup-storage',
                mount_path='/backup',
            ),
        ],
    )

    # Volumes - mount the domain's backup PVC
    volumes = [
        client.V1Volume(
            name='backup-storage',
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name='backup',
            ),
        ),
    ]

    # Pod spec
    pod_spec = client.V1PodSpec(
        containers=[container],
        volumes=volumes,
        restart_policy='Never',
        service_account_name=BACKUP_SERVICE_ACCOUNT,
    )

    # Job spec
    job_spec = client.V1JobSpec(
        template=client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels=labels),
            spec=pod_spec,
        ),
        backoff_limit=1,
        ttl_seconds_after_finished=86400,  # Clean up after 24 hours
    )

    return client.V1Job(
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=namespace,
            labels=labels,
        ),
        spec=job_spec,
    )


def build_mailbox_cleanup_job(domain_name: str, mailbox_names: list) -> client.V1Job:
    """
    Build a Kubernetes Job to clean up mailbox files for a deleted domain.

    Runs in kubepanel namespace, mounts smtp-pvc, deletes maildirs for
    the domain and all its email-enabled aliases.

    Args:
        domain_name: Primary domain name (used for job naming)
        mailbox_names: List of all mailbox directories to delete (domain + aliases)
    """
    job_name = f"mailbox-cleanup-{sanitize_name(domain_name)}"

    labels = {
        'kubepanel.io/domain': domain_name.replace('.', '-'),
        'app.kubernetes.io/managed-by': 'kubepanel-operator',
        'app.kubernetes.io/component': 'mailbox-cleanup',
    }

    # Build rm command for all mailbox directories
    rm_paths = ' '.join([f'/var/mail/vmail/{name}' for name in mailbox_names])
    cleanup_command = f'rm -rf {rm_paths} && echo "Deleted mailboxes: {", ".join(mailbox_names)}"'

    # Use alpine for minimal image with rm command
    container = client.V1Container(
        name='cleanup',
        image='alpine:3.19',
        command=['sh', '-c', cleanup_command],
        volume_mounts=[
            client.V1VolumeMount(
                name='mail-storage',
                mount_path='/var/mail/vmail',
            ),
        ],
    )

    volumes = [
        client.V1Volume(
            name='mail-storage',
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name='smtp-pvc',
            ),
        ),
    ]

    # Pod affinity to run on the same node as the SMTP pod (required for RWO PVC)
    affinity = client.V1Affinity(
        pod_affinity=client.V1PodAffinity(
            required_during_scheduling_ignored_during_execution=[
                client.V1PodAffinityTerm(
                    label_selector=client.V1LabelSelector(
                        match_labels={'app': 'smtp'}
                    ),
                    topology_key='kubernetes.io/hostname',
                )
            ]
        )
    )

    pod_spec = client.V1PodSpec(
        containers=[container],
        volumes=volumes,
        restart_policy='Never',
        affinity=affinity,
    )

    job_spec = client.V1JobSpec(
        template=client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels=labels),
            spec=pod_spec,
        ),
        backoff_limit=1,
        ttl_seconds_after_finished=300,  # Clean up job after 5 minutes
    )

    return client.V1Job(
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=DKIM_NAMESPACE,  # kubepanel namespace
            labels=labels,
        ),
        spec=job_spec,
    )


@kopf.on.create(BACKUP_GROUP, BACKUP_VERSION, BACKUP_PLURAL)
def on_backup_create(spec, name, namespace, meta, status, patch, **kwargs):
    """
    Handle Backup CR creation.

    When a Backup CR is created, we create a Job to run the backup.
    """
    logger.info(f"Backup '{name}' created in namespace '{namespace}'")

    domain_name = spec.get('domainName')
    if not domain_name:
        patch.status['phase'] = 'Failed'
        patch.status['message'] = 'Missing required field: domainName'
        logger.error(f"Backup '{name}' missing domainName")
        return {'message': 'Missing domainName'}

    # Derive database name from domain name (same logic as in resources.py)
    db_name = domain_name.replace('.', '_').replace('-', '_')[:32]

    # Set initial status
    patch.status['phase'] = 'Pending'
    patch.status['message'] = 'Creating backup job'

    # Get API clients
    k8s = get_api_clients()
    batch_api = client.BatchV1Api()

    # Build and create the backup job
    job = build_backup_job(
        backup_name=name,
        namespace=namespace,
        domain_name=domain_name,
        db_name=db_name,
    )

    try:
        batch_api.create_namespaced_job(
            namespace=namespace,
            body=job,
        )
        logger.info(f"Created backup job for '{name}' in namespace '{namespace}'")

        # The job will update the Backup CR status when it runs
        return {'message': f"Backup job created for {domain_name}"}

    except ApiException as e:
        if e.status == 409:
            # Job already exists
            logger.info(f"Backup job already exists for '{name}'")
            return {'message': 'Backup job already exists'}
        else:
            logger.error(f"Failed to create backup job: {e}")
            patch.status['phase'] = 'Failed'
            patch.status['message'] = f'Failed to create backup job: {e.reason}'
            raise kopf.PermanentError(f"Failed to create backup job: {e}")


@kopf.on.delete(BACKUP_GROUP, BACKUP_VERSION, BACKUP_PLURAL)
def on_backup_delete(spec, name, namespace, meta, status, **kwargs):
    """
    Handle Backup CR deletion.

    When a Backup CR is deleted, we clean up associated resources:
    1. Delete the VolumeSnapshot
    2. Delete the backup files from the backup PVC
    3. Delete the backup job (if still exists)
    """
    logger.info(f"Backup '{name}' being deleted from namespace '{namespace}'")

    k8s = get_api_clients()
    batch_api = client.BatchV1Api()

    # Delete the backup job if it exists
    job_name = f"backup-{name}"
    try:
        batch_api.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            body=client.V1DeleteOptions(propagation_policy='Background'),
        )
        logger.info(f"Deleted backup job '{job_name}'")
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete backup job '{job_name}': {e}")

    # Delete the VolumeSnapshot if it exists
    volume_snapshot_name = status.get('volumeSnapshotName') if status else None
    if volume_snapshot_name:
        try:
            k8s.custom.delete_namespaced_custom_object(
                group='snapshot.storage.k8s.io',
                version='v1',
                namespace=namespace,
                plural='volumesnapshots',
                name=volume_snapshot_name,
            )
            logger.info(f"Deleted VolumeSnapshot '{volume_snapshot_name}'")
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete VolumeSnapshot '{volume_snapshot_name}': {e}")

    # Note: Backup files on the backup PVC are cleaned up by the cleanup CronJob
    # based on retention policy (retentionExpiresAt field)

    logger.info(f"Backup '{name}' cleanup complete")
    return {'message': f"Backup {name} deleted"}


# =============================================================================
# Restore Handlers
# =============================================================================

def build_restore_job(
    restore_name: str,
    namespace: str,
    domain_name: str,
    backup_name: str,
    volume_snapshot_name: str,
    database_backup_path: str,
    db_name: str,
    storage_size: str,
) -> client.V1Job:
    """
    Build a Kubernetes Job to run a restore.

    The Job runs the restore container which:
    1. Scales down the domain deployment
    2. Deletes the data PVC and recreates it from VolumeSnapshot
    3. Restores the database from the dump file
    4. Scales up the domain deployment
    5. Updates the Restore CR status
    """
    job_name = f"restore-{restore_name}"

    # Labels
    labels = {
        'kubepanel.io/restore': restore_name,
        'kubepanel.io/domain': domain_name.replace('.', '-'),
        'app.kubernetes.io/managed-by': 'kubepanel-operator',
        'app.kubernetes.io/component': 'restore',
    }

    # Environment variables for the restore script
    env = [
        client.V1EnvVar(name='RESTORE_NAME', value=restore_name),
        client.V1EnvVar(name='NAMESPACE', value=namespace),
        client.V1EnvVar(name='DOMAIN_NAME', value=domain_name),
        client.V1EnvVar(name='BACKUP_NAME', value=backup_name),
        client.V1EnvVar(name='VOLUME_SNAPSHOT_NAME', value=volume_snapshot_name),
        client.V1EnvVar(name='DATABASE_BACKUP_PATH', value=database_backup_path),
        client.V1EnvVar(name='DB_NAME', value=db_name),
        client.V1EnvVar(name='DB_HOST', value=MARIADB_HOST),
        client.V1EnvVar(name='STORAGE_SIZE', value=storage_size),
        client.V1EnvVar(
            name='MARIADB_ROOT_PASSWORD',
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name='backup-credentials',
                    key='mariadb-root-password',
                )
            )
        ),
    ]

    # Container - only mounts backup PVC (data PVC is deleted and recreated)
    container = client.V1Container(
        name='restore',
        image=RESTORE_IMAGE,
        image_pull_policy='Always',
        env=env,
        volume_mounts=[
            client.V1VolumeMount(
                name='backup-storage',
                mount_path='/backup',
            ),
        ],
    )

    # Volumes - only backup PVC needed
    volumes = [
        client.V1Volume(
            name='backup-storage',
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name='backup',
            ),
        ),
    ]

    # Pod spec
    pod_spec = client.V1PodSpec(
        containers=[container],
        volumes=volumes,
        restart_policy='Never',
        service_account_name=BACKUP_SERVICE_ACCOUNT,
    )

    # Job spec
    job_spec = client.V1JobSpec(
        template=client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels=labels),
            spec=pod_spec,
        ),
        backoff_limit=1,
        ttl_seconds_after_finished=3600,  # Keep completed job for 1 hour
    )

    # Job
    job = client.V1Job(
        api_version='batch/v1',
        kind='Job',
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=namespace,
            labels=labels,
        ),
        spec=job_spec,
    )

    return job


def build_uploaded_restore_job(
    restore_name: str,
    namespace: str,
    domain_name: str,
    uploaded_archive_path: str,
    db_name: str,
) -> client.V1Job:
    """
    Build a Kubernetes Job to restore from an uploaded tar.gz archive.

    The Job runs a shell script which:
    1. Scales down the domain deployment
    2. Extracts html/ from archive to data PVC
    3. Imports database.sql to MariaDB
    4. Scales up the domain deployment
    5. Updates the Restore CR status
    """
    job_name = f"restore-{restore_name}"
    cr_name = domain_name.replace('.', '-')

    # Labels
    labels = {
        'kubepanel.io/restore': restore_name,
        'kubepanel.io/domain': cr_name,
        'app.kubernetes.io/managed-by': 'kubepanel-operator',
        'app.kubernetes.io/component': 'restore',
    }

    # Restore script that runs inside the container
    restore_script = f'''
#!/bin/sh
set -e

NAMESPACE="{namespace}"
DOMAIN_NAME="{domain_name}"
CR_NAME="{cr_name}"
ARCHIVE_PATH="{uploaded_archive_path}"
DB_NAME="{db_name}"
RESTORE_NAME="{restore_name}"

echo "Starting uploaded restore for $DOMAIN_NAME"

# Update status to Running
kubectl patch restore "$RESTORE_NAME" -n "$NAMESPACE" --type=merge --subresource=status -p '{{"status":{{"phase":"Running","message":"Restore in progress","startedAt":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}}}}'

# Scale down deployment (deployment name is always 'web' in KubePanel)
echo "Scaling down deployment..."
kubectl scale deployment web -n "$NAMESPACE" --replicas=0
sleep 5

# Wait for pods to terminate
echo "Waiting for pods to terminate..."
kubectl wait --for=delete pod -l app=web -n "$NAMESPACE" --timeout=120s 2>/dev/null || true

# Extract archive to data PVC
echo "Extracting archive from $ARCHIVE_PATH..."
cd /tmp
tar xzf "$ARCHIVE_PATH"

# Copy website files to data PVC (mounted at /data)
# Support both html/ (KubePanel format) and www/ (legacy format)
if [ -d "html" ]; then
    echo "Copying website files from html/..."
    rm -rf /data/*
    cp -a html/. /data/
    chown -R 7777:7777 /data/
    echo "Website files restored from html/"
elif [ -d "www" ]; then
    echo "Copying website files from www/..."
    rm -rf /data/*
    cp -a www/. /data/
    chown -R 7777:7777 /data/
    echo "Website files restored from www/"
else
    echo "WARNING: No html/ or www/ directory found in archive"
fi

# Import database if exists
if [ -f "database.sql" ]; then
    echo "Importing database..."
    mysql -h "$DB_HOST" -u root -p"$MARIADB_ROOT_PASSWORD" "$DB_NAME" < database.sql
    echo "Database imported"
else
    echo "WARNING: No database.sql found in archive"
fi

# Scale up deployment
echo "Scaling up deployment..."
kubectl scale deployment web -n "$NAMESPACE" --replicas=1

# Wait for deployment to be ready
echo "Waiting for deployment to be ready..."
kubectl rollout status deployment web -n "$NAMESPACE" --timeout=300s

# Update status to Completed
kubectl patch restore "$RESTORE_NAME" -n "$NAMESPACE" --type=merge --subresource=status -p '{{"status":{{"phase":"Completed","message":"Restore completed successfully","completedAt":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}}}}'

echo "Restore completed successfully"
'''

    # Environment variables
    env = [
        client.V1EnvVar(name='DB_HOST', value=MARIADB_HOST),
        client.V1EnvVar(
            name='MARIADB_ROOT_PASSWORD',
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name='backup-credentials',
                    key='mariadb-root-password',
                )
            )
        ),
    ]

    # Container with kubectl and mysql client
    container = client.V1Container(
        name='restore',
        image='docker.io/kubepanel/backup:v1.0',  # Has kubectl and mysql
        image_pull_policy='Always',
        command=['sh', '-c', restore_script],
        env=env,
        volume_mounts=[
            client.V1VolumeMount(
                name='backup-storage',
                mount_path='/backup',
                read_only=True,
            ),
            client.V1VolumeMount(
                name='data-storage',
                mount_path='/data',
            ),
        ],
    )

    # Volumes
    volumes = [
        client.V1Volume(
            name='backup-storage',
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name='backup',
            ),
        ),
        client.V1Volume(
            name='data-storage',
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name='data',
            ),
        ),
    ]

    # Pod affinity - schedule on the same node as the domain's web deployment
    # This is required because the data PVC (Linstor ReadWriteOnce) can only be
    # mounted on the node where it's currently bound
    affinity = client.V1Affinity(
        pod_affinity=client.V1PodAffinity(
            required_during_scheduling_ignored_during_execution=[
                client.V1PodAffinityTerm(
                    label_selector=client.V1LabelSelector(
                        match_labels={
                            'app.kubernetes.io/name': 'web',
                            'kubepanel.io/domain': cr_name,
                        }
                    ),
                    topology_key='kubernetes.io/hostname',
                )
            ]
        )
    )

    # Pod spec
    pod_spec = client.V1PodSpec(
        containers=[container],
        volumes=volumes,
        restart_policy='Never',
        service_account_name=BACKUP_SERVICE_ACCOUNT,
        affinity=affinity,
    )

    # Job spec
    job_spec = client.V1JobSpec(
        template=client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels=labels),
            spec=pod_spec,
        ),
        backoff_limit=1,
        ttl_seconds_after_finished=3600,
    )

    # Job
    job = client.V1Job(
        api_version='batch/v1',
        kind='Job',
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=namespace,
            labels=labels,
        ),
        spec=job_spec,
    )

    return job


@kopf.on.create('kubepanel.io', 'v1alpha1', 'restores')
def on_restore_create(spec, name, namespace, meta, status, patch, **kwargs):
    """
    Handle Restore CR creation.

    When a Restore CR is created, we create a Job to run the restore.
    Supports two restore types:
    - snapshot: Restore from VolumeSnapshot (requires volumeSnapshotName, databaseBackupPath)
    - uploaded: Restore from uploaded tar.gz archive (requires uploadedArchivePath)
    """
    logger.info(f"Restore '{name}' created in namespace '{namespace}'")

    domain_name = spec.get('domainName')
    restore_type = spec.get('restoreType', 'snapshot')

    if not domain_name:
        patch.status['phase'] = 'Failed'
        patch.status['message'] = 'Missing required field: domainName'
        logger.error(f"Restore '{name}' missing domainName")
        return {'message': 'Missing domainName'}

    # Derive database name from domain name
    db_name = domain_name.replace('.', '_').replace('-', '_')[:32]

    # Get storage size from Domain CR (cluster-scoped)
    k8s = get_api_clients()
    cr_name = domain_name.replace('.', '-')
    try:
        domain_cr = k8s.custom.get_cluster_custom_object(
            group=DOMAIN_GROUP,
            version=DOMAIN_VERSION,
            plural=DOMAIN_PLURAL,
            name=cr_name,
        )
        storage_size = domain_cr.get('spec', {}).get('resources', {}).get('storage', '5Gi')
    except ApiException as e:
        logger.warning(f"Failed to get Domain CR for storage size, using default: {e}")
        storage_size = '5Gi'

    # Get API client
    batch_api = client.BatchV1Api()

    if restore_type == 'uploaded':
        # Uploaded archive restore
        uploaded_archive_path = spec.get('uploadedArchivePath')

        if not uploaded_archive_path:
            patch.status['phase'] = 'Failed'
            patch.status['message'] = 'Missing uploadedArchivePath for uploaded restore'
            logger.error(f"Restore '{name}' missing uploadedArchivePath")
            return {'message': 'Missing uploadedArchivePath'}

        logger.info(f"Creating uploaded restore job for '{name}' from archive: {uploaded_archive_path}")

        # Set initial status
        patch.status['phase'] = 'Pending'
        patch.status['message'] = 'Creating uploaded restore job'

        # Build uploaded restore job
        job = build_uploaded_restore_job(
            restore_name=name,
            namespace=namespace,
            domain_name=domain_name,
            uploaded_archive_path=uploaded_archive_path,
            db_name=db_name,
        )
    else:
        # Snapshot restore (existing logic)
        backup_name = spec.get('backupName')
        volume_snapshot_name = spec.get('volumeSnapshotName')
        database_backup_path = spec.get('databaseBackupPath')

        if not backup_name:
            patch.status['phase'] = 'Failed'
            patch.status['message'] = 'Missing required field: backupName'
            logger.error(f"Restore '{name}' missing backupName")
            return {'message': 'Missing backupName'}

        if not volume_snapshot_name:
            patch.status['phase'] = 'Failed'
            patch.status['message'] = 'Missing volumeSnapshotName for snapshot restore'
            logger.error(f"Restore '{name}' missing volumeSnapshotName")
            return {'message': 'Missing volumeSnapshotName'}

        if not database_backup_path:
            patch.status['phase'] = 'Failed'
            patch.status['message'] = 'Missing databaseBackupPath for snapshot restore'
            logger.error(f"Restore '{name}' missing databaseBackupPath")
            return {'message': 'Missing databaseBackupPath'}

        # Set initial status
        patch.status['phase'] = 'Pending'
        patch.status['message'] = 'Creating restore job'

        # Build snapshot restore job
        job = build_restore_job(
            restore_name=name,
            namespace=namespace,
            domain_name=domain_name,
            backup_name=backup_name,
            volume_snapshot_name=volume_snapshot_name,
            database_backup_path=database_backup_path,
            db_name=db_name,
            storage_size=storage_size,
        )

    try:
        batch_api.create_namespaced_job(
            namespace=namespace,
            body=job,
        )
        logger.info(f"Created restore job for '{name}' in namespace '{namespace}'")

        return {'message': f"Restore job created for {domain_name}"}

    except ApiException as e:
        if e.status == 409:
            logger.info(f"Restore job already exists for '{name}'")
            return {'message': 'Restore job already exists'}
        else:
            logger.error(f"Failed to create restore job: {e}")
            patch.status['phase'] = 'Failed'
            patch.status['message'] = f'Failed to create restore job: {e.reason}'
            raise kopf.PermanentError(f"Failed to create restore job: {e}")


@kopf.on.delete('kubepanel.io', 'v1alpha1', 'restores')
def on_restore_delete(spec, name, namespace, meta, status, **kwargs):
    """
    Handle Restore CR deletion.

    Cleanup associated job if it still exists.
    """
    logger.info(f"Restore '{name}' being deleted from namespace '{namespace}'")

    batch_api = client.BatchV1Api()

    # Delete the restore job if it exists
    job_name = f"restore-{name}"
    try:
        batch_api.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            body=client.V1DeleteOptions(propagation_policy='Background'),
        )
        logger.info(f"Deleted restore job '{job_name}'")
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete restore job '{job_name}': {e}")

    logger.info(f"Restore '{name}' cleanup complete")
    return {'message': f"Restore {name} deleted"}


# =============================================================================
# DNSZone CR Management
# =============================================================================

# DNSZone CRD constants
DNSZONE_GROUP = 'kubepanel.io'
DNSZONE_VERSION = 'v1alpha1'
DNSZONE_PLURAL = 'dnszones'


def reconcile_dnszone(spec, name, status, patch, **kwargs):
    """
    Reconcile DNSZone CR - sync all records from spec to Cloudflare.

    The operator is intentionally simple:
    1. Records without recordId -> create in CF, write recordId back to spec
    2. Records with recordId where content differs from status -> update in CF
    3. recordIds in status but not in spec -> delete from CF
    """
    zone_name = spec.get('zoneName')
    credential_ref = spec.get('credentialSecretRef')
    spec_records = spec.get('records', [])

    if not credential_ref:
        logger.warning(f"DNSZone '{name}' has no credentialSecretRef")
        patch.status['phase'] = 'Failed'
        patch.status['message'] = 'Missing credentialSecretRef'
        return {'message': 'Missing credentialSecretRef'}

    k8s = get_api_clients()

    # Get current status
    current_status = status or {}
    current_zone_id = current_status.get('zoneId')
    status_records = current_status.get('records', [])

    # Build lookup from status: recordId -> status record
    status_by_id = {r.get('recordId'): r for r in status_records if r.get('recordId')}

    try:
        # Get Cloudflare client
        cf_client = get_cloudflare_client(k8s.core, credential_ref)

        # Get or create zone
        zone_id = current_zone_id
        zone_obj = None  # Store zone object for nameservers
        if not zone_id:
            logger.info(f"Looking for zone '{zone_name}' in Cloudflare...")
            zones = cf_client.zones.list(name=zone_name)

            if zones.result:
                zone_obj = zones.result[0]
                zone_id = zone_obj.id if hasattr(zone_obj, 'id') else zone_obj['id']
                logger.info(f"Found existing zone '{zone_name}' with ID '{zone_id}'")
            else:
                # Try to create the zone
                logger.info(f"Zone '{zone_name}' not found, creating...")
                try:
                    accounts = cf_client.accounts.list()
                    if not accounts.result:
                        patch.status['phase'] = 'Failed'
                        patch.status['message'] = 'No Cloudflare account found'
                        return {'error': 'No Cloudflare account found'}

                    first_account = accounts.result[0]
                    account_id = first_account.id if hasattr(first_account, 'id') else first_account['id']

                    zone_obj = cf_client.zones.create(
                        name=zone_name,
                        account={"id": account_id},
                        type="full"
                    )
                    zone_id = zone_obj.id if hasattr(zone_obj, 'id') else zone_obj['id']
                    logger.info(f"Created zone '{zone_name}' with ID '{zone_id}'")

                except CloudflareAPIError as e:
                    logger.error(f"Failed to create zone '{zone_name}': {e}")
                    patch.status['phase'] = 'Failed'
                    patch.status['message'] = f"Failed to create zone: {str(e)}"
                    return {'error': str(e)}

        patch.status['zoneId'] = zone_id
        patch.status['phase'] = 'Syncing'

        # Extract nameservers from zone object
        # If zone_obj is None (we had zone_id from status), fetch zone to get nameservers
        current_nameservers = current_status.get('nameservers', [])
        if not current_nameservers:
            if zone_obj:
                nameservers = getattr(zone_obj, 'name_servers', None)
                if nameservers:
                    patch.status['nameservers'] = list(nameservers)
                    logger.info(f"[DNSZone:{name}] Nameservers: {nameservers}")
            elif zone_id:
                # Fetch zone details to get nameservers
                try:
                    zone_details = cf_client.zones.get(zone_id)
                    nameservers = getattr(zone_details, 'name_servers', None)
                    if nameservers:
                        patch.status['nameservers'] = list(nameservers)
                        logger.info(f"[DNSZone:{name}] Fetched nameservers: {nameservers}")
                except Exception as ns_err:
                    logger.warning(f"[DNSZone:{name}] Failed to fetch nameservers: {ns_err}")

        # Auto-import records from CloudFlare if spec.records is empty
        # This is a one-time import when connecting to an existing zone
        if zone_id and not spec_records:
            logger.info(f"[DNSZone:{name}] spec.records is empty, importing existing records from CloudFlare zone {zone_id}")
            try:
                existing_cf_records = list(cf_client.dns.records.list(zone_id=zone_id))

                imported_records = []
                for rec in existing_cf_records:
                    rec_type = rec.type if hasattr(rec, 'type') else rec.get('type')
                    rec_name = rec.name if hasattr(rec, 'name') else rec.get('name')
                    rec_content = rec.content if hasattr(rec, 'content') else rec.get('content')
                    rec_id = rec.id if hasattr(rec, 'id') else rec.get('id')
                    rec_ttl = rec.ttl if hasattr(rec, 'ttl') else rec.get('ttl', 1)
                    rec_proxied = getattr(rec, 'proxied', False) if hasattr(rec, 'proxied') else rec.get('proxied', False)

                    # Skip NS records for the root domain (managed by CF)
                    if rec_type == 'NS' and rec_name == zone_name:
                        continue
                    # Skip SOA records (managed by CF)
                    if rec_type == 'SOA':
                        continue

                    # Normalize record name: remove zone suffix, use @ for root
                    normalized_name = rec_name
                    if rec_name == zone_name:
                        normalized_name = '@'
                    elif rec_name.endswith(f'.{zone_name}'):
                        normalized_name = rec_name[:-len(f'.{zone_name}')]

                    imported_rec = {
                        'type': rec_type,
                        'name': normalized_name,
                        'content': rec_content,
                        'ttl': rec_ttl,
                        'proxied': rec_proxied,
                        'recordId': rec_id,
                    }

                    # Add priority for MX/SRV records
                    if rec_type in ('MX', 'SRV'):
                        rec_priority = getattr(rec, 'priority', None) if hasattr(rec, 'priority') else rec.get('priority')
                        if rec_priority is not None:
                            imported_rec['priority'] = rec_priority

                    imported_records.append(imported_rec)

                if imported_records:
                    # Patch spec.records with imported records
                    k8s.custom.patch_cluster_custom_object(
                        group=DNSZONE_GROUP,
                        version=DNSZONE_VERSION,
                        plural=DNSZONE_PLURAL,
                        name=name,
                        body={'spec': {'records': imported_records}}
                    )
                    logger.info(f"[DNSZone:{name}] Imported {len(imported_records)} records from CloudFlare")
                    patch.status['message'] = f"Imported {len(imported_records)} existing records from CloudFlare"

                    # Update spec_records for the rest of the reconciliation
                    spec_records = imported_records
                else:
                    logger.info(f"[DNSZone:{name}] No records to import from CloudFlare zone")

            except Exception as import_err:
                logger.warning(f"[DNSZone:{name}] Failed to import records from CloudFlare: {import_err}")
                # Continue with empty records - not fatal

        synced_records = []
        spec_updates = []  # List of (index, recordId) to patch back to spec

        # Process each spec record
        for i, rec in enumerate(spec_records):
            record_type = rec.get('type')
            record_name = rec.get('name')
            record_content = rec.get('content')
            record_ttl = rec.get('ttl', 1)
            record_proxied = rec.get('proxied', False)
            record_priority = rec.get('priority')
            record_id = rec.get('recordId')

            try:
                if record_id:
                    # Has recordId - check if needs update
                    status_rec = status_by_id.get(record_id)
                    if status_rec and status_rec.get('content') == record_content:
                        # Already synced, no change needed
                        logger.debug(f"Record already synced: {record_type} {record_name}")
                        synced_records.append({
                            'type': record_type,
                            'name': record_name,
                            'content': record_content,
                            'recordId': record_id,
                            'synced': True,
                        })
                    else:
                        # Content changed - update in CF
                        logger.info(f"Updating DNS record: {record_type} {record_name}")
                        params = {
                            'zone_id': zone_id,
                            'type': record_type,
                            'name': record_name,
                            'content': record_content,
                            'ttl': record_ttl,
                            'proxied': record_proxied,
                        }
                        if record_priority is not None:
                            params['priority'] = record_priority
                        try:
                            cf_client.dns.records.update(record_id, **params)
                            synced_records.append({
                                'type': record_type,
                                'name': record_name,
                                'content': record_content,
                                'recordId': record_id,
                                'synced': True,
                            })
                        except CloudflareAPIError as update_err:
                            # Record might have been deleted externally - try to create
                            if '81044' in str(update_err) or 'not found' in str(update_err).lower():
                                logger.warning(f"Record {record_id} not found, creating new one")
                                params.pop('zone_id')  # create uses zone_id differently
                                params['zone_id'] = zone_id
                                result = cf_client.dns.records.create(**params)
                                new_id = result.id if hasattr(result, 'id') else result['id']
                                spec_updates.append((i, new_id))
                                synced_records.append({
                                    'type': record_type,
                                    'name': record_name,
                                    'content': record_content,
                                    'recordId': new_id,
                                    'synced': True,
                                })
                            else:
                                raise
                else:
                    # No recordId - create in CF
                    logger.info(f"Creating DNS record: {record_type} {record_name}")
                    params = {
                        'zone_id': zone_id,
                        'type': record_type,
                        'name': record_name,
                        'content': record_content,
                        'ttl': record_ttl,
                        'proxied': record_proxied,
                    }
                    if record_priority is not None:
                        params['priority'] = record_priority

                    try:
                        result = cf_client.dns.records.create(**params)
                        new_id = result.id if hasattr(result, 'id') else result['id']
                        spec_updates.append((i, new_id))
                        synced_records.append({
                            'type': record_type,
                            'name': record_name,
                            'content': record_content,
                            'recordId': new_id,
                            'synced': True,
                        })
                        logger.info(f"Created DNS record: {record_type} {record_name} -> {new_id}")
                    except CloudflareAPIError as create_err:
                        # Handle duplicate record error
                        if '81058' in str(create_err) or 'already exists' in str(create_err).lower():
                            logger.info(f"Record already exists, fetching ID for {record_type} {record_name}")
                            # Try to find existing record
                            existing = cf_client.dns.records.list(
                                zone_id=zone_id,
                                type=record_type,
                                name=record_name,
                            )
                            found_id = None
                            for existing_rec in existing.result or []:
                                existing_content = existing_rec.content if hasattr(existing_rec, 'content') else existing_rec.get('content')
                                if existing_content == record_content:
                                    found_id = existing_rec.id if hasattr(existing_rec, 'id') else existing_rec['id']
                                    break
                            if found_id:
                                spec_updates.append((i, found_id))
                                synced_records.append({
                                    'type': record_type,
                                    'name': record_name,
                                    'content': record_content,
                                    'recordId': found_id,
                                    'synced': True,
                                })
                            else:
                                synced_records.append({
                                    'type': record_type,
                                    'name': record_name,
                                    'content': record_content,
                                    'synced': False,
                                    'error': 'Exists but could not find ID',
                                })
                        else:
                            raise

            except CloudflareAPIError as e:
                logger.error(f"Failed to sync record {record_type} {record_name}: {e}")
                synced_records.append({
                    'type': record_type,
                    'name': record_name,
                    'content': record_content,
                    'recordId': record_id,
                    'synced': False,
                    'error': str(e),
                })

        # Handle deletions: records in status but not in spec
        spec_record_ids = {r.get('recordId') for r in spec_records if r.get('recordId')}
        for record_id, status_rec in status_by_id.items():
            if record_id and record_id not in spec_record_ids:
                logger.info(f"Deleting orphaned DNS record: {status_rec.get('type')} {status_rec.get('name')} ({record_id})")
                try:
                    cf_client.dns.records.delete(record_id, zone_id=zone_id)
                    logger.info(f"Deleted DNS record {record_id}")
                except CloudflareAPIError as e:
                    if '81044' not in str(e) and 'not found' not in str(e).lower():
                        logger.warning(f"Failed to delete record {record_id}: {e}")

        # Update status
        patch.status['records'] = synced_records
        patch.status['recordCount'] = len([r for r in synced_records if r.get('synced')])
        patch.status['lastSyncedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        # Check if all records synced successfully
        all_synced = all(r.get('synced', False) for r in synced_records)
        if all_synced:
            patch.status['phase'] = 'Ready'
            patch.status['message'] = f"All {len(synced_records)} records synced"
        else:
            failed_count = len([r for r in synced_records if not r.get('synced')])
            patch.status['phase'] = 'Failed'
            patch.status['message'] = f"{failed_count} records failed to sync"

        # Patch spec with new recordIds
        if spec_updates:
            logger.info(f"Writing {len(spec_updates)} recordIds back to spec")
            # Build updated records list
            updated_records = list(spec_records)
            for idx, new_record_id in spec_updates:
                if idx < len(updated_records):
                    updated_records[idx] = {**updated_records[idx], 'recordId': new_record_id}

            # Patch the CR spec
            try:
                k8s.custom.patch_cluster_custom_object(
                    group=DNSZONE_GROUP,
                    version=DNSZONE_VERSION,
                    plural=DNSZONE_PLURAL,
                    name=name,
                    body={'spec': {'records': updated_records}},
                )
                logger.info(f"Patched DNSZone '{name}' spec with recordIds")
            except ApiException as e:
                logger.error(f"Failed to patch DNSZone spec: {e}")
                # Don't fail the reconciliation - status is still updated

        return {'message': f"Synced {len(synced_records)} records for zone {zone_name}"}

    except Exception as e:
        logger.error(f"Failed to reconcile DNSZone '{name}': {e}")
        patch.status['phase'] = 'Failed'
        patch.status['message'] = str(e)
        raise kopf.TemporaryError(f"Failed to reconcile: {e}", delay=60)


def delete_dnszone_records(core_api, credential_ref, zone_id, records):
    """Delete all DNS records for a zone when DNSZone CR is deleted."""
    if not credential_ref or not zone_id:
        return

    try:
        cf_client = get_cloudflare_client(core_api, credential_ref)
        for rec in records:
            record_id = rec.get('recordId')
            if record_id:
                try:
                    cf_client.dns.records.delete(record_id, zone_id=zone_id)
                    logger.info(f"Deleted DNS record {record_id}")
                except CloudflareAPIError as e:
                    if '81044' not in str(e) and 'not found' not in str(e).lower():
                        logger.warning(f"Failed to delete record {record_id}: {e}")
    except Exception as e:
        logger.error(f"Failed to delete DNS records: {e}")


@kopf.on.create(DNSZONE_GROUP, DNSZONE_VERSION, DNSZONE_PLURAL)
def on_dnszone_create(spec, name, status, patch, **kwargs):
    """Handle DNSZone CR creation."""
    logger.info(f"DNSZone '{name}' created, starting reconciliation...")
    patch.status['phase'] = 'Pending'
    patch.status['message'] = 'Starting sync...'
    return reconcile_dnszone(spec, name, status, patch, **kwargs)


@kopf.on.update(DNSZONE_GROUP, DNSZONE_VERSION, DNSZONE_PLURAL)
def on_dnszone_update(spec, name, status, patch, **kwargs):
    """Handle DNSZone CR updates."""
    logger.info(f"DNSZone '{name}' updated, reconciling changes...")
    return reconcile_dnszone(spec, name, status, patch, **kwargs)


@kopf.on.delete(DNSZONE_GROUP, DNSZONE_VERSION, DNSZONE_PLURAL)
def on_dnszone_delete(spec, name, status, **kwargs):
    """Handle DNSZone CR deletion."""
    logger.info(f"DNSZone '{name}' being deleted, cleaning up DNS records...")

    k8s = get_api_clients()
    credential_ref = spec.get('credentialSecretRef')
    zone_id = (status or {}).get('zoneId')
    records = (status or {}).get('records', [])

    delete_dnszone_records(k8s.core, credential_ref, zone_id, records)

    logger.info(f"DNSZone '{name}' cleanup complete")
    return {'message': f"DNSZone {name} deleted"}


@kopf.on.resume(DNSZONE_GROUP, DNSZONE_VERSION, DNSZONE_PLURAL)
def on_dnszone_resume(spec, name, status, patch, **kwargs):
    """Handle operator restart - reconcile existing DNSZone CRs."""
    logger.info(f"Resuming DNSZone '{name}'...")
    return reconcile_dnszone(spec, name, status, patch, **kwargs)


@kopf.timer(DNSZONE_GROUP, DNSZONE_VERSION, DNSZONE_PLURAL, interval=300, initial_delay=300)
def on_dnszone_timer(spec, name, status, patch, **kwargs):
    """Periodic reconciliation every 5 minutes (first run delayed to avoid overlap with resume)."""
    current_phase = (status or {}).get('phase')
    if current_phase == 'Ready':
        # Skip if already ready - DNS doesn't drift often
        logger.debug(f"DNSZone '{name}' is Ready, skipping periodic reconciliation")
        return
    logger.debug(f"Periodic reconciliation for DNSZone '{name}'")
    return reconcile_dnszone(spec, name, status, patch, **kwargs)


# =============================================================================
# GlobalWAF CR Management - Web Application Firewall on ingress-nginx
# =============================================================================

# GlobalWAF CRD constants
GLOBALWAF_GROUP = 'kubepanel.io'
GLOBALWAF_VERSION = 'v1alpha1'
GLOBALWAF_PLURAL = 'globalwafs'

# ingress-nginx ConfigMap location
INGRESS_NAMESPACE = 'ingress'
INGRESS_CONFIGMAP_NAME = 'nginx-load-balancer-microk8s-conf'

# ModSecurity rule ID ranges (to avoid conflicts with other rule sources)
GLOBALWAF_RULE_ID_START = 100000


def generate_modsec_rule(rule: dict, rule_id: int) -> str:
    """
    Generate a ModSecurity rule from a GlobalWAF rule spec.

    Supports any combination of: domain, ip, path
    Uses chain rules when multiple conditions are present.

    Args:
        rule: Rule dict with domain, ip, path, pathMatchType, action, comment
        rule_id: Unique rule ID

    Returns:
        ModSecurity SecRule string
    """
    domain = rule.get('domain')
    ip = rule.get('ip')
    path = rule.get('path')
    path_match_type = rule.get('pathMatchType', 'prefix')
    action = rule.get('action', 'block')
    comment = rule.get('comment', '')

    # Determine action keyword
    action_keyword = 'deny' if action == 'block' else 'allow'

    # Build conditions list
    conditions = []

    if domain:
        conditions.append(('SERVER_NAME', '@streq', domain))

    if ip:
        # Check if it's a CIDR or single IP
        if '/' in ip:
            conditions.append(('REMOTE_ADDR', '@ipMatch', ip))
        else:
            conditions.append(('REMOTE_ADDR', '@streq', ip))

    if path:
        if path_match_type == 'exact':
            conditions.append(('REQUEST_URI', '@streq', path))
        elif path_match_type == 'regex':
            conditions.append(('REQUEST_URI', '@rx', path))
        else:  # prefix (default)
            conditions.append(('REQUEST_URI', '@beginsWith', path))

    if not conditions:
        return ''  # No conditions, skip this rule

    # Build the rule message
    msg_parts = []
    if domain:
        msg_parts.append(f"domain={domain}")
    if ip:
        msg_parts.append(f"ip={ip}")
    if path:
        msg_parts.append(f"path={path}")
    msg = f"GlobalWAF: {action} {', '.join(msg_parts)}"
    if comment:
        msg += f" ({comment})"
    # Escape double quotes in the message to prevent breaking ModSecurity syntax
    msg = msg.replace('"', '\\"')

    # Single condition - simple rule
    if len(conditions) == 1:
        var, op, val = conditions[0]
        return f'SecRule {var} "{op} {val}" "phase:1,id:{rule_id},{action_keyword},log,msg:\\"{msg}\\""'

    # Multiple conditions - chain rules
    lines = []
    for i, (var, op, val) in enumerate(conditions):
        is_first = (i == 0)
        is_last = (i == len(conditions) - 1)

        if is_first:
            # First rule has the action and starts the chain
            lines.append(
                f'SecRule {var} "{op} {val}" "chain,phase:1,id:{rule_id},{action_keyword},log,msg:\\"{msg}\\""'
            )
        elif is_last:
            # Last rule in chain - no chain keyword, empty action
            lines.append(f'    SecRule {var} "{op} {val}" ""')
        else:
            # Middle rules - chain keyword, empty action
            lines.append(f'    SecRule {var} "{op} {val}" "chain"')

    return '\n'.join(lines)


def generate_modsec_rules(rules: list, geo_block: dict = None) -> str:
    """
    Generate all ModSecurity rules from GlobalWAF spec.

    Args:
        rules: List of rule dicts
        geo_block: Optional geo-blocking config

    Returns:
        Complete modsecurity-snippet content
    """
    modsec_rules = []
    rule_id = GLOBALWAF_RULE_ID_START

    # Header comment
    modsec_rules.append('# GlobalWAF Rules - Managed by KubePanel Operator')
    modsec_rules.append('# Do not edit manually - changes will be overwritten')
    modsec_rules.append('')
    modsec_rules.append('SecRuleEngine On')
    modsec_rules.append('')

    # Add GeoIP database directive if geo-blocking is enabled
    geo_enabled = geo_block and geo_block.get('enabled') and geo_block.get('blockedCountries')
    if geo_enabled:
        modsec_rules.append('# GeoIP Database for geo-blocking')
        modsec_rules.append('SecGeoLookupDb /etc/ingress-controller/geoip/dbip-country-lite-2026-03.mmdb')
        modsec_rules.append('')

    # Generate rules
    for rule in rules:
        modsec_rule = generate_modsec_rule(rule, rule_id)
        if modsec_rule:
            modsec_rules.append(modsec_rule)
            modsec_rules.append('')
            rule_id += 1

    # Geo-blocking rules
    if geo_enabled:
        countries = geo_block['blockedCountries']
        country_pattern = '|'.join(countries)
        modsec_rules.append(f'# Geo-blocking: {", ".join(countries)}')
        # First do a GeoIP lookup on the remote address, then check the country code
        modsec_rules.append(
            f'SecRule REMOTE_ADDR "@geoLookup" '
            f'"chain,phase:1,id:{rule_id},deny,log,msg:\\"GlobalWAF: Geo-blocked country\\""'
        )
        modsec_rules.append(f'    SecRule GEO:COUNTRY_CODE "@rx ^({country_pattern})$" ""')
        modsec_rules.append('')

    return '\n'.join(modsec_rules)


def update_ingress_configmap(core_api, modsec_content: str) -> None:
    """
    Update the ingress-nginx ConfigMap with ModSecurity rules.

    Args:
        core_api: Kubernetes CoreV1Api client
        modsec_content: ModSecurity rules content
    """
    try:
        configmap = core_api.read_namespaced_config_map(
            name=INGRESS_CONFIGMAP_NAME,
            namespace=INGRESS_NAMESPACE
        )

        if configmap.data is None:
            configmap.data = {}

        if modsec_content.strip():
            configmap.data['modsecurity-snippet'] = modsec_content
            logger.info(f"Setting modsecurity-snippet ({len(modsec_content)} chars)")
        else:
            # Set to comment instead of removing key - ensures ingress-nginx detects the change
            configmap.data['modsecurity-snippet'] = '# WAF disabled'
            logger.info("Setting modsecurity-snippet to disabled state")

        core_api.patch_namespaced_config_map(
            name=INGRESS_CONFIGMAP_NAME,
            namespace=INGRESS_NAMESPACE,
            body=configmap
        )

        logger.info(f"Updated ingress ConfigMap {INGRESS_NAMESPACE}/{INGRESS_CONFIGMAP_NAME}")

    except ApiException as e:
        logger.error(f"Failed to update ingress ConfigMap: {e}")
        raise


def reconcile_global_waf(spec, name, status, patch, **kwargs):
    """
    Reconcile GlobalWAF CR - generate ModSecurity rules and update ingress ConfigMap.

    1. Check if WAF is enabled
    2. Generate ModSecurity rules from spec.rules
    3. Update ingress-nginx ConfigMap with modsecurity-snippet
    4. Update CR status
    """
    enabled = spec.get('enabled', True)
    rules = spec.get('rules', [])
    geo_block = spec.get('geoBlock')

    k8s = get_api_clients()

    try:
        if not enabled:
            # WAF disabled - clear rules
            logger.info(f"GlobalWAF '{name}' is disabled, clearing rules...")
            update_ingress_configmap(k8s.core, '')
            patch.status['phase'] = 'Disabled'
            patch.status['message'] = 'WAF is disabled'
            patch.status['ruleCount'] = 0
            patch.status['lastSyncedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            return {'message': f"GlobalWAF '{name}' disabled"}

        # Generate ModSecurity rules
        patch.status['phase'] = 'Syncing'
        modsec_content = generate_modsec_rules(rules, geo_block)

        # Update ConfigMap
        update_ingress_configmap(k8s.core, modsec_content)

        # Update status
        patch.status['phase'] = 'Ready'
        patch.status['message'] = f'{len(rules)} rules synced to ingress-nginx'
        patch.status['ruleCount'] = len(rules)
        patch.status['lastSyncedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        logger.info(f"GlobalWAF '{name}' reconciled: {len(rules)} rules")
        return {'message': f"Synced {len(rules)} rules"}

    except Exception as e:
        logger.error(f"Failed to reconcile GlobalWAF '{name}': {e}")
        patch.status['phase'] = 'Failed'
        patch.status['message'] = str(e)[:1024]
        return {'error': str(e)}


@kopf.on.create(GLOBALWAF_GROUP, GLOBALWAF_VERSION, GLOBALWAF_PLURAL)
def on_globalwaf_create(spec, name, status, patch, **kwargs):
    """Handle GlobalWAF CR creation."""
    logger.info(f"GlobalWAF '{name}' created, starting reconciliation...")
    patch.status['phase'] = 'Pending'
    return reconcile_global_waf(spec, name, status, patch, **kwargs)


@kopf.on.update(GLOBALWAF_GROUP, GLOBALWAF_VERSION, GLOBALWAF_PLURAL)
def on_globalwaf_update(spec, name, status, patch, **kwargs):
    """Handle GlobalWAF CR updates."""
    logger.info(f"GlobalWAF '{name}' updated, reconciling changes...")
    return reconcile_global_waf(spec, name, status, patch, **kwargs)


@kopf.on.delete(GLOBALWAF_GROUP, GLOBALWAF_VERSION, GLOBALWAF_PLURAL)
def on_globalwaf_delete(spec, name, **kwargs):
    """Handle GlobalWAF CR deletion - clear all rules."""
    logger.info(f"GlobalWAF '{name}' being deleted, clearing rules...")

    k8s = get_api_clients()

    try:
        update_ingress_configmap(k8s.core, '')
        logger.info(f"GlobalWAF '{name}' cleanup complete - rules cleared")
    except Exception as e:
        logger.error(f"Failed to clear rules on GlobalWAF deletion: {e}")

    return {'message': f"GlobalWAF '{name}' deleted"}


@kopf.on.resume(GLOBALWAF_GROUP, GLOBALWAF_VERSION, GLOBALWAF_PLURAL)
def on_globalwaf_resume(spec, name, status, patch, **kwargs):
    """Handle operator restart - reconcile existing GlobalWAF CRs."""
    logger.info(f"Resuming GlobalWAF '{name}'...")

    # Skip full reconciliation if already Ready or Disabled
    current_phase = status.get('phase') if status else None
    if current_phase in ('Ready', 'Disabled'):
        logger.info(f"GlobalWAF '{name}' is {current_phase}, skipping full reconciliation")
        return {'message': f"GlobalWAF resumed (already {current_phase})"}

    return reconcile_global_waf(spec, name, status, patch, **kwargs)


# =============================================================================
# GlobalL3Firewall CR Management - Network-level firewall using Calico
# =============================================================================

# L3 Firewall CRD constants
L3FIREWALL_GROUP = 'kubepanel.io'
L3FIREWALL_VERSION = 'v1alpha1'
L3FIREWALL_PLURAL = 'globall3firewalls'

# Calico GlobalNetworkPolicy constants
CALICO_GROUP = 'crd.projectcalico.org'
CALICO_VERSION = 'v1'
CALICO_GNP_PLURAL = 'globalnetworkpolicies'
L3FIREWALL_POLICY_NAME = 'kubepanel-l3-firewall'


def generate_globalnetworkpolicy(rules: list) -> dict:
    """
    Generate a Calico GlobalNetworkPolicy from L3 firewall rules.

    Args:
        rules: List of rule dicts from GlobalL3Firewall CR spec.rules

    Returns:
        Calico GlobalNetworkPolicy resource dict
    """
    ingress_rules = []

    for rule in rules:
        action = rule.get('action', 'deny')
        protocol = rule.get('protocol', 'TCP')
        source = rule.get('source', {})
        destination = rule.get('destination', {})

        calico_rule = {
            'action': 'Deny' if action == 'deny' else 'Allow',
            'protocol': protocol,
        }

        # Source networks
        if source.get('nets'):
            calico_rule['source'] = {'nets': source['nets']}
        elif source.get('notNets'):
            calico_rule['source'] = {'notNets': source['notNets']}

        # Destination ports
        if destination.get('ports'):
            calico_rule['destination'] = {'ports': destination['ports']}

        ingress_rules.append(calico_rule)

    # Always add default allow at the end to not block all traffic
    ingress_rules.append({'action': 'Allow'})

    return {
        'apiVersion': f'{CALICO_GROUP}/{CALICO_VERSION}',
        'kind': 'GlobalNetworkPolicy',
        'metadata': {
            'name': L3FIREWALL_POLICY_NAME,
        },
        'spec': {
            'order': 100,
            'selector': 'all()',
            'types': ['Ingress'],
            'ingress': ingress_rules,
        }
    }


def apply_globalnetworkpolicy(custom_api, policy: dict) -> None:
    """
    Create or update a Calico GlobalNetworkPolicy.

    Args:
        custom_api: Kubernetes CustomObjectsApi client
        policy: GlobalNetworkPolicy resource dict
    """
    name = policy['metadata']['name']

    try:
        # Try to get existing policy
        existing = custom_api.get_cluster_custom_object(
            group=CALICO_GROUP,
            version=CALICO_VERSION,
            plural=CALICO_GNP_PLURAL,
            name=name
        )
        # Policy exists, copy resourceVersion for update (required by K8s)
        policy['metadata']['resourceVersion'] = existing['metadata']['resourceVersion']
        # Replace it
        custom_api.replace_cluster_custom_object(
            group=CALICO_GROUP,
            version=CALICO_VERSION,
            plural=CALICO_GNP_PLURAL,
            name=name,
            body=policy
        )
        logger.info(f"Updated GlobalNetworkPolicy '{name}'")

    except ApiException as e:
        if e.status == 404:
            # Policy doesn't exist, create it
            custom_api.create_cluster_custom_object(
                group=CALICO_GROUP,
                version=CALICO_VERSION,
                plural=CALICO_GNP_PLURAL,
                body=policy
            )
            logger.info(f"Created GlobalNetworkPolicy '{name}'")
        else:
            raise


def delete_globalnetworkpolicy(custom_api, name: str) -> None:
    """
    Delete a Calico GlobalNetworkPolicy.

    Args:
        custom_api: Kubernetes CustomObjectsApi client
        name: Policy name
    """
    try:
        custom_api.delete_cluster_custom_object(
            group=CALICO_GROUP,
            version=CALICO_VERSION,
            plural=CALICO_GNP_PLURAL,
            name=name
        )
        logger.info(f"Deleted GlobalNetworkPolicy '{name}'")
    except ApiException as e:
        if e.status == 404:
            logger.info(f"GlobalNetworkPolicy '{name}' already deleted")
        else:
            raise


def reconcile_l3_firewall(spec, name, status, patch, **kwargs):
    """
    Reconcile GlobalL3Firewall CR - generate and apply Calico GlobalNetworkPolicy.

    1. Check if L3 firewall is enabled
    2. Generate GlobalNetworkPolicy from spec.rules
    3. Apply/update or delete the policy
    4. Update CR status
    """
    enabled = spec.get('enabled', True)
    rules = spec.get('rules', [])

    k8s = get_api_clients()

    try:
        if not enabled:
            # L3 firewall disabled - delete policy
            logger.info(f"GlobalL3Firewall '{name}' is disabled, removing policy...")
            delete_globalnetworkpolicy(k8s.custom, L3FIREWALL_POLICY_NAME)
            patch.status['phase'] = 'Disabled'
            patch.status['message'] = 'L3 Firewall is disabled'
            patch.status['ruleCount'] = 0
            patch.status['lastSyncedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            return {'message': f"GlobalL3Firewall '{name}' disabled"}

        # Generate Calico GlobalNetworkPolicy
        patch.status['phase'] = 'Syncing'

        if rules:
            policy = generate_globalnetworkpolicy(rules)
            apply_globalnetworkpolicy(k8s.custom, policy)
            msg = f'{len(rules)} rules synced to GlobalNetworkPolicy'
        else:
            # No rules - delete policy to avoid unnecessary traffic filtering
            delete_globalnetworkpolicy(k8s.custom, L3FIREWALL_POLICY_NAME)
            msg = 'No rules configured, policy removed'

        # Update status
        patch.status['phase'] = 'Ready'
        patch.status['message'] = msg
        patch.status['ruleCount'] = len(rules)
        patch.status['lastSyncedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        logger.info(f"GlobalL3Firewall '{name}' reconciled: {len(rules)} rules")
        return {'message': f"Synced {len(rules)} rules"}

    except Exception as e:
        logger.error(f"Failed to reconcile GlobalL3Firewall '{name}': {e}")
        patch.status['phase'] = 'Failed'
        patch.status['message'] = str(e)[:1024]
        return {'error': str(e)}


@kopf.on.create(L3FIREWALL_GROUP, L3FIREWALL_VERSION, L3FIREWALL_PLURAL)
def on_l3firewall_create(spec, name, status, patch, **kwargs):
    """Handle GlobalL3Firewall CR creation."""
    logger.info(f"GlobalL3Firewall '{name}' created, starting reconciliation...")
    patch.status['phase'] = 'Pending'
    return reconcile_l3_firewall(spec, name, status, patch, **kwargs)


@kopf.on.update(L3FIREWALL_GROUP, L3FIREWALL_VERSION, L3FIREWALL_PLURAL)
def on_l3firewall_update(spec, name, status, patch, **kwargs):
    """Handle GlobalL3Firewall CR updates."""
    logger.info(f"GlobalL3Firewall '{name}' updated, reconciling changes...")
    return reconcile_l3_firewall(spec, name, status, patch, **kwargs)


@kopf.on.delete(L3FIREWALL_GROUP, L3FIREWALL_VERSION, L3FIREWALL_PLURAL)
def on_l3firewall_delete(spec, name, **kwargs):
    """Handle GlobalL3Firewall CR deletion - delete policy."""
    logger.info(f"GlobalL3Firewall '{name}' being deleted, removing policy...")

    k8s = get_api_clients()

    try:
        delete_globalnetworkpolicy(k8s.custom, L3FIREWALL_POLICY_NAME)
        logger.info(f"GlobalL3Firewall '{name}' cleanup complete - policy removed")
    except Exception as e:
        logger.error(f"Failed to delete policy on GlobalL3Firewall deletion: {e}")

    return {'message': f"GlobalL3Firewall '{name}' deleted"}


@kopf.on.resume(L3FIREWALL_GROUP, L3FIREWALL_VERSION, L3FIREWALL_PLURAL)
def on_l3firewall_resume(spec, name, status, patch, **kwargs):
    """Handle operator restart - reconcile existing GlobalL3Firewall CRs."""
    logger.info(f"Resuming GlobalL3Firewall '{name}'...")
    return reconcile_l3_firewall(spec, name, status, patch, **kwargs)


# =============================================================================
# DomainWAF CR Management - Per-domain ModSecurity WAF rules
# =============================================================================

# DomainWAF CRD constants
DOMAINWAF_GROUP = 'kubepanel.io'
DOMAINWAF_VERSION = 'v1alpha1'
DOMAINWAF_PLURAL = 'domainwafs'

# ModSecurity rule ID range for DomainWAF (separate from GlobalWAF to avoid conflicts)
DOMAINWAF_RULE_ID_START = 200000


def generate_domain_modsec_rule(rule: dict, rule_id: int, domain_name: str = None) -> str:
    """
    Generate a ModSecurity rule from a DomainWAF rule spec.

    Supports: ip, path, userAgent, countries conditions with chain rules for multiple conditions.

    Args:
        rule: Rule dict with ip, path, pathMatchType, userAgent, countries, action, comment
        rule_id: Unique rule ID
        domain_name: Domain name for log messages (optional)

    Returns:
        ModSecurity SecRule string
    """
    ip = rule.get('ip')
    path = rule.get('path')
    path_match_type = rule.get('pathMatchType', 'prefix')
    user_agent = rule.get('userAgent')
    countries = rule.get('countries', [])  # Optional: only block from these countries
    action = rule.get('action', 'block')
    comment = rule.get('comment', '')

    # Determine action keyword (only block is supported now)
    action_keyword = 'deny'

    # Build conditions list
    conditions = []

    if ip:
        # Check if it's a CIDR or single IP
        if '/' in ip:
            conditions.append(('REMOTE_ADDR', '@ipMatch', ip))
        else:
            conditions.append(('REMOTE_ADDR', '@streq', ip))

    if path:
        if path_match_type == 'exact':
            conditions.append(('REQUEST_URI', '@streq', path))
        elif path_match_type == 'regex':
            conditions.append(('REQUEST_URI', '@rx', path))
        else:  # prefix (default)
            conditions.append(('REQUEST_URI', '@beginsWith', path))

    if user_agent:
        # User agent matching - use regex for flexibility
        conditions.append(('REQUEST_HEADERS:User-Agent', '@rx', user_agent))

    # Add country restriction if specified (block only from these countries)
    if countries:
        country_pattern = '|'.join(countries)
        # GeoIP lookup must be chained before country code check
        conditions.append(('REMOTE_ADDR', '@geoLookup', ''))
        conditions.append(('GEO:COUNTRY_CODE', '@rx', f'^({country_pattern})$'))

    if not conditions:
        return ''  # No conditions, skip this rule

    # Build the rule message - sanitize for ModSecurity compatibility
    msg_parts = []
    if domain_name:
        msg_parts.append(f"domain={domain_name}")
    if ip:
        msg_parts.append(f"ip={ip}")
    if path:
        # Sanitize path - remove special chars that break ModSecurity
        safe_path = path.replace("'", "").replace('"', "").replace("\\", "")
        msg_parts.append(f"path={safe_path}")
    if user_agent:
        msg_parts.append(f"ua={user_agent[:50]}")  # Truncate long UA
    if countries:
        msg_parts.append(f"countries={','.join(countries)}")
    msg = f"DomainWAF block {' '.join(msg_parts)}"
    if comment:
        # Sanitize comment - remove parentheses, quotes, and other special chars
        safe_comment = comment.replace("(", "").replace(")", "").replace('"', "").replace("'", "").replace("\\", "")
        msg += f" - {safe_comment}"

    # Single condition - simple rule
    if len(conditions) == 1:
        var, op, val = conditions[0]
        return f"SecRule {var} \"{op} {val}\" \"phase:1,id:{rule_id},{action_keyword},log,msg:'{msg}'\""

    # Multiple conditions - chain rules
    lines = []
    for i, (var, op, val) in enumerate(conditions):
        is_first = (i == 0)
        is_last = (i == len(conditions) - 1)

        if is_first:
            # First rule has the action and starts the chain
            lines.append(
                f"SecRule {var} \"{op} {val}\" \"chain,phase:1,id:{rule_id},{action_keyword},log,msg:'{msg}'\""
            )
        elif is_last:
            # Last rule in chain - no chain keyword, empty action
            lines.append(f'    SecRule {var} "{op} {val}" ""')
        else:
            # Middle rules - chain keyword, empty action
            lines.append(f'    SecRule {var} "{op} {val}" "chain"')

    return '\n'.join(lines)


def generate_protected_path_rules(protected_path: dict, rule_id: int, domain_name: str = None) -> str:
    """
    Generate ModSecurity rules for a protected path (allowlist).

    Protected paths deny access UNLESS the request comes from an allowed IP or country.

    Args:
        protected_path: Dict with path, pathMatchType, allowedIp, allowedCountries, comment
        rule_id: Unique rule ID
        domain_name: Domain name for log messages (optional)

    Returns:
        ModSecurity SecRule string (or empty string if no conditions)
    """
    path = protected_path.get('path', '/')
    path_match_type = protected_path.get('pathMatchType', 'prefix')
    allowed_ip = protected_path.get('allowedIp')
    allowed_countries = protected_path.get('allowedCountries', [])
    comment = protected_path.get('comment', '')

    # Build path operator
    if path_match_type == 'exact':
        path_op = '@streq'
    elif path_match_type == 'regex':
        path_op = '@rx'
    else:  # prefix (default)
        path_op = '@beginsWith'

    # Sanitize path for message
    safe_path = path.replace("'", "").replace('"', "").replace("\\", "")

    # Build message
    msg_parts = []
    if domain_name:
        msg_parts.append(f"domain={domain_name}")
    msg_parts.append(f"path={safe_path}")

    if allowed_ip:
        # Deny if path matches AND IP doesn't match
        msg = f"DomainWAF Protected path - IP not allowed {' '.join(msg_parts)}"
        if comment:
            safe_comment = comment.replace("(", "").replace(")", "").replace('"', "").replace("'", "").replace("\\", "")
            msg += f" - {safe_comment}"

        # Check if CIDR or single IP
        if '/' in allowed_ip:
            ip_op = '!@ipMatch'
        else:
            ip_op = '!@streq'

        return (
            f"SecRule REQUEST_URI \"{path_op} {path}\" \"chain,phase:1,id:{rule_id},deny,log,msg:'{msg}'\"\n"
            f"    SecRule REMOTE_ADDR \"{ip_op} {allowed_ip}\" \"\""
        )

    if allowed_countries:
        # Deny if path matches AND country doesn't match
        country_pattern = '|'.join(allowed_countries)
        msg = f"DomainWAF Protected path - country not allowed {' '.join(msg_parts)}"
        if comment:
            safe_comment = comment.replace("(", "").replace(")", "").replace('"', "").replace("'", "").replace("\\", "")
            msg += f" - {safe_comment}"

        return (
            f"SecRule REQUEST_URI \"{path_op} {path}\" \"chain,phase:1,id:{rule_id},deny,log,msg:'{msg}'\"\n"
            f"    SecRule REMOTE_ADDR \"@geoLookup\" \"chain\"\n"
            f"        SecRule GEO:COUNTRY_CODE \"!@rx ^({country_pattern})$\" \"\""
        )

    # No allowed IP or countries - skip this rule
    return ''


def generate_domain_modsec_rules(rules: list, geo_block: dict = None, protected_paths: list = None, domain_name: str = None) -> str:
    """
    Generate all ModSecurity rules from DomainWAF spec.

    Args:
        rules: List of rule dicts
        geo_block: Optional geo-blocking config
        protected_paths: Optional list of protected path configs
        domain_name: Domain name for log messages

    Returns:
        Complete ModSecurity rules content for the domain
    """
    modsec_rules = []
    rule_id = DOMAINWAF_RULE_ID_START
    protected_paths = protected_paths or []

    # Header comment
    modsec_rules.append(f'# DomainWAF Rules for {domain_name or "domain"}')
    modsec_rules.append('# Managed by KubePanel Operator - do not edit manually')
    modsec_rules.append('')
    modsec_rules.append('SecRuleEngine On')
    modsec_rules.append('')

    # Check if GeoIP database is needed:
    # - Domain-wide geo-blocking enabled
    # - Any rule has countries restriction
    # - Any protected path has allowedCountries
    geo_enabled = geo_block and geo_block.get('enabled') and geo_block.get('blockedCountries')
    rules_need_geoip = any(rule.get('countries') for rule in rules)
    protected_paths_need_geoip = any(pp.get('allowedCountries') for pp in protected_paths)

    if geo_enabled or rules_need_geoip or protected_paths_need_geoip:
        modsec_rules.append('# GeoIP Database for geo-blocking')
        modsec_rules.append('SecGeoLookupDb /etc/nginx/geoip/dbip-country-lite-2026-03.mmdb')
        modsec_rules.append('')

    # Generate block rules
    if rules:
        modsec_rules.append('# Block Rules')
        for rule in rules:
            modsec_rule = generate_domain_modsec_rule(rule, rule_id, domain_name)
            if modsec_rule:
                modsec_rules.append(modsec_rule)
                modsec_rules.append('')
                rule_id += 1

    # Geo-blocking rules (domain-wide)
    if geo_enabled:
        countries = geo_block['blockedCountries']
        country_pattern = '|'.join(countries)
        modsec_rules.append(f'# Geo-blocking: {", ".join(countries)}')
        modsec_rules.append(
            f"SecRule REMOTE_ADDR \"@geoLookup\" "
            f"\"chain,phase:1,id:{rule_id},deny,log,msg:'DomainWAF Geo-blocked country'\""
        )
        modsec_rules.append(f'    SecRule GEO:COUNTRY_CODE "@rx ^({country_pattern})$" ""')
        modsec_rules.append('')
        rule_id += 1

    # Protected paths rules
    if protected_paths:
        modsec_rules.append('# Protected Paths')
        for pp in protected_paths:
            pp_rule = generate_protected_path_rules(pp, rule_id, domain_name)
            if pp_rule:
                modsec_rules.append(pp_rule)
                modsec_rules.append('')
                rule_id += 1

    return '\n'.join(modsec_rules)


def update_domain_modsec_configmap(core_api, namespace: str, modsec_content: str) -> bool:
    """
    Create or update the modsec-rules ConfigMap in the domain namespace.

    Args:
        core_api: Kubernetes CoreV1Api client
        namespace: Domain namespace (e.g., 'dom-example-com')
        modsec_content: ModSecurity rules content

    Returns:
        True if ConfigMap was created or updated, False if unchanged
    """
    configmap_name = 'modsec-rules'
    configmap_changed = False

    try:
        # Try to get existing ConfigMap
        existing_content = None
        try:
            configmap = core_api.read_namespaced_config_map(
                name=configmap_name,
                namespace=namespace
            )
            existing_content = configmap.data.get('rules.conf', '') if configmap.data else ''
        except ApiException as e:
            if e.status != 404:
                raise

        # When WAF is disabled or has no rules, keep ConfigMap with empty content
        # (don't delete it - the deployment may still have the volume mount)
        if not modsec_content.strip():
            modsec_content = '# DomainWAF disabled - no rules active'

        # Check if content actually changed
        if existing_content == modsec_content:
            logger.debug(f"modsec-rules ConfigMap unchanged in {namespace}")
            return False

        # Create or update ConfigMap
        configmap_body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=configmap_name,
                namespace=namespace,
                labels={
                    'app.kubernetes.io/managed-by': 'kubepanel-operator',
                    'app.kubernetes.io/component': 'waf'
                }
            ),
            data={
                'rules.conf': modsec_content,
            }
        )

        if existing_content is not None:
            core_api.replace_namespaced_config_map(
                name=configmap_name,
                namespace=namespace,
                body=configmap_body
            )
            logger.info(f"Updated modsec-rules ConfigMap in {namespace}")
        else:
            core_api.create_namespaced_config_map(
                namespace=namespace,
                body=configmap_body
            )
            logger.info(f"Created modsec-rules ConfigMap in {namespace}")
        configmap_changed = True

    except ApiException as e:
        logger.error(f"Failed to update modsec-rules ConfigMap in {namespace}: {e}")
        raise

    return configmap_changed


def trigger_deployment_restart(apps_api, namespace: str, deployment_name: str = 'web') -> None:
    """
    Trigger a deployment restart by patching the restart annotation.

    Args:
        apps_api: Kubernetes AppsV1Api client
        namespace: Namespace of the deployment
        deployment_name: Name of the deployment (default: 'web')
    """
    try:
        restart_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        patch_body = {
            'spec': {
                'template': {
                    'metadata': {
                        'annotations': {
                            'kubepanel.io/restart-trigger': restart_timestamp
                        }
                    }
                }
            }
        }
        apps_api.patch_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            body=patch_body
        )
        logger.info(f"Triggered deployment restart in '{namespace}'")
    except ApiException as e:
        if e.status == 404:
            logger.warning(f"Deployment '{deployment_name}' not found in '{namespace}', skipping restart")
        else:
            logger.error(f"Failed to trigger deployment restart in '{namespace}': {e}")


def reconcile_domain_waf(spec, name, namespace, status, patch, **kwargs):
    """
    Reconcile DomainWAF CR - generate ModSecurity rules for the domain.

    1. Check if WAF is enabled
    2. Generate ModSecurity rules from spec.rules, geo_block, and protected_paths
    3. Update modsec-rules ConfigMap in domain namespace
    4. Trigger deployment restart to pick up new rules
    5. Update CR status
    """
    enabled = spec.get('enabled', True)
    rules = spec.get('rules', [])
    geo_block = spec.get('geoBlock')
    protected_paths = spec.get('protectedPaths', [])

    # Extract domain name from namespace (dom-example-com -> example.com)
    domain_name = namespace.replace('dom-', '').replace('-', '.')

    k8s = get_api_clients()

    try:
        if not enabled:
            # WAF disabled - clear rules
            logger.info(f"DomainWAF '{namespace}/{name}' is disabled, clearing rules...")
            configmap_changed = update_domain_modsec_configmap(k8s.core, namespace, '')

            # Trigger deployment restart only if rules actually changed
            if configmap_changed:
                trigger_deployment_restart(k8s.apps, namespace)

            patch.status['phase'] = 'Disabled'
            patch.status['message'] = 'WAF is disabled'
            patch.status['ruleCount'] = 0
            patch.status['lastSyncedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            return {'message': f"DomainWAF '{namespace}/{name}' disabled"}

        # Generate ModSecurity rules
        patch.status['phase'] = 'Syncing'
        modsec_content = generate_domain_modsec_rules(rules, geo_block, protected_paths, domain_name)

        # Update ConfigMap (returns True if content changed)
        configmap_changed = update_domain_modsec_configmap(k8s.core, namespace, modsec_content)

        # Trigger deployment restart only if rules actually changed
        if configmap_changed:
            trigger_deployment_restart(k8s.apps, namespace)

        # Calculate total rule count (block rules + protected paths)
        total_rules = len(rules) + len(protected_paths)

        # Update status
        patch.status['phase'] = 'Ready'
        patch.status['message'] = f'{len(rules)} block rules, {len(protected_paths)} protected paths synced'
        patch.status['ruleCount'] = total_rules
        patch.status['lastSyncedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        logger.info(f"DomainWAF '{namespace}/{name}' reconciled: {len(rules)} block rules, {len(protected_paths)} protected paths")
        return {'message': f"Synced {total_rules} rules"}

    except Exception as e:
        logger.error(f"Failed to reconcile DomainWAF '{namespace}/{name}': {e}")
        patch.status['phase'] = 'Failed'
        patch.status['message'] = str(e)[:1024]
        return {'error': str(e)}


@kopf.on.create(DOMAINWAF_GROUP, DOMAINWAF_VERSION, DOMAINWAF_PLURAL)
def on_domainwaf_create(spec, name, namespace, status, patch, **kwargs):
    """Handle DomainWAF CR creation."""
    logger.info(f"DomainWAF '{namespace}/{name}' created, starting reconciliation...")
    patch.status['phase'] = 'Pending'
    return reconcile_domain_waf(spec, name, namespace, status, patch, **kwargs)


@kopf.on.update(DOMAINWAF_GROUP, DOMAINWAF_VERSION, DOMAINWAF_PLURAL)
def on_domainwaf_update(spec, name, namespace, status, patch, **kwargs):
    """Handle DomainWAF CR updates."""
    logger.info(f"DomainWAF '{namespace}/{name}' updated, reconciling changes...")
    return reconcile_domain_waf(spec, name, namespace, status, patch, **kwargs)


@kopf.on.delete(DOMAINWAF_GROUP, DOMAINWAF_VERSION, DOMAINWAF_PLURAL)
def on_domainwaf_delete(spec, name, namespace, **kwargs):
    """Handle DomainWAF CR deletion - clear rules ConfigMap."""
    logger.info(f"DomainWAF '{namespace}/{name}' being deleted, clearing rules...")

    k8s = get_api_clients()

    try:
        update_domain_modsec_configmap(k8s.core, namespace, '')
        logger.info(f"DomainWAF '{namespace}/{name}' cleanup complete - rules cleared")
    except Exception as e:
        logger.error(f"Failed to clear rules on DomainWAF deletion: {e}")

    return {'message': f"DomainWAF '{namespace}/{name}' deleted"}


@kopf.on.resume(DOMAINWAF_GROUP, DOMAINWAF_VERSION, DOMAINWAF_PLURAL)
def on_domainwaf_resume(spec, name, namespace, status, patch, **kwargs):
    """Handle operator restart - reconcile existing DomainWAF CRs."""
    logger.info(f"Resuming DomainWAF '{namespace}/{name}'...")

    # Skip full reconciliation if WAF is already Ready or Disabled
    # The modsec-rules ConfigMap and deployment are already configured
    current_phase = status.get('phase') if status else None
    if current_phase in ('Ready', 'Disabled'):
        k8s = get_api_clients()
        try:
            # Quick check: verify ConfigMap exists
            k8s.core.read_namespaced_config_map(name='modsec-rules', namespace=namespace)
            logger.info(f"DomainWAF '{namespace}/{name}' is {current_phase}, skipping full reconciliation")
            return {'message': f"DomainWAF resumed (already {current_phase})"}
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"modsec-rules ConfigMap missing for '{namespace}/{name}', triggering reconciliation")
            else:
                raise

    return reconcile_domain_waf(spec, name, namespace, status, patch, **kwargs)


# =============================================================================
# SMTPFirewall CR Management - SMTP blocking and rate limits via rspamd
# =============================================================================

# SMTPFirewall CRD constants
SMTPFIREWALL_GROUP = 'kubepanel.io'
SMTPFIREWALL_VERSION = 'v1alpha1'
SMTPFIREWALL_PLURAL = 'smtpfirewalls'
SMTPFIREWALL_CONFIGMAP = 'rspamd-config'
SMTPFIREWALL_NAMESPACE = 'kubepanel'


def reconcile_smtp_firewall(spec, name, status, patch, **kwargs):
    """
    Reconcile SMTPFirewall CR - update rspamd ConfigMap with map files.

    1. Read spec for blocked senders, domains, IPs, and rate limits
    2. Generate map file contents
    3. Patch rspamd ConfigMap
    4. Trigger rspamd deployment restart
    5. Update CR status
    """
    blocked_senders = spec.get('blockedSenders', [])
    blocked_domains = spec.get('blockedDomains', [])
    blocked_ips = spec.get('blockedIPs', [])
    rate_limits = spec.get('rateLimits', [])

    k8s = get_api_clients()

    try:
        patch.status['phase'] = 'Syncing'

        # Generate map file contents (one entry per line)
        # For blocking maps, just the value (rspamd checks presence in map)
        senders_map = '\n'.join(blocked_senders) if blocked_senders else '# No blocked senders'
        domains_map = '\n'.join(blocked_domains) if blocked_domains else '# No blocked domains'
        ips_map = '\n'.join(blocked_ips) if blocked_ips else '# No blocked IPs'

        # For rate limits map, format: user rate (hash map type)
        # e.g., "user@example.com 50 / 1h"
        rates_lines = []
        for rl in rate_limits:
            user = rl.get('user', '')
            rate = rl.get('rate', '')
            if user and rate:
                rates_lines.append(f"{user} {rate}")
        rates_map = '\n'.join(rates_lines) if rates_lines else '# No custom rate limits'

        # Read current ConfigMap
        try:
            configmap = k8s.core.read_namespaced_config_map(
                SMTPFIREWALL_CONFIGMAP, SMTPFIREWALL_NAMESPACE
            )
        except ApiException as e:
            if e.status == 404:
                logger.error(f"ConfigMap {SMTPFIREWALL_CONFIGMAP} not found in {SMTPFIREWALL_NAMESPACE}")
                patch.status['phase'] = 'Failed'
                patch.status['message'] = f'ConfigMap {SMTPFIREWALL_CONFIGMAP} not found'
                return {'error': 'ConfigMap not found'}
            raise

        # Update map files in ConfigMap data
        if configmap.data is None:
            configmap.data = {}

        # Check if any data actually changed
        current_senders = configmap.data.get('blocked_senders.map', '')
        current_domains = configmap.data.get('blocked_domains.map', '')
        current_ips = configmap.data.get('blocked_ips.map', '')
        current_rates = configmap.data.get('user_ratelimits.map', '')

        configmap_changed = (
            current_senders != senders_map or
            current_domains != domains_map or
            current_ips != ips_map or
            current_rates != rates_map
        )

        if configmap_changed:
            configmap.data['blocked_senders.map'] = senders_map
            configmap.data['blocked_domains.map'] = domains_map
            configmap.data['blocked_ips.map'] = ips_map
            configmap.data['user_ratelimits.map'] = rates_map

            # Patch the ConfigMap
            k8s.core.patch_namespaced_config_map(
                SMTPFIREWALL_CONFIGMAP, SMTPFIREWALL_NAMESPACE, configmap
            )

            logger.info(f"SMTPFirewall '{name}': Updated ConfigMap with {len(blocked_senders)} senders, "
                        f"{len(blocked_domains)} domains, {len(blocked_ips)} IPs, {len(rate_limits)} rate limits")

            # Trigger rspamd deployment restart only if maps changed
            trigger_deployment_restart(k8s.apps, SMTPFIREWALL_NAMESPACE, 'rspamd')
        else:
            logger.debug(f"SMTPFirewall '{name}': ConfigMap unchanged, skipping restart")

        # Update status
        patch.status['phase'] = 'Active'
        patch.status['message'] = f'{len(blocked_senders)} senders, {len(blocked_domains)} domains, {len(blocked_ips)} IPs, {len(rate_limits)} rate limits'
        patch.status['blockedSendersCount'] = len(blocked_senders)
        patch.status['blockedDomainsCount'] = len(blocked_domains)
        patch.status['blockedIPsCount'] = len(blocked_ips)
        patch.status['rateLimitsCount'] = len(rate_limits)
        patch.status['lastSyncedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        return {'message': f"SMTPFirewall '{name}' reconciled"}

    except Exception as e:
        logger.error(f"Failed to reconcile SMTPFirewall '{name}': {e}")
        patch.status['phase'] = 'Failed'
        patch.status['message'] = str(e)[:1024]
        return {'error': str(e)}


@kopf.on.create(SMTPFIREWALL_GROUP, SMTPFIREWALL_VERSION, SMTPFIREWALL_PLURAL)
def on_smtpfirewall_create(spec, name, status, patch, **kwargs):
    """Handle SMTPFirewall CR creation."""
    logger.info(f"SMTPFirewall '{name}' created, starting reconciliation...")
    patch.status['phase'] = 'Pending'
    return reconcile_smtp_firewall(spec, name, status, patch, **kwargs)


@kopf.on.update(SMTPFIREWALL_GROUP, SMTPFIREWALL_VERSION, SMTPFIREWALL_PLURAL)
def on_smtpfirewall_update(spec, name, status, patch, **kwargs):
    """Handle SMTPFirewall CR updates."""
    logger.info(f"SMTPFirewall '{name}' updated, reconciling changes...")
    return reconcile_smtp_firewall(spec, name, status, patch, **kwargs)


@kopf.on.delete(SMTPFIREWALL_GROUP, SMTPFIREWALL_VERSION, SMTPFIREWALL_PLURAL)
def on_smtpfirewall_delete(spec, name, **kwargs):
    """Handle SMTPFirewall CR deletion - clear map files."""
    logger.info(f"SMTPFirewall '{name}' being deleted, clearing map files...")

    k8s = get_api_clients()

    try:
        # Clear map files in ConfigMap
        configmap = k8s.core.read_namespaced_config_map(
            SMTPFIREWALL_CONFIGMAP, SMTPFIREWALL_NAMESPACE
        )

        if configmap.data:
            configmap.data['blocked_senders.map'] = '# No blocked senders'
            configmap.data['blocked_domains.map'] = '# No blocked domains'
            configmap.data['blocked_ips.map'] = '# No blocked IPs'
            configmap.data['user_ratelimits.map'] = '# No custom rate limits'

            k8s.core.patch_namespaced_config_map(
                SMTPFIREWALL_CONFIGMAP, SMTPFIREWALL_NAMESPACE, configmap
            )

        # Restart rspamd to apply cleared maps
        trigger_deployment_restart(k8s.apps, SMTPFIREWALL_NAMESPACE, 'rspamd')

        logger.info(f"SMTPFirewall '{name}' cleanup complete - map files cleared")
    except Exception as e:
        logger.error(f"Failed to clear maps on SMTPFirewall deletion: {e}")

    return {'message': f"SMTPFirewall '{name}' deleted"}


@kopf.on.resume(SMTPFIREWALL_GROUP, SMTPFIREWALL_VERSION, SMTPFIREWALL_PLURAL)
def on_smtpfirewall_resume(spec, name, status, patch, **kwargs):
    """Handle operator restart - reconcile existing SMTPFirewall CRs."""
    logger.info(f"Resuming SMTPFirewall '{name}'...")

    # Skip full reconciliation if already Active - ConfigMap is already configured
    current_phase = status.get('phase') if status else None
    if current_phase == 'Active':
        k8s = get_api_clients()
        try:
            # Quick check: verify ConfigMap exists
            k8s.core.read_namespaced_config_map(SMTPFIREWALL_CONFIGMAP, SMTPFIREWALL_NAMESPACE)
            logger.info(f"SMTPFirewall '{name}' is Active, skipping full reconciliation")
            return {'message': f"SMTPFirewall resumed (already Active)"}
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"SMTPFirewall ConfigMap missing, triggering reconciliation")
            else:
                raise

    return reconcile_smtp_firewall(spec, name, status, patch, **kwargs)


# =============================================================================
# License Management
# =============================================================================

import hashlib
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.exceptions import InvalidSignature

# License verification public key 
LICENSE_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAV3dpjazDJjUnl6tED1aKyEMYuOCNM4y7i2oblXmKQx4=
-----END PUBLIC KEY-----"""



def _get_license_public_key() -> Ed25519PublicKey:
    """Load the embedded license public key."""
    return load_pem_public_key(LICENSE_PUBLIC_KEY_PEM)


def _add_base64_padding(data: str) -> str:
    """Add padding to base64 string if needed."""
    missing_padding = len(data) % 4
    if missing_padding:
        data += '=' * (4 - missing_padding)
    return data


def verify_license_key(license_key: str) -> dict | None:
    """
    Verify a license key signature and return the payload.

    Args:
        license_key: The license key in format "base64(payload).base64(signature)"

    Returns:
        The decoded payload dict if valid, None if invalid.
    """
    if not license_key or '.' not in license_key:
        return None

    try:
        parts = license_key.rsplit('.', 1)
        if len(parts) != 2:
            return None

        payload_b64, sig_b64 = parts

        payload_bytes = base64.urlsafe_b64decode(_add_base64_padding(payload_b64))
        signature = base64.urlsafe_b64decode(_add_base64_padding(sig_b64))

        public_key = _get_license_public_key()
        public_key.verify(signature, payload_bytes)

        import json
        payload = json.loads(payload_bytes.decode('utf-8'))

        # Validate required fields
        required_fields = ['iss', 'sub', 'tier', 'exp']
        for field in required_fields:
            if field not in payload:
                logger.warning(f"License payload missing required field: {field}")
                return None

        if payload.get('iss') != 'kubepanel.io':
            logger.warning(f"Invalid license issuer: {payload.get('iss')}")
            return None

        return payload

    except InvalidSignature:
        logger.warning("License key signature verification failed")
        return None
    except Exception as e:
        logger.warning(f"License key verification failed: {e}")
        return None


def count_domains() -> int:
    """Count total Domain CRs in the cluster."""
    try:
        k8s = get_api_clients()
        domains = k8s.custom.list_cluster_custom_object(
            group=DOMAIN_GROUP,
            version=DOMAIN_VERSION,
            plural=DOMAIN_PLURAL
        )
        return len(domains.get('items', []))
    except Exception as e:
        logger.error(f"Failed to count domains: {e}")
        return 0


def count_users() -> int:
    """Count users (placeholder - would need Django DB access or API)."""
    # For now, return 0 - this could be enhanced to call a Django API
    # or read from a ConfigMap that Django updates
    return 0


def get_k8s_version() -> str:
    """Get Kubernetes cluster version."""
    try:
        k8s = get_api_clients()
        version_info = k8s.core.api_client.call_api(
            '/version', 'GET',
            response_type='object',
            _return_http_data_only=True
        )
        return version_info.get('gitVersion', 'unknown')
    except Exception:
        return 'unknown'


def count_cluster_nodes() -> int:
    """Count nodes in the cluster."""
    try:
        k8s = get_api_clients()
        nodes = k8s.core.list_node()
        return len(nodes.items)
    except Exception:
        return 0


def get_cluster_id() -> str:
    """
    Get unique cluster identifier from kube-system namespace UID.
    This is stable across restarts and independent of IP addresses.
    """
    try:
        k8s = get_api_clients()
        ns = k8s.core.read_namespace(name='kube-system')
        return ns.metadata.uid
    except Exception as e:
        logger.warning(f"Failed to get cluster ID: {e}")
        return 'unknown'


def do_phone_home(patch, payload: dict, current_status: dict | None = None):
    """
    Send stats to license server and handle response.

    Args:
        patch: Kopf patch object for updating CR status
        payload: Verified license payload
        current_status: Current status from CR (for failure counting)
    """
    current_status = current_status or {}

    # Collect stats
    stats = {
        'customer_id': payload.get('sub', ''),
        'license_key_hash': hashlib.sha256(
            payload.get('sub', '').encode()
        ).hexdigest()[:16],
        'cluster_id': get_cluster_id(),
        'domain_count': count_domains(),
        'user_count': count_users(),
        'kubepanel_version': KUBEPANEL_VERSION,
        'kubernetes_version': get_k8s_version(),
        'cluster_nodes': count_cluster_nodes(),
        'timestamp': datetime.now(timezone.utc).isoformat()
    }

    phone_home_url = payload.get('phone_home_url', PHONE_HOME_URL)

    try:
        response = requests.post(
            phone_home_url,
            json=stats,
            timeout=30,
            headers={
                'Authorization': f'Bearer {payload.get("sub", "")}',
                'Content-Type': 'application/json',
                'User-Agent': f'KubePanel/{KUBEPANEL_VERSION}'
            }
        )
        response.raise_for_status()

        # Success - reset failure counter
        patch.status['lastPhoneHome'] = stats['timestamp']
        patch.status['phoneHomeFailures'] = 0
        patch.status['gracePeriodEndsAt'] = None
        patch.status['domainsUsed'] = stats['domain_count']

        # Server can return updated license info or revocation
        try:
            server_response = response.json()
            if server_response.get('revoked'):
                patch.status['valid'] = False
                patch.status['tier'] = 'community'
                patch.status['maxDomains'] = COMMUNITY_MAX_DOMAINS
                patch.status['message'] = 'License revoked by server'
                logger.warning(f"License revoked for customer: {payload.get('sub')}")
        except Exception:
            pass  # Server response parsing is optional

        logger.info(f"Phone-home successful for customer: {payload.get('sub')}")

    except requests.RequestException as e:
        # Phone-home failed
        failures = current_status.get('phoneHomeFailures', 0) + 1
        patch.status['phoneHomeFailures'] = failures

        logger.warning(f"Phone-home failed (attempt {failures}): {e}")

        # After 3 failures, enter grace period
        grace_end_str = current_status.get('gracePeriodEndsAt')

        if failures >= 3 and not grace_end_str:
            grace_end = datetime.now(timezone.utc) + timedelta(days=GRACE_PERIOD_DAYS)
            patch.status['gracePeriodEndsAt'] = grace_end.isoformat()
            patch.status['message'] = (
                f'Phone-home failed {failures} times. '
                f'Grace period until {grace_end.date()}. '
                f'Please check network connectivity to {phone_home_url}'
            )
            logger.warning(f"Entering grace period until {grace_end.date()}")

        # Check if grace period has expired
        if grace_end_str:
            try:
                grace_end_dt = datetime.fromisoformat(
                    grace_end_str.replace('Z', '+00:00')
                )
                if datetime.now(timezone.utc) > grace_end_dt:
                    patch.status['tier'] = 'community'
                    patch.status['maxDomains'] = COMMUNITY_MAX_DOMAINS
                    patch.status['message'] = (
                        'Grace period expired. Degraded to Community tier. '
                        'Please renew your license or check network connectivity.'
                    )
                    logger.warning("Grace period expired, degrading to Community tier")
            except (ValueError, AttributeError):
                pass


def do_community_phone_home(patch):
    """
    Send anonymous stats for community tier installations.
    No license key required - just sends usage stats.
    """
    stats = {
        'cluster_id': get_cluster_id(),
        'domain_count': count_domains(),
        'user_count': count_users(),
        'kubepanel_version': KUBEPANEL_VERSION,
        'kubernetes_version': get_k8s_version(),
        'cluster_nodes': count_cluster_nodes(),
        'timestamp': datetime.now(timezone.utc).isoformat()
    }

    try:
        response = requests.post(
            PHONE_HOME_URL,
            json=stats,  # Note: no customer_id
            timeout=30,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': f'KubePanel/{KUBEPANEL_VERSION}'
            }
        )
        response.raise_for_status()
        patch.status['lastPhoneHome'] = stats['timestamp']
        patch.status['domainsUsed'] = stats['domain_count']
        logger.info(f"Community phone-home successful: {stats['domain_count']} domains")

    except requests.RequestException as e:
        logger.debug(f"Community phone-home failed (non-critical): {e}")
        # Don't update failure counters for community - it's informational only


@kopf.on.create(LICENSE_GROUP, LICENSE_VERSION, LICENSE_PLURAL)
@kopf.on.update(LICENSE_GROUP, LICENSE_VERSION, LICENSE_PLURAL)
def on_license_change(spec, name, status, patch, **kwargs):
    """
    Handle License CR creation or update.

    Validates the license key signature and updates status.
    Triggers immediate phone-home on valid license.
    """
    logger.info(f"Processing License '{name}'...")

    license_key = spec.get('licenseKey', '')

    # No license key = community tier
    if not license_key:
        patch.status['valid'] = True
        patch.status['tier'] = 'community'
        patch.status['maxDomains'] = COMMUNITY_MAX_DOMAINS
        patch.status['domainsUsed'] = count_domains()
        patch.status['message'] = 'No license key configured - Community tier'
        patch.status['customerName'] = ''
        patch.status['customerId'] = ''
        patch.status['expiresAt'] = None
        patch.status['features'] = []
        logger.info(f"License '{name}' set to Community tier (no key)")
        # Initial community phone-home
        do_community_phone_home(patch)
        return

    # Verify license key signature
    payload = verify_license_key(license_key)

    if not payload:
        patch.status['valid'] = False
        patch.status['tier'] = 'community'
        patch.status['maxDomains'] = COMMUNITY_MAX_DOMAINS
        patch.status['domainsUsed'] = count_domains()
        patch.status['message'] = 'Invalid license key - signature verification failed'
        logger.warning(f"License '{name}' has invalid key")
        return

    # Check expiration
    exp_timestamp = payload.get('exp', 0)
    exp_datetime = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)

    if datetime.now(timezone.utc) > exp_datetime:
        patch.status['valid'] = False
        patch.status['tier'] = 'community'
        patch.status['maxDomains'] = COMMUNITY_MAX_DOMAINS
        patch.status['message'] = f'License expired on {exp_datetime.date()}'
        patch.status['expiresAt'] = exp_datetime.isoformat()
        logger.warning(f"License '{name}' is expired")
        return

    # License is valid - update status
    patch.status['valid'] = True
    patch.status['tier'] = payload.get('tier', 'community')
    patch.status['customerName'] = payload.get('customer_name', '')
    patch.status['customerId'] = payload.get('sub', '')
    patch.status['maxDomains'] = payload.get('max_domains', COMMUNITY_MAX_DOMAINS)
    patch.status['expiresAt'] = exp_datetime.isoformat()
    patch.status['features'] = payload.get('features', [])
    patch.status['message'] = f"License valid until {exp_datetime.date()}"

    logger.info(
        f"License '{name}' validated: tier={payload.get('tier')}, "
        f"customer={payload.get('customer_name')}, "
        f"expires={exp_datetime.date()}"
    )

    # Trigger immediate phone-home
    do_phone_home(patch, payload, status)


@kopf.on.resume(LICENSE_GROUP, LICENSE_VERSION, LICENSE_PLURAL)
def on_license_resume(spec, name, status, patch, **kwargs):
    """
    Handle operator restart with existing License CR.
    Triggers phone-home for community tier on operator restart.
    """
    logger.info(f"Resuming License '{name}' after operator restart...")

    license_key = spec.get('licenseKey', '')

    # For community tier, do phone-home on resume
    if not license_key:
        patch.status['domainsUsed'] = count_domains()
        do_community_phone_home(patch)
        logger.info(f"Community phone-home triggered on resume for '{name}'")
        return

    # For licensed tier, verify and phone-home
    payload = verify_license_key(license_key)
    if payload:
        do_phone_home(patch, payload, status)
        logger.info(f"Licensed phone-home triggered on resume for '{name}'")


@kopf.timer(LICENSE_GROUP, LICENSE_VERSION, LICENSE_PLURAL, interval=86400, initial_delay=300)
def on_license_timer(spec, name, status, patch, **kwargs):
    """
    Daily phone-home for stats and license refresh.

    Runs every 24 hours (first run delayed 5 min to avoid overlap with resume).
    """
    logger.debug(f"Daily license check for '{name}'...")

    license_key = spec.get('licenseKey', '')

    # No license key - community tier phone-home
    if not license_key:
        do_community_phone_home(patch)
        return

    # Verify license key
    payload = verify_license_key(license_key)

    if not payload:
        # Key became invalid (shouldn't happen, but handle it)
        patch.status['valid'] = False
        patch.status['tier'] = 'community'
        patch.status['maxDomains'] = COMMUNITY_MAX_DOMAINS
        patch.status['message'] = 'License key validation failed'
        return

    # Check expiration
    exp_timestamp = payload.get('exp', 0)
    exp_datetime = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)

    if datetime.now(timezone.utc) > exp_datetime:
        patch.status['valid'] = False
        patch.status['tier'] = 'community'
        patch.status['maxDomains'] = COMMUNITY_MAX_DOMAINS
        patch.status['message'] = f'License expired on {exp_datetime.date()}'
        logger.warning(f"License '{name}' has expired")
        return

    # Perform phone-home
    do_phone_home(patch, payload, status)


@kopf.on.delete(LICENSE_GROUP, LICENSE_VERSION, LICENSE_PLURAL)
def on_license_delete(spec, name, status, **kwargs):
    """Handle License CR deletion."""
    logger.info(f"License '{name}' deleted")
    return {'message': f"License '{name}' deleted"}


# =============================================================================
# Main entry point
# =============================================================================

if __name__ == '__main__':
    pass
