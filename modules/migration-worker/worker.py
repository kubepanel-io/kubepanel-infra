#!/usr/bin/env python
"""
cPanel Migration Worker

This script runs in a Kubernetes Job to process a migration batch.
It receives backups from cPanel via SFTP push, transforms them, and imports them into KubePanel.

Environment variables:
    BATCH_ID - The MigrationBatch ID to process
    DATABASE_URL - Connection URL for KubePanel database
    SFTP_HOST - Public hostname for cPanel to push backups
    SFTP_PORT - NodePort for SFTP service
    SFTP_USER - SFTP username
    SFTP_PASSWORD - SFTP password
"""

import os
import sys
import logging
import tempfile
import shutil
import glob
import time
from datetime import datetime
from typing import Optional

# Set up Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'kubepanel.settings')
import django
django.setup()

from django.utils import timezone
from dashboard.models import MigrationBatch, MigrationDomain, Domain, Package, MailUser
from dashboard.services.cpanel_api import CPanelClient, CPanelAPIError, CPanelConnectionError
from dashboard.services.cpanel_parser import CPanelBackupParser, CPanelParserError
from dashboard.services.cpanel_transformer import CPanelTransformer, CPanelTransformerError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def decrypt_token(batch: MigrationBatch) -> str:
    """
    Get the decrypted API token from the batch.

    Uses the model's get_api_token method which handles decryption.
    """
    return batch.get_api_token()


def update_domain_status(
    domain: MigrationDomain,
    status: str,
    progress: int = None,
    message: str = '',
    error: str = ''
):
    """Update the status of a migration domain."""
    domain.status = status
    if progress is not None:
        domain.progress_percent = progress
    domain.progress_message = message
    if error:
        domain.error_message = error
    domain.save()
    logger.info(f"{domain.source_domain}: {status} ({progress}%) - {message}")


def update_batch_backup_status(batch: MigrationBatch, message: str, progress: int = 0):
    """Update the backup status on the batch."""
    batch.backup_status = message
    batch.backup_progress = progress
    batch.save(update_fields=['backup_status', 'backup_progress'])


def wait_for_backup_file(directory: str, timeout: int = 3600) -> Optional[str]:
    """
    Poll for backup file arrival via SFTP.

    cPanel creates backup files with pattern: backup-M.D.YYYY_HH-MM-SS_username.tar.gz

    Args:
        directory: Directory to watch for backup files
        timeout: Maximum time to wait in seconds

    Returns:
        Path to backup file, or None if timeout
    """
    start_time = time.time()
    logger.info(f"Waiting for backup file in {directory} (timeout: {timeout}s)")

    while time.time() - start_time < timeout:
        # Look for tar.gz files
        files = glob.glob(os.path.join(directory, "*.tar.gz"))
        if files:
            # Return the newest file
            newest = max(files, key=os.path.getctime)
            # Verify file is complete (not still being written)
            size1 = os.path.getsize(newest)
            time.sleep(3)
            size2 = os.path.getsize(newest)
            if size1 == size2 and size1 > 0:
                logger.info(f"Backup file arrived: {newest} ({size2} bytes)")
                return newest
            logger.info(f"File still being written: {newest} ({size1} -> {size2} bytes)")
        time.sleep(10)  # Check every 10 seconds

    logger.warning(f"Backup file did not arrive within {timeout}s")
    return None


def wait_for_sftp_service(host: str, port: int, timeout: int = 120) -> bool:
    """
    Wait for SFTP service to be externally reachable.

    This ensures the K8s Service has endpoints before triggering cPanel.
    The worker pod must be Ready for the Service to route traffic.

    Args:
        host: SFTP hostname (NodePort external address)
        port: SFTP port (NodePort)
        timeout: Maximum time to wait in seconds

    Returns:
        True if reachable

    Raises:
        Exception: If not reachable within timeout
    """
    import socket
    start_time = time.time()
    logger.info(f"Waiting for SFTP service at {host}:{port} to be reachable...")

    while time.time() - start_time < timeout:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host, port))
            sock.close()
            logger.info(f"SFTP service at {host}:{port} is reachable")
            return True
        except (socket.error, socket.timeout) as e:
            elapsed = int(time.time() - start_time)
            logger.debug(f"SFTP not ready yet ({elapsed}s): {e}")
            time.sleep(3)

    raise Exception(f"SFTP service at {host}:{port} not reachable after {timeout}s")


def trigger_sftp_backup(client, username: str, backup_dir: str) -> str:
    """
    Trigger cPanel to push backup to SFTP and wait for arrival.

    Args:
        client: CPanelClient instance
        username: cPanel username to backup
        backup_dir: Local directory where SFTP receives backups

    Returns:
        Path to downloaded backup file

    Raises:
        Exception: If backup fails or times out
    """
    sftp_host = os.environ.get('SFTP_HOST')
    sftp_port = int(os.environ.get('SFTP_PORT', 22))
    sftp_user = os.environ.get('SFTP_USER')
    sftp_password = os.environ.get('SFTP_PASSWORD')

    if not all([sftp_host, sftp_port, sftp_user, sftp_password]):
        raise Exception("SFTP environment variables not set")

    # Wait for SFTP service to be reachable (K8s Service needs pod to be Ready)
    wait_for_sftp_service(sftp_host, sftp_port)

    # Create destination directory for this user's backup
    # Must be writable by group (gid 1000) for SSH user to write files via SCP
    user_backup_dir = os.path.join(backup_dir, username)
    os.makedirs(user_backup_dir, exist_ok=True)
    os.chmod(user_backup_dir, 0o775)

    # Use absolute path to /data/{username} where PVC is mounted
    # This ensures the backup lands on persistent storage
    remote_dir = f"/data/{username}"

    logger.info(f"Triggering SCP backup for {username} to {sftp_host}:{sftp_port}{remote_dir}")

    # Trigger cPanel to push backup to our SFTP
    client.trigger_backup_to_sftp(
        cpanel_user=username,
        sftp_host=sftp_host,
        sftp_port=sftp_port,
        sftp_user=sftp_user,
        sftp_password=sftp_password,
        remote_dir=remote_dir,
    )

    # Poll for backup file arrival
    # SSH container mounts PVC at /data, worker at /backups
    # cPanel SCP uploads to /data/{username}/, so files appear at /backups/{username}/
    poll_dir = os.path.join('/backups', username)
    os.makedirs(poll_dir, exist_ok=True)

    backup_file = wait_for_backup_file(poll_dir, timeout=3600)

    if not backup_file:
        raise Exception(f"Backup file for {username} did not arrive within timeout")

    return backup_file


def wait_for_domain_ready(domain_obj: Domain, timeout: int = 960):
    """
    Wait for a domain to be ready in Kubernetes.

    Polls the Domain CR status until it's 'Ready' or timeout.
    """
    from dashboard.k8s import get_domain, K8sNotFoundError
    import time

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            domain_cr = get_domain(domain_obj.domain_name)

            if domain_cr.status.is_ready:
                logger.info(f"Domain {domain_obj.domain_name} is Ready")
                return True
            elif domain_cr.status.is_failed:
                raise Exception(f"Domain provisioning failed: {domain_cr.status.message}")

            logger.debug(f"Domain {domain_obj.domain_name} phase: {domain_cr.status.phase}")
            time.sleep(5)
        except K8sNotFoundError:
            time.sleep(5)

    raise TimeoutError(f"Domain {domain_obj.domain_name} did not become ready within {timeout}s")


def upload_and_restore(domain_obj: Domain, archive_path: str):
    """
    Upload archive to domain's backup PVC and create Restore CR.

    This reuses the existing restore mechanism from admin.py.
    """
    from dashboard.views.admin import _upload_archive_to_pvc
    from dashboard.k8s import create_restore

    namespace = domain_obj.namespace
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    restore_name = f"cpanel-migration-{timestamp}"
    dest_path = f"/backup/uploaded/{restore_name}/archive.tar.gz"

    # Upload archive to backup PVC
    _upload_archive_to_pvc(namespace, archive_path, dest_path)

    # Create Restore CR
    create_restore(
        namespace=namespace,
        domain_name=domain_obj.domain_name,
        backup_name=restore_name,
        volume_snapshot_name=None,
        database_backup_path=dest_path,
        restore_type="uploaded",
        uploaded_archive_path=dest_path,
    )


def import_mailbox(domain_obj: Domain, local_part: str, maildir_tar_path: str, password: str = None):
    """
    Import a mailbox for a domain.

    Creates the MailUser record and imports the maildir archive.
    """
    from passlib.hash import sha512_crypt
    import subprocess
    import secrets

    # Generate a random password if not provided
    if not password:
        password = secrets.token_urlsafe(16)

    # Create MailUser
    mail_user, created = MailUser.objects.get_or_create(
        domain=domain_obj,
        local_part=local_part,
        defaults={
            'password': sha512_crypt.using(rounds=5000).hash(password),
        }
    )

    if not created:
        logger.info(f"Mail user {local_part}@{domain_obj.domain_name} already exists, skipping")
        return mail_user

    # Import mailbox archive
    # This mimics the logic from mail.py import_mailbox()
    namespace = 'kubepanel'
    smtp_pod = None

    # Find SMTP pod
    from kubernetes import client, config
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(
        namespace=namespace,
        label_selector='app=smtp'
    )
    if pods.items:
        smtp_pod = pods.items[0].metadata.name
    else:
        raise Exception("SMTP pod not found")

    # Copy archive to SMTP pod
    dest_dir = f"/var/mail/vmail/{domain_obj.domain_name}/{local_part}"
    subprocess.run([
        'kubectl', 'exec', '-n', namespace, smtp_pod, '--',
        'mkdir', '-p', dest_dir
    ], check=True, capture_output=True)

    subprocess.run([
        'kubectl', 'cp', maildir_tar_path,
        f'{namespace}/{smtp_pod}:/tmp/mailbox.tar.gz'
    ], check=True, capture_output=True)

    subprocess.run([
        'kubectl', 'exec', '-n', namespace, smtp_pod, '--',
        'tar', '-xzf', '/tmp/mailbox.tar.gz', '-C', dest_dir, '--strip-components=1'
    ], check=True, capture_output=True)

    subprocess.run([
        'kubectl', 'exec', '-n', namespace, smtp_pod, '--',
        'chown', '-R', 'vmail:vmail', dest_dir
    ], check=True, capture_output=True)

    subprocess.run([
        'kubectl', 'exec', '-n', namespace, smtp_pod, '--',
        'rm', '/tmp/mailbox.tar.gz'
    ], check=True, capture_output=True)

    logger.info(f"Imported mailbox {local_part}@{domain_obj.domain_name}")
    return mail_user


def process_domain(client: CPanelClient, md: MigrationDomain, work_dir: str, backup_path: str = None):
    """
    Process a single domain migration.

    Steps:
    1. Download backup from cPanel (or use provided backup)
    2. Parse backup
    3. Create domain in KubePanel
    4. Wait for domain to be ready
    5. Import files and database
    6. Import mailboxes

    Args:
        client: CPanelClient instance
        md: MigrationDomain to process
        work_dir: Working directory for temp files
        backup_path: Optional pre-downloaded backup path (shared across domains)
    """
    domain_name = md.source_domain

    try:
        # Step 1: Get backup (via SFTP push if not provided)
        if backup_path is None:
            update_domain_status(md, 'fetching', 5, 'Triggering cPanel backup to SFTP')

            try:
                backup_path = trigger_sftp_backup(client, md.source_username, '/backups')
                update_domain_status(md, 'fetching', 25, f'Backup received: {os.path.basename(backup_path)}')
            except Exception as e:
                raise Exception(f"Backup failed: {e}")

            logger.info(f"Received backup via SFTP: {backup_path}")
        else:
            update_domain_status(md, 'fetching', 20, 'Using shared backup file')

        # Step 2: Parse backup
        update_domain_status(md, 'fetching', 25, 'Parsing backup contents')

        with CPanelBackupParser(backup_path) as parser:
            metadata = parser.get_metadata()
            logger.info(f"Backup metadata: {metadata}")

            # Step 3: Create domain in KubePanel (or resume existing)
            update_domain_status(md, 'creating', 30, 'Creating domain in KubePanel')

            # Get default workload version
            from dashboard.models import WorkloadVersion
            default_workload = WorkloadVersion.objects.filter(
                workload_type__slug='php',
                is_default=True,
                is_active=True
            ).first()

            if not default_workload:
                default_workload = WorkloadVersion.objects.filter(
                    is_active=True
                ).first()

            # Determine resource limits from package
            if md.target_package:
                storage_size = md.target_package.max_storage_size
                cpu_limit = md.target_package.max_cpu
                mem_limit = md.target_package.max_memory
            else:
                storage_size = 10
                cpu_limit = 500
                mem_limit = 512

            # Check if domain already exists (from a previous failed migration)
            from dashboard.k8s import create_domain, get_domain, DomainSpec, K8sNotFoundError

            domain_obj = Domain.objects.filter(domain_name=domain_name).first()
            domain_cr_exists = False

            if domain_obj:
                logger.info(f"Domain {domain_name} already exists in Django DB, resuming migration")
                # Check if CR also exists
                try:
                    get_domain(domain_name)
                    domain_cr_exists = True
                    logger.info(f"Domain CR for {domain_name} also exists")
                except K8sNotFoundError:
                    logger.info(f"Domain CR for {domain_name} does not exist, will create")
            else:
                # Create new domain
                domain_obj = Domain.objects.create(
                    domain_name=domain_name,
                    title=f"Migrated from cPanel",
                    owner=md.target_owner,
                    storage_size=storage_size,
                    cpu_limit=cpu_limit,
                    mem_limit=mem_limit,
                    workload_version=default_workload,
                )
                logger.info(f"Created domain {domain_name} in Django DB")

            md.target_domain = domain_obj
            md.save()

            # Create Domain CR if it doesn't exist
            if not domain_cr_exists:
                spec = DomainSpec(
                    domain_name=domain_name,
                    storage=f"{storage_size}Gi",
                    cpu_limit=f"{cpu_limit}m",
                    memory_limit=f"{mem_limit}Mi",
                    workload_type=default_workload.workload_type.slug,
                    workload_version=default_workload.version,
                    workload_image=default_workload.image_url,
                    workload_port=default_workload.workload_type.app_port,
                    proxy_mode=default_workload.workload_type.proxy_mode,
                )
                spec.email_enabled = bool(md.selected_mailboxes)
                create_domain(spec, owner=md.target_owner.username)
                logger.info(f"Created Domain CR for {domain_name}")

            # Step 4: Wait for domain to be ready
            update_domain_status(md, 'creating', 40, 'Waiting for domain to be provisioned')
            wait_for_domain_ready(domain_obj)

            # Step 5: Import files and database
            update_domain_status(md, 'importing_files', 50, 'Extracting website files')

            transformer = CPanelTransformer()
            domain_work_dir = os.path.join(work_dir, domain_name.replace('.', '-'))
            os.makedirs(domain_work_dir, exist_ok=True)

            # Extract public_html
            public_html_path = os.path.join(domain_work_dir, 'html')
            try:
                parser.extract_public_html(public_html_path)
            except CPanelParserError as e:
                logger.warning(f"Could not extract public_html: {e}")
                public_html_path = None

            # Extract database if selected
            database_sql_path = None
            if md.selected_database:
                update_domain_status(md, 'importing_db', 60, f'Extracting database {md.selected_database}')
                database_sql_path = os.path.join(domain_work_dir, 'database.sql')
                try:
                    parser.extract_database(md.selected_database, database_sql_path)
                except CPanelParserError as e:
                    logger.warning(f"Could not extract database: {e}")
                    database_sql_path = None

            # Create restore archive
            if public_html_path or database_sql_path:
                update_domain_status(md, 'importing_files', 70, 'Creating restore archive')
                archive_path = os.path.join(domain_work_dir, 'restore.tar.gz')
                transformer.create_restore_archive(
                    public_html_path=public_html_path,
                    database_sql_path=database_sql_path,
                    output_path=archive_path,
                    metadata={'source': 'cpanel', 'original_domain': domain_name}
                )

                # Upload and trigger restore
                upload_and_restore(domain_obj, archive_path)

            # Step 6: Import mailboxes
            if md.selected_mailboxes and md.email_method == 'backup':
                update_domain_status(md, 'importing_mail', 80, f'Importing {len(md.selected_mailboxes)} mailbox(es)')

                for i, email in enumerate(md.selected_mailboxes):
                    local_part = email.split('@')[0]
                    progress = 80 + int((i / len(md.selected_mailboxes)) * 15)
                    update_domain_status(md, 'importing_mail', progress, f'Importing mailbox {email}')

                    maildir_path = os.path.join(domain_work_dir, f'mail_{local_part}')
                    try:
                        parser.extract_mailbox(domain_name, local_part, maildir_path)

                        # Create mailbox archive
                        mailbox_tar = os.path.join(domain_work_dir, f'{local_part}-mailbox.tar.gz')
                        transformer.create_mailbox_archive(maildir_path, local_part, mailbox_tar)

                        # Import mailbox
                        import_mailbox(domain_obj, local_part, mailbox_tar)

                    except CPanelParserError as e:
                        logger.warning(f"Could not import mailbox {email}: {e}")

            # Step 7: IMAP sync (if selected)
            elif md.selected_mailboxes and md.email_method == 'imap':
                update_domain_status(md, 'syncing_mail', 85, 'IMAP sync not yet implemented')
                # TODO: Implement IMAP sync using imapsync

            # Done!
            update_domain_status(md, 'completed', 100, 'Migration completed successfully')
            md.completed_at = timezone.now()
            md.save()

    except Exception as e:
        logger.exception(f"Failed to migrate {domain_name}")
        update_domain_status(md, 'failed', error=str(e))
        raise


def main():
    batch_id = os.environ.get('BATCH_ID')
    if not batch_id:
        logger.error("BATCH_ID environment variable not set")
        sys.exit(1)

    try:
        batch_id = int(batch_id)
        batch = MigrationBatch.objects.get(id=batch_id)
    except (ValueError, MigrationBatch.DoesNotExist) as e:
        logger.error(f"Invalid batch ID or batch not found: {batch_id}")
        sys.exit(1)

    logger.info(f"Starting migration batch {batch_id}")
    logger.info(f"Source: {batch.cpanel_hostname}:{batch.cpanel_port}")
    logger.info(f"Domains to migrate: {batch.domains.filter(status='queued').count()}")

    # Create work directory
    work_dir = tempfile.mkdtemp(prefix='migration_')
    logger.info(f"Work directory: {work_dir}")

    try:
        # Connect to cPanel
        client = CPanelClient(
            hostname=batch.cpanel_hostname,
            port=batch.cpanel_port,
            username=batch.cpanel_username,
            api_token=decrypt_token(batch),
            verify_ssl=batch.cpanel_verify_ssl,
        )

        # Test connection
        try:
            client.test_connection()
            logger.info("Successfully connected to cPanel")
        except (CPanelConnectionError, CPanelAPIError) as e:
            logger.error(f"Failed to connect to cPanel: {e}")
            batch.status = 'failed'
            batch.save()
            sys.exit(1)

        # Group domains by cPanel username (for backup sharing)
        # All domains from the same cPanel user share a single backup
        domains_by_user = {}
        for md in batch.domains.filter(status='queued').order_by('id'):
            username = md.source_username or batch.cpanel_username
            if username not in domains_by_user:
                domains_by_user[username] = []
            domains_by_user[username].append(md)

        # Process domains, triggering backup once per cPanel user via SFTP
        for username, domain_list in domains_by_user.items():
            backup_path = None

            # Trigger backup for this user via SFTP (first domain triggers download)
            if len(domain_list) > 1:
                logger.info(f"Triggering shared SFTP backup for user {username} ({len(domain_list)} domains)")

                # Use first domain to show progress
                first_domain = domain_list[0]
                first_domain.started_at = timezone.now()
                first_domain.save()

                try:
                    update_batch_backup_status(batch, 'Triggering backup to SFTP...', 10)
                    update_domain_status(first_domain, 'fetching', 5, '[Shared] Triggering SFTP backup')

                    backup_path = trigger_sftp_backup(client, username, '/backups')

                    update_batch_backup_status(batch, 'Backup received via SFTP', 100)
                    update_domain_status(first_domain, 'fetching', 25, f'[Shared] Backup received')
                    logger.info(f"Received shared backup via SFTP: {backup_path}")
                except Exception as e:
                    logger.error(f"Failed to get SFTP backup for {username}: {e}")
                    update_batch_backup_status(batch, f'Backup failed: {e}', 0)
                    # Mark all domains for this user as failed
                    for md in domain_list:
                        update_domain_status(md, 'failed', error=f"SFTP backup failed: {e}")
                    continue

            # Process each domain
            for md in domain_list:
                if md.status == 'failed':  # Skip if already failed from backup
                    continue

                md.started_at = md.started_at or timezone.now()
                md.save()

                try:
                    process_domain(client, md, work_dir, backup_path)
                except Exception as e:
                    logger.error(f"Domain {md.source_domain} failed: {e}")
                    # Continue with other domains

        # Update batch status
        total = batch.domains.count()
        completed = batch.domains.filter(status='completed').count()
        failed = batch.domains.filter(status='failed').count()

        if failed == total:
            batch.status = 'failed'
        elif completed + failed == total:
            batch.status = 'completed'
        else:
            batch.status = 'completed'  # Partial success

        batch.completed_at = timezone.now()
        batch.save()

        logger.info(f"Migration batch {batch_id} completed: {completed}/{total} successful, {failed} failed")

    finally:
        # Cleanup work directory
        shutil.rmtree(work_dir, ignore_errors=True)

        # Clear API token for security
        batch.clear_api_token()

        # Cleanup SFTP infrastructure
        try:
            from dashboard.services.migration_sftp import delete_migration_sftp
            delete_migration_sftp(batch_id)
            logger.info(f"Cleaned up SFTP resources for batch {batch_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup SFTP resources: {e}")


if __name__ == '__main__':
    main()
