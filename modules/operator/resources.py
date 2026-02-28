"""
Resource Builders

Functions to create Kubernetes resources for a Domain.
Each builder returns a K8s resource object ready to be created.
"""

from kubernetes import client
from typing import Optional
import base64
import secrets
import string
import crypt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


# =============================================================================
# Image Configuration
# =============================================================================

# Sidecar images - each versioned independently
# Update these when releasing new sidecar versions
NGINX_IMAGE = "docker.io/kubepanel/nginx:v1.0.0"
SFTP_IMAGE = "docker.io/kubepanel/sftp:v1.0.0"
SSHGIT_IMAGE = "docker.io/kubepanel/sshgit:v1.0.0"
INIT_IMAGE = "docker.io/kubepanel/php_init:v1.0.0"
REDIS_IMAGE = "redis:7-alpine"

# NOTE: Workload app images are provided via the Domain CR spec.workload.image
# The operator no longer maintains a hardcoded mapping of versions to images.


# =============================================================================
# Helper Functions
# =============================================================================

def _generate_password(length: int = 24) -> str:
    """Generate a secure random password."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _generate_shadow_hash(password: str) -> str:
    """Generate a shadow-compatible password hash."""
    salt = crypt.mksalt(crypt.METHOD_SHA512)
    return crypt.crypt(password, salt)


def _generate_shadow_line(username: str, password_hash: str) -> str:
    """Generate a shadow file line for a user."""
    # Format: username:password_hash:lastchange:min:max:warn:inactive:expire:reserved
    # Using reasonable defaults
    return f"{username}:{password_hash}:19000:0:99999:7:::"


def _generate_ssh_keypair() -> tuple[str, str]:
    """
    Generate an SSH keypair.
    
    Returns:
        Tuple of (private_key_pem, public_key_openssh)
    """
    key = rsa.generate_private_key(
        backend=default_backend(),
        public_exponent=65537,
        key_size=2048
    )
    
    private_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()
    ).decode('utf-8')
    
    public_key = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH
    ).decode('utf-8')
    
    return private_key, public_key


def _generate_dkim_keypair() -> tuple[str, str, str]:
    """
    Generate a DKIM keypair.
    
    Returns:
        Tuple of (private_key_pem, public_key_base64, dns_txt_record)
    """
    key = rsa.generate_private_key(
        backend=default_backend(),
        public_exponent=65537,
        key_size=2048
    )
    
    private_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()
    ).decode('utf-8')
    
    # Get public key in PEM format, then extract the base64 part
    public_key_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')
    
    # Remove PEM headers and join lines
    lines = public_key_pem.strip().split('\n')
    public_key_base64 = ''.join(lines[1:-1])  # Skip first and last line (headers)
    
    # Format as DNS TXT record
    dns_txt_record = f"v=DKIM1; k=rsa; p={public_key_base64}"
    
    return private_key, public_key_base64, dns_txt_record


def _b64encode(value: str) -> str:
    """Base64 encode a string for K8s secret data."""
    return base64.b64encode(value.encode('utf-8')).decode('utf-8')


# REMOVED: get_php_image() - workload images are now provided via CR spec.workload.image


# =============================================================================
# Namespace & PVC Builders
# =============================================================================

def build_namespace(
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    owner: str
) -> client.V1Namespace:
    """Build a Namespace for a domain."""
    return client.V1Namespace(
        metadata=client.V1ObjectMeta(
            name=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'kubepanel.io/owner': owner,
                'app.kubernetes.io/managed-by': 'kubepanel-operator'
            },
            annotations={
                'kubepanel.io/domain-name': domain_name
            }
        )
    )


def build_pvc(
    namespace_name: str,
    domain_cr_name: str,
    storage_size: str = '5Gi',
    storage_class: Optional[str] = None
) -> client.V1PersistentVolumeClaim:
    """
    Build a PersistentVolumeClaim for domain storage.
    """
    pvc_spec = client.V1PersistentVolumeClaimSpec(
        access_modes=['ReadWriteOnce'],
        resources=client.V1VolumeResourceRequirements(
            requests={'storage': storage_size}
        )
    )
    
    if storage_class:
        pvc_spec.storage_class_name = storage_class
    
    return client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name='data',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'storage'
            }
        ),
        spec=pvc_spec
    )


def get_pvc_status(pvc: client.V1PersistentVolumeClaim) -> tuple[str, str, str]:
    """Get status info from a PVC."""
    phase = pvc.status.phase if pvc.status else 'Unknown'
    
    if phase == 'Bound':
        return ('True', 'Bound', f"PVC bound to {pvc.spec.volume_name or 'volume'}")
    elif phase == 'Pending':
        return ('False', 'Pending', 'PVC is waiting to be bound')
    elif phase == 'Lost':
        return ('False', 'Lost', 'PVC has lost its underlying volume')
    else:
        return ('Unknown', phase, f"PVC phase: {phase}")


# =============================================================================
# Secret Builders
# =============================================================================

def build_sftp_secret(
    namespace_name: str,
    domain_cr_name: str,
    username: str = 'webuser',
) -> tuple[client.V1Secret, dict]:
    """
    Build SFTP secret with generated SSH keypair, password, and shadow hash.
    """
    private_key, public_key = _generate_ssh_keypair()
    password = _generate_password(16)
    password_hash = _generate_shadow_hash(password)
    shadow_line = _generate_shadow_line(username, password_hash)
    
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name='sftp-credentials',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'sftp'
            }
        ),
        type='Opaque',
        data={
            'ssh-privatekey': _b64encode(private_key),
            'ssh-publickey': _b64encode(public_key),
            'password': _b64encode(password),
            'shadow': _b64encode(shadow_line),
        }
    )
    
    status_info = {
        'public_key': public_key,
        'username': username,
    }
    
    return secret, status_info


def build_database_secret(
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    db_host: str = 'mariadb.kubepanel-system.svc.cluster.local',
    db_port: int = 3306,
) -> tuple[client.V1Secret, dict]:
    """
    Build database credentials secret.
    """
    db_name = domain_name.replace('.', '_').replace('-', '_')[:32]
    db_user = db_name[:32]
    db_password = _generate_password(24)
    
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name='db-credentials',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'database'
            }
        ),
        type='Opaque',
        data={
            'host': _b64encode(db_host),
            'port': _b64encode(str(db_port)),
            'database': _b64encode(db_name),
            'username': _b64encode(db_user),
            'password': _b64encode(db_password),
        }
    )
    
    status_info = {
        'host': db_host,
        'port': db_port,
        'database': db_name,
        'username': db_user,
    }
    
    return secret, status_info


def build_dkim_secret(
    namespace_name: str,
    domain_cr_name: str,
    selector: str = 'default',
) -> tuple[client.V1Secret, dict]:
    """
    Build DKIM secret with generated keypair.
    """
    private_key, public_key_b64, dns_txt_record = _generate_dkim_keypair()
    
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name='dkim-credentials',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'email'
            }
        ),
        type='Opaque',
        data={
            'selector': _b64encode(selector),
            'private-key': _b64encode(private_key),
            'public-key': _b64encode(public_key_b64),
            'dns-txt-record': _b64encode(dns_txt_record),
        }
    )
    
    status_info = {
        'selector': selector,
        'public_key': public_key_b64,
        'dns_txt_record': dns_txt_record,
    }
    
    return secret, status_info


# =============================================================================
# ConfigMap Builders
# =============================================================================

# Base nginx config template - proxy_location is injected based on workload type
# ModSecurity is enabled via modsec_config placeholder (set when DomainWAF exists)
# FastCGI cache is enabled via cache_zone_config and cache_bypass_config placeholders
NGINX_CONFIG_BASE = """user webuser;
worker_processes auto;
pid /run/nginx.pid;

# Load nginx modules (including ModSecurity if installed)
include /etc/nginx/modules-enabled/*.conf;

events {{
    worker_connections 768;
}}

http {{
    sendfile on;
    tcp_nopush on;
    types_hash_max_size 2048;
    port_in_redirect off;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    # Real IP configuration for Kubernetes (get actual client IPs from proxy headers)
    real_ip_header X-Forwarded-For;
    set_real_ip_from 127.0.0.1;
    set_real_ip_from 10.0.0.0/8;
    real_ip_recursive on;

    access_log /var/log/nginx/access.log;
    error_log /var/log/nginx/error.log;

{modsec_config}
{cache_zone_config}
    gzip on;

    server {{
        listen 8080;
        server_name {server_names};
        root {document_root};
        index {index_files};

        client_max_body_size {client_max_body_size};

{cache_bypass_config}
{proxy_location}

        location ~ /\\.ht {{
            deny all;
        }}

{custom_config}
    }}
}}
"""


def _generate_proxy_location(
    proxy_mode: str,
    app_port: int,
    document_root: str,
    cache_enabled: bool = False,
    cache_valid_time: str = '10m',
) -> str:
    """
    Generate nginx location block based on proxy mode.

    Args:
        proxy_mode: 'fastcgi', 'http', or 'uwsgi'
        app_port: Port the app container listens on
        document_root: Document root path for static file serving
        cache_enabled: Enable FastCGI caching
        cache_valid_time: Cache validity time (e.g., '10m', '1h')

    Returns:
        Nginx location configuration block
    """
    if proxy_mode == 'fastcgi':
        # FastCGI mode (PHP-FPM)
        if cache_enabled:
            cache_directives = f"""
            # FastCGI Cache
            fastcgi_cache DOMAIN_CACHE;
            fastcgi_cache_valid 200 301 302 {cache_valid_time};
            fastcgi_cache_bypass $skip_cache;
            fastcgi_no_cache $skip_cache;
            fastcgi_cache_lock on;
            fastcgi_cache_use_stale error timeout updating invalid_header http_500 http_503;
            fastcgi_cache_background_update on;
            fastcgi_ignore_headers Cache-Control Expires Set-Cookie;
            add_header X-FastCGI-Cache $upstream_cache_status;"""
        else:
            cache_directives = ""

        return f"""        location / {{
            try_files $uri $uri/ /index.php?$args;
        }}

        location ~ \\.php$ {{
            fastcgi_pass 127.0.0.1:{app_port};
            fastcgi_index index.php;
            fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
            include fastcgi_params;
            fastcgi_param HTTPS on;{cache_directives}
        }}"""

    elif proxy_mode == 'http':
        # HTTP reverse proxy (Python/Node.js/etc.)
        return f"""        location / {{
            proxy_pass http://127.0.0.1:{app_port};
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection 'upgrade';
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_cache_bypass $http_upgrade;
            proxy_read_timeout 300s;
            proxy_connect_timeout 75s;
        }}

        location /static/ {{
            alias {document_root}/static/;
            expires 30d;
        }}"""

    elif proxy_mode == 'uwsgi':
        # uWSGI protocol (Python WSGI apps)
        return f"""        location / {{
            uwsgi_pass 127.0.0.1:{app_port};
            include uwsgi_params;
            uwsgi_param Host $host;
            uwsgi_param X-Real-IP $remote_addr;
            uwsgi_param X-Forwarded-For $proxy_add_x_forwarded_for;
            uwsgi_param X-Forwarded-Proto $scheme;
        }}

        location /static/ {{
            alias {document_root}/static/;
            expires 30d;
        }}"""

    else:
        # Default to fastcgi for backward compatibility
        return _generate_proxy_location('fastcgi', app_port, document_root, cache_enabled, cache_valid_time)


def _generate_cache_zone_config(inactive_time: str = '60m') -> str:
    """Generate FastCGI cache zone configuration for http block."""
    return f"""    # FastCGI Cache Zone
    fastcgi_cache_path /var/cache/nginx levels=1:2
        keys_zone=DOMAIN_CACHE:10m
        inactive={inactive_time}
        use_temp_path=off;
    fastcgi_cache_key "$scheme$request_method$host$request_uri";
"""


def _generate_cache_bypass_config(bypass_uris: list = None) -> str:
    """Generate cache bypass rules for server block."""
    # Build custom bypass rules from user-provided URIs
    custom_bypass_rules = ""
    if bypass_uris:
        uri_patterns = "|".join(uri.strip().replace("/", "\\/") for uri in bypass_uris if uri.strip())
        if uri_patterns:
            custom_bypass_rules = f"""
        # Custom bypass URIs
        if ($request_uri ~* "({uri_patterns})") {{
            set $skip_cache 1;
        }}"""

    return f"""        # Cache bypass conditions
        set $skip_cache 0;

        # POST requests - never cache
        if ($request_method = POST) {{
            set $skip_cache 1;
        }}

        # URLs with query strings - don't cache (except for known static params)
        if ($query_string != "") {{
            set $skip_cache 1;
        }}

        # WordPress: Don't cache admin, login, or dynamic pages
        if ($request_uri ~* "/wp-admin/|/wp-login.php|/xmlrpc.php|wp-.*.php|/feed/") {{
            set $skip_cache 1;
        }}

        # WordPress: Don't cache for logged in users or password-protected posts
        if ($http_cookie ~* "wordpress_logged_in|wp-postpass") {{
            set $skip_cache 1;
        }}

        # WooCommerce: Don't cache cart, checkout, or account pages
        if ($request_uri ~* "/cart/|/checkout/|/my-account/|/addons/|add-to-cart") {{
            set $skip_cache 1;
        }}

        # WooCommerce: Don't cache if cart has items
        if ($http_cookie ~* "woocommerce_items_in_cart|woocommerce_cart_hash") {{
            set $skip_cache 1;
        }}{custom_bypass_rules}
"""


def build_nginx_configmap(
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    aliases: list[str] = None,
    document_root: str = '/usr/share/nginx/html',
    client_max_body_size: str = '64m',
    custom_config: str = '',
    www_redirect: str = 'none',
    # New workload parameters
    proxy_mode: str = 'fastcgi',
    app_port: int = 9001,
    workload_type: str = 'php',
    # ModSecurity/DomainWAF parameter
    modsec_enabled: bool = False,
    # FastCGI cache parameters
    cache_enabled: bool = False,
    cache_inactive_time: str = '60m',
    cache_valid_time: str = '10m',
    cache_bypass_uris: list = None,
) -> client.V1ConfigMap:
    """
    Build nginx ConfigMap with generated config.

    Args:
        www_redirect: 'none', 'www-to-root', or 'root-to-www'
        proxy_mode: 'fastcgi', 'http', or 'uwsgi'
        app_port: Port the app container listens on
        workload_type: Workload type slug for index file selection
        modsec_enabled: Enable ModSecurity WAF (reads rules from /etc/nginx/modsec/rules.conf)
        cache_enabled: Enable FastCGI caching for PHP responses
        cache_inactive_time: Remove cached items not accessed in this time (e.g., '60m')
        cache_valid_time: Cache validity time for responses (e.g., '10m')
        cache_bypass_uris: Additional URI patterns to bypass cache
    """
    # Build server_names based on www_redirect setting
    www_domain = f'www.{domain_name}'

    if www_redirect == 'www-to-root':
        # Main server handles root domain, www redirects to root
        server_names = [domain_name]
        redirect_from = www_domain
        redirect_to = domain_name
    elif www_redirect == 'root-to-www':
        # Main server handles www domain, root redirects to www
        server_names = [www_domain]
        redirect_from = domain_name
        redirect_to = www_domain
    else:
        # No redirect - serve both
        server_names = [domain_name, www_domain]
        redirect_from = None
        redirect_to = None

    # Add aliases to main server
    if aliases:
        server_names.extend(aliases)
    server_names_str = ' '.join(server_names)

    # Select index files based on workload type
    if workload_type == 'php':
        index_files = 'index.php index.html index.htm'
    else:
        index_files = 'index.html index.htm'

    # Indent custom config (sanitize Windows line endings first)
    if custom_config:
        # Remove carriage returns (Windows line endings) to prevent nginx parse errors
        sanitized_config = custom_config.replace('\r', '').strip()
        custom_lines = sanitized_config.split('\n')
        custom_config_indented = '\n'.join(f'        {line}' for line in custom_lines)
    else:
        custom_config_indented = ''

    # Generate proxy location block based on mode (with cache support for fastcgi)
    proxy_location = _generate_proxy_location(
        proxy_mode, app_port, document_root,
        cache_enabled=cache_enabled and proxy_mode == 'fastcgi',
        cache_valid_time=cache_valid_time,
    )

    # Generate ModSecurity config block if enabled
    if modsec_enabled:
        modsec_config = """    # ModSecurity WAF enabled
    modsecurity on;
    modsecurity_rules_file /etc/nginx/modsec/main.conf;
"""
    else:
        modsec_config = ""

    # Generate FastCGI cache config if enabled (only for fastcgi mode)
    if cache_enabled and proxy_mode == 'fastcgi':
        cache_zone_config = _generate_cache_zone_config(cache_inactive_time)
        cache_bypass_config = _generate_cache_bypass_config(cache_bypass_uris)
    else:
        cache_zone_config = ""
        cache_bypass_config = ""

    # Generate main config
    nginx_conf = NGINX_CONFIG_BASE.format(
        server_names=server_names_str,
        document_root=document_root,
        index_files=index_files,
        client_max_body_size=client_max_body_size,
        proxy_location=proxy_location,
        custom_config=custom_config_indented,
        modsec_config=modsec_config,
        cache_zone_config=cache_zone_config,
        cache_bypass_config=cache_bypass_config,
    )

    # Add redirect server block if needed
    if redirect_from and redirect_to:
        redirect_block = f"""
    server {{
        listen 8080;
        server_name {redirect_from};
        return 301 https://{redirect_to}$request_uri;
    }}
"""
        # Insert redirect block before the closing }} of http block
        nginx_conf = nginx_conf.rstrip().rstrip('}').rstrip() + redirect_block + '\n}\n'

    return client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name='nginx-config',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'webserver'
            }
        ),
        data={
            'nginx.conf': nginx_conf,
        }
    )


def build_app_configmap(
    namespace_name: str,
    domain_cr_name: str,
    workload_type: str = 'php',
    # PHP-specific settings
    memory_limit: str = '256M',
    max_execution_time: int = 30,
    upload_max_filesize: str = '64M',
    post_max_size: str = '64M',
    custom_config: str = '',
    # PHP-FPM pool settings
    fpm_max_children: int = 25,
    fpm_process_idle_timeout: int = 30,
) -> client.V1ConfigMap:
    """
    Build ConfigMap for app configuration.

    For PHP: Creates a custom php.ini file and www.conf (FPM pool config)
    For other types: Creates type-specific config (placeholder for now)
    """
    # Sanitize custom config (remove Windows line endings)
    sanitized_custom_config = custom_config.replace('\r', '') if custom_config else ''

    data = {}

    if workload_type == 'php':
        # PHP.ini configuration
        config_content = f"""; KubePanel PHP Configuration
; Domain: {domain_cr_name}

memory_limit = {memory_limit}
max_execution_time = {max_execution_time}
upload_max_filesize = {upload_max_filesize}
post_max_size = {post_max_size}

; Custom configuration
{sanitized_custom_config}
"""
        data['kubepanel.ini'] = config_content

        # PHP-FPM pool configuration (www.conf)
        fpm_config = f"""[php]
; KubePanel PHP-FPM Pool Configuration
; Domain: {domain_cr_name}

user = webuser
group = webgroup
listen.mode = 0660
listen = 9001

pm = ondemand
pm.max_children = {fpm_max_children}
pm.process_idle_timeout = {fpm_process_idle_timeout}s
"""
        data['www.conf'] = fpm_config
    else:
        # Generic config for non-PHP workloads
        config_content = f"""# KubePanel App Configuration
# Domain: {domain_cr_name}
# Workload Type: {workload_type}

{sanitized_custom_config}
"""
        data['kubepanel.conf'] = config_content

    return client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name='app-config',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'kubepanel.io/workload-type': workload_type,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'app'
            },
        ),
        data=data,
    )


# Backward compatibility alias
def build_php_configmap(*args, **kwargs):
    """DEPRECATED: Use build_app_configmap instead."""
    kwargs.setdefault('workload_type', 'php')
    return build_app_configmap(*args, **kwargs)


# =============================================================================
# Deployment Builder
# =============================================================================

def build_deployment(
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    # Workload configuration (replaces php_version)
    workload_type: str,
    workload_version: str,
    workload_image: str,
    workload_port: int = 9000,
    workload_command: list = None,
    workload_args: list = None,
    workload_env: list = None,
    # Resource limits
    cpu_limit: str = '500m',
    memory_limit: str = '256Mi',
    cpu_request: str = '32m',
    memory_request: str = '64Mi',
    # Optional features
    wp_preinstall: bool = False,
    # ModSecurity/DomainWAF
    modsec_enabled: bool = False,
    # FastCGI cache
    cache_enabled: bool = False,
    cache_size: str = '512Mi',
    # Node scheduling preferences
    preferred_nodes: list = None,
    # Container timezone
    timezone: str = 'UTC',
    # Container options
    sftp_type: str = 'standard',
    redis_enabled: bool = False,
) -> client.V1Deployment:
    """
    Build the main deployment with php-init, app, nginx, and sftp containers.

    The app container is generic - determined by workload_type and workload_image.
    For PHP: workload_type='php', workload_port=9001
    For Python: workload_type='python', workload_port=8000
    For Node.js: workload_type='nodejs', workload_port=3000
    """
    # Labels for pod
    labels = {
        'kubepanel.io/domain': domain_cr_name,
        'kubepanel.io/workload-type': workload_type,
        'app.kubernetes.io/managed-by': 'kubepanel-operator',
        'app.kubernetes.io/name': 'web',
        'app': 'web',
    }

    # Init container - iptables NAT rules + optional WP install (PHP only)
    init_env = [
        client.V1EnvVar(
            name='WP_PREINSTALL',
            value='True' if (wp_preinstall and workload_type == 'php') else 'False'
        ),
    ]
    init_container = client.V1Container(
        name='php-init',
        image=INIT_IMAGE,
        image_pull_policy='Always',
        env=init_env,
        security_context=client.V1SecurityContext(
            privileged=True,
            capabilities=client.V1Capabilities(add=['NET_ADMIN', 'SYS_ADMIN']),
        ),
        volume_mounts=[
            client.V1VolumeMount(name='data', mount_path='/usr/share/nginx/html'),
        ],
    )

    # App container (generic - was PHP-FPM, now supports any workload type)
    app_volume_mounts = [
        client.V1VolumeMount(name='data', mount_path='/usr/share/nginx/html'),
    ]

    # Add type-specific config volume mount (only for PHP currently)
    if workload_type == 'php':
        app_volume_mounts.append(
            client.V1VolumeMount(
                name='app-config',
                mount_path='/etc/php-custom/kubepanel.ini',
                sub_path='kubepanel.ini',
            )
        )
        # PHP-FPM pool configuration (www.conf)
        # Use dynamic path based on PHP version (e.g., /etc/php/8.2/fpm/pool.d/www.conf)
        fpm_config_path = f'/etc/php/{workload_version}/fpm/pool.d/www.conf'
        app_volume_mounts.append(
            client.V1VolumeMount(
                name='app-config',
                mount_path=fpm_config_path,
                sub_path='www.conf',
            )
        )

    # Build environment variables
    # TZ env var for timezone (applies to all containers)
    tz_env = client.V1EnvVar(name='TZ', value=timezone)

    app_env = [tz_env]  # Start with TZ
    if workload_env:
        for env_item in workload_env:
            app_env.append(client.V1EnvVar(
                name=env_item.get('name', ''),
                value=env_item.get('value', '')
            ))

    app_container = client.V1Container(
        name='app',  # Renamed from 'php' to 'app' for generic workload support
        image=workload_image,
        image_pull_policy='Always',
        ports=[client.V1ContainerPort(container_port=workload_port)],
        resources=client.V1ResourceRequirements(
            limits={'cpu': cpu_limit, 'memory': memory_limit},
            requests={'cpu': cpu_request, 'memory': memory_request},
        ),
        volume_mounts=app_volume_mounts,
        liveness_probe=client.V1Probe(
            tcp_socket=client.V1TCPSocketAction(port=workload_port),
            initial_delay_seconds=15,
            period_seconds=10,
            timeout_seconds=5,
            failure_threshold=3,
        ),
        readiness_probe=client.V1Probe(
            tcp_socket=client.V1TCPSocketAction(port=workload_port),
            initial_delay_seconds=5,
            period_seconds=5,
            timeout_seconds=3,
            failure_threshold=3,
        ),
    )

    # Add optional command/args
    if workload_command:
        app_container.command = workload_command
    if workload_args:
        app_container.args = workload_args
    if app_env:
        app_container.env = app_env
    
    # Nginx container volume mounts
    nginx_volume_mounts = [
        client.V1VolumeMount(name='data', mount_path='/usr/share/nginx/html'),
        client.V1VolumeMount(
            name='nginx-config',
            mount_path='/etc/nginx/nginx.conf',
            sub_path='nginx.conf',
        ),
    ]

    # Add ModSecurity rules volume mount if enabled
    if modsec_enabled:
        nginx_volume_mounts.append(
            client.V1VolumeMount(
                name='modsec-rules',
                mount_path='/etc/nginx/modsec/rules.conf',
                sub_path='rules.conf',
            )
        )

    # Add cache volume mount if enabled
    if cache_enabled:
        nginx_volume_mounts.append(
            client.V1VolumeMount(
                name='nginx-cache',
                mount_path='/var/cache/nginx',
            )
        )

    # Nginx container
    nginx_container = client.V1Container(
        name='nginx',
        image=NGINX_IMAGE,
        image_pull_policy='Always',
        ports=[client.V1ContainerPort(container_port=8080)],
        env=[tz_env],  # Set timezone
        resources=client.V1ResourceRequirements(
            limits={'cpu': cpu_limit, 'memory': memory_limit},
            requests={'cpu': cpu_request, 'memory': memory_request},
        ),
        volume_mounts=nginx_volume_mounts,
        liveness_probe=client.V1Probe(
            tcp_socket=client.V1TCPSocketAction(port=8080),
            initial_delay_seconds=15,
            period_seconds=10,
            timeout_seconds=5,
            failure_threshold=3,
        ),
        readiness_probe=client.V1Probe(
            tcp_socket=client.V1TCPSocketAction(port=8080),
            initial_delay_seconds=5,
            period_seconds=5,
            timeout_seconds=3,
            failure_threshold=3,
        ),
    )
    
    # SFTP container (use sshgit image if SSH access is enabled)
    sftp_image = SSHGIT_IMAGE if sftp_type == 'sshgit' else SFTP_IMAGE
    sftp_container = client.V1Container(
        name='sftp',
        image=sftp_image,
        image_pull_policy='Always',
        ports=[client.V1ContainerPort(container_port=22)],
        env=[tz_env],  # Set timezone
        resources=client.V1ResourceRequirements(
            limits={'cpu': '100m', 'memory': '64Mi'},
            requests={'cpu': '10m', 'memory': '32Mi'},
        ),
        volume_mounts=[
            client.V1VolumeMount(name='data', mount_path='/home/webuser/html'),
            client.V1VolumeMount(
                name='sftp-secrets',
                mount_path='/etc/sftp-secrets',
                read_only=True,
            ),
        ],
    )

    # Redis container (optional)
    redis_container = None
    if redis_enabled:
        redis_container = client.V1Container(
            name='redis',
            image=REDIS_IMAGE,
            image_pull_policy='IfNotPresent',
            ports=[client.V1ContainerPort(container_port=6379)],
            env=[tz_env],
            resources=client.V1ResourceRequirements(
                limits={'cpu': '200m', 'memory': '128Mi'},
                requests={'cpu': '50m', 'memory': '64Mi'},
            ),
            volume_mounts=[
                client.V1VolumeMount(name='redis-data', mount_path='/data'),
            ],
            liveness_probe=client.V1Probe(
                tcp_socket=client.V1TCPSocketAction(port=6379),
                initial_delay_seconds=10,
                period_seconds=10,
            ),
            readiness_probe=client.V1Probe(
                tcp_socket=client.V1TCPSocketAction(port=6379),
                initial_delay_seconds=5,
                period_seconds=5,
            ),
        )

    # Volumes
    volumes = [
        client.V1Volume(
            name='data',
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name='data',
            ),
        ),
        client.V1Volume(
            name='nginx-config',
            config_map=client.V1ConfigMapVolumeSource(
                name='nginx-config',
            ),
        ),
        client.V1Volume(
            name='app-config',
            config_map=client.V1ConfigMapVolumeSource(
                name='app-config',
            ),
        ),
        client.V1Volume(
            name='sftp-secrets',
            secret=client.V1SecretVolumeSource(
                secret_name='sftp-credentials',
                items=[
                    client.V1KeyToPath(key='password', path='password', mode=0o400),
                    client.V1KeyToPath(key='ssh-publickey', path='ssh-publickey', mode=0o400),
                ],
            ),
        ),
    ]

    # Add ModSecurity rules volume if enabled
    if modsec_enabled:
        volumes.append(
            client.V1Volume(
                name='modsec-rules',
                config_map=client.V1ConfigMapVolumeSource(
                    name='modsec-rules',
                    optional=True,  # Don't fail if ConfigMap doesn't exist yet
                ),
            )
        )

    # Add cache volume if enabled (emptyDir for ephemeral cache storage)
    if cache_enabled:
        volumes.append(
            client.V1Volume(
                name='nginx-cache',
                empty_dir=client.V1EmptyDirVolumeSource(
                    size_limit=cache_size,  # e.g., '512Mi', '1Gi'
                ),
            )
        )

    # Add Redis volume if enabled (emptyDir for ephemeral cache)
    if redis_enabled:
        volumes.append(
            client.V1Volume(
                name='redis-data',
                empty_dir=client.V1EmptyDirVolumeSource(),
            )
        )

    # Pod spec with affinity for zero-downtime rolling updates
    # RWO PVCs can be mounted by multiple pods on the SAME node
    # Pod affinity ensures new pod schedules on same node during updates
    pod_affinity = client.V1PodAffinity(
        preferred_during_scheduling_ignored_during_execution=[
            client.V1WeightedPodAffinityTerm(
                weight=100,
                pod_affinity_term=client.V1PodAffinityTerm(
                    label_selector=client.V1LabelSelector(
                        match_labels={
                            'app': 'web',
                            'kubepanel.io/domain': domain_cr_name,
                        }
                    ),
                    topology_key='kubernetes.io/hostname',
                ),
            )
        ]
    )

    # Node affinity for preferred servers (soft constraint)
    node_affinity = None
    if preferred_nodes and len(preferred_nodes) > 0:
        node_affinity = client.V1NodeAffinity(
            preferred_during_scheduling_ignored_during_execution=[
                client.V1PreferredSchedulingTerm(
                    weight=100,
                    preference=client.V1NodeSelectorTerm(
                        match_expressions=[
                            client.V1NodeSelectorRequirement(
                                key='kubernetes.io/hostname',
                                operator='In',
                                values=preferred_nodes,
                            )
                        ]
                    )
                )
            ]
        )

    # Build containers list (Redis is optional)
    containers = [app_container, nginx_container, sftp_container]
    if redis_container:
        containers.append(redis_container)

    pod_spec = client.V1PodSpec(
        affinity=client.V1Affinity(
            pod_affinity=pod_affinity,
            node_affinity=node_affinity,
        ),
        init_containers=[init_container],
        containers=containers,
        volumes=volumes,
    )
    
    # Deployment
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name='web',
            namespace=namespace_name,
            labels=labels,
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            # Default RollingUpdate strategy - pod affinity ensures same-node scheduling
            selector=client.V1LabelSelector(match_labels={'app': 'web'}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=pod_spec,
            ),
        ),
    )


def get_deployment_status(deployment: client.V1Deployment) -> tuple[str, str, str]:
    """Get status info from a Deployment."""
    if not deployment.status:
        return ('Unknown', 'Unknown', 'Deployment status unknown')
    
    available = deployment.status.available_replicas or 0
    desired = deployment.spec.replicas or 1
    
    if available >= desired:
        return ('True', 'Available', f'Deployment has {available}/{desired} replicas available')
    elif available > 0:
        return ('False', 'Degraded', f'Deployment has {available}/{desired} replicas available')
    else:
        return ('False', 'Unavailable', 'Deployment has no available replicas')


# =============================================================================
# Service Builder
# =============================================================================

def build_service(
    namespace_name: str,
    domain_cr_name: str,
) -> client.V1Service:
    """
    Build the Service to expose nginx (ClusterIP for ingress).
    """
    return client.V1Service(
        metadata=client.V1ObjectMeta(
            name='web',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'webserver'
            }
        ),
        spec=client.V1ServiceSpec(
            selector={'app': 'web'},
            ports=[
                client.V1ServicePort(
                    name='http',
                    port=80,
                    target_port=8080,
                    protocol='TCP',
                ),
            ],
            type='ClusterIP',
        ),
    )


def build_sftp_service(
    namespace_name: str,
    domain_cr_name: str,
    node_port: Optional[int] = None,
) -> client.V1Service:
    """
    Build the SFTP Service (NodePort for external access).
    """
    ports = [
        client.V1ServicePort(
            name='sftp',
            port=22,
            target_port=22,
            protocol='TCP',
            node_port=node_port,
        ),
    ]
    
    return client.V1Service(
        metadata=client.V1ObjectMeta(
            name='sftp',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'sftp'
            }
        ),
        spec=client.V1ServiceSpec(
            selector={'app': 'web'},
            ports=ports,
            type='NodePort',
        ),
    )


# =============================================================================
# Ingress Builder
# =============================================================================

def build_ingress(
    namespace_name: str,
    domain_cr_name: str,
    domain_name: str,
    aliases: list[str] = None,
    ssl_redirect: bool = True,
    www_redirect: str = 'none',
    ingress_class: str = 'public',
    cluster_issuer: str = 'letsencrypt-prod',
) -> client.V1Ingress:
    """
    Build Ingress for HTTP/HTTPS access with TLS.

    Args:
        www_redirect: 'none', 'www-to-root', or 'root-to-www'
                      Determines whether www domain is included in Ingress hosts
    """
    # All hosts (domain + www + aliases)
    www_domain = f'www.{domain_name}'
    all_hosts = [domain_name, www_domain]  # Always include both for Ingress routing
    if aliases:
        all_hosts.extend(aliases)
    
    # TLS configuration - separate secret per domain/alias for cert-manager
    # Primary domain + www share one certificate
    tls = [
        client.V1IngressTLS(
            hosts=[domain_name, www_domain],
            secret_name=f'{domain_cr_name}-tls',
        )
    ]
    # Each alias gets its own certificate
    if aliases:
        for alias in aliases:
            alias_cr_name = alias.replace('.', '-')
            tls.append(
                client.V1IngressTLS(
                    hosts=[alias],
                    secret_name=f'{alias_cr_name}-tls',
                )
            )
    
    # Rules - one rule per host, all pointing to the web service
    rules = []
    for host in all_hosts:
        rules.append(
            client.V1IngressRule(
                host=host,
                http=client.V1HTTPIngressRuleValue(
                    paths=[
                        client.V1HTTPIngressPath(
                            path='/',
                            path_type='Prefix',
                            backend=client.V1IngressBackend(
                                service=client.V1IngressServiceBackend(
                                    name='web',
                                    port=client.V1ServiceBackendPort(number=80),
                                ),
                            ),
                        ),
                    ],
                ),
            )
        )
    
    # Annotations
    annotations = {
        'cert-manager.io/cluster-issuer': cluster_issuer,
    }
    
    if ssl_redirect:
        annotations['nginx.ingress.kubernetes.io/ssl-redirect'] = 'true'
    else:
        annotations['nginx.ingress.kubernetes.io/ssl-redirect'] = 'false'
    
    return client.V1Ingress(
        metadata=client.V1ObjectMeta(
            name='web',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'ingress'
            },
            annotations=annotations,
        ),
        spec=client.V1IngressSpec(
            ingress_class_name=ingress_class,
            tls=tls,
            rules=rules,
        ),
    )


def get_ingress_status(ingress: client.V1Ingress) -> tuple[str, str, str]:
    """Get status info from an Ingress."""
    if not ingress.status or not ingress.status.load_balancer:
        return ('Unknown', 'Pending', 'Ingress is waiting for load balancer')

    ingress_ips = ingress.status.load_balancer.ingress or []

    if ingress_ips:
        # Has at least one IP/hostname assigned
        addresses = [i.ip or i.hostname for i in ingress_ips if i.ip or i.hostname]
        if addresses:
            return ('True', 'Ready', f'Ingress ready at {", ".join(addresses)}')

    return ('Unknown', 'Pending', 'Ingress is waiting for IP assignment')


# =============================================================================
# Backup Resource Builders
# =============================================================================

def build_backup_pvc(
    namespace_name: str,
    domain_cr_name: str,
    storage_size: str = '10Gi',
    storage_class: Optional[str] = None
) -> client.V1PersistentVolumeClaim:
    """
    Build a PersistentVolumeClaim for backup storage.
    """
    pvc_spec = client.V1PersistentVolumeClaimSpec(
        access_modes=['ReadWriteOnce'],
        resources=client.V1VolumeResourceRequirements(
            requests={'storage': storage_size}
        )
    )

    if storage_class:
        pvc_spec.storage_class_name = storage_class

    return client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name='backup',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'backup'
            }
        ),
        spec=pvc_spec
    )


def build_backup_service_account(
    namespace_name: str,
    domain_cr_name: str,
) -> client.V1ServiceAccount:
    """
    Build a ServiceAccount for backup jobs.
    """
    return client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(
            name='kubepanel-backup',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'backup'
            }
        )
    )


def build_backup_role_binding(
    namespace_name: str,
    domain_cr_name: str,
) -> client.V1RoleBinding:
    """
    Build a RoleBinding that grants the backup ServiceAccount permissions
    from the kubepanel-backup ClusterRole in this namespace only.
    """
    return client.V1RoleBinding(
        metadata=client.V1ObjectMeta(
            name='kubepanel-backup',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'backup'
            }
        ),
        role_ref=client.V1RoleRef(
            api_group='rbac.authorization.k8s.io',
            kind='ClusterRole',
            name='kubepanel-backup',
        ),
        subjects=[
            client.RbacV1Subject(
                kind='ServiceAccount',
                name='kubepanel-backup',
                namespace=namespace_name,
            )
        ]
    )


def build_backup_credentials_secret(
    namespace_name: str,
    domain_cr_name: str,
    mariadb_root_password: str,
) -> client.V1Secret:
    """
    Build a secret containing credentials needed for backups.
    This is a copy of credentials from kubepanel namespace for backup job access.
    """
    return client.V1Secret(
        metadata=client.V1ObjectMeta(
            name='backup-credentials',
            namespace=namespace_name,
            labels={
                'kubepanel.io/domain': domain_cr_name,
                'app.kubernetes.io/managed-by': 'kubepanel-operator',
                'app.kubernetes.io/component': 'backup'
            }
        ),
        type='Opaque',
        data={
            'mariadb-root-password': _b64encode(mariadb_root_password),
        }
    )
