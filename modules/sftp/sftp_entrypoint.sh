#!/bin/bash
set -e

# Generate SSH host keys if missing
ssh-keygen -A

# Create webuser if not exists (should exist from Dockerfile)
id webuser &>/dev/null || useradd -u 7777 -g webgroup -m webuser

# Set up password from mounted secret (if password file exists)
if [ -f /etc/sftp-secrets/password ]; then
    PASSWORD=$(cat /etc/sftp-secrets/password)
    echo "webuser:${PASSWORD}" | chpasswd
    echo "Password authentication configured for webuser"
fi

# Set up authorized_keys from mounted secret (if exists)
if [ -f /etc/sftp-secrets/ssh-publickey ]; then
    mkdir -p /home/webuser/.ssh
    cp /etc/sftp-secrets/ssh-publickey /home/webuser/.ssh/authorized_keys
    chmod 700 /home/webuser/.ssh
    chmod 600 /home/webuser/.ssh/authorized_keys
    chown -R webuser:webgroup /home/webuser/.ssh
    echo "SSH key authentication configured for webuser"
fi

# Ensure correct chroot permissions (must be root:root 755)
chown root:root /home/webuser
chmod 755 /home/webuser

# Ensure html directory is writable by webuser
chown webuser:webgroup /home/webuser/html
chmod 755 /home/webuser/html

echo "Starting SFTP server..."

# Start SSH daemon in foreground
exec /usr/sbin/sshd -D -e
