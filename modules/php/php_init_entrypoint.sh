#!/bin/bash
set -euo pipefail
SMTP=$(dig +short smtp.kubepanel.svc.cluster.local)
MARIADB=$(dig +short mariadb.kubepanel.svc.cluster.local)
REDIS=$(dig +short redis.kubepanel.svc.cluster.local)
iptables -t nat -I OUTPUT -m tcp -p tcp --dport 25 -j DNAT --to-destination $SMTP:25
iptables -t nat -I OUTPUT -m tcp -p tcp --dport 3306 -j DNAT --to-destination $MARIADB:3306
iptables -t nat -A POSTROUTING -j MASQUERADE

# Check if WP_PREINSTALL is set to "True"
if [ "${WP_PREINSTALL:-}" = "True" ]; then
    echo "WP_PREINSTALL is True. Checking if /usr/share/nginx/html is empty..."
    CONTENTS=$(ls -A /usr/share/nginx/html | grep -v "lost+found" || true)
    if [ -z "$CONTENTS" ]; then
        echo "/usr/share/nginx/html is empty (except possibly lost+found). Proceeding with WordPress download..."
        curl -sSLo /tmp/wordpress.zip https://wordpress.org/latest.zip
        unzip -qo /tmp/wordpress.zip -d /tmp
        mv /tmp/wordpress/* /usr/share/nginx/html/
        rm -rf /tmp/wordpress /tmp/wordpress.zip
        chown -R 7777:7777 /usr/share/nginx/html
        sed -i 's/localhost/127.0.0.1/g' /usr/share/nginx/html/wp-admin/setup-config.php
        echo "WordPress installation completed successfully."
    else
        echo "/usr/share/nginx/html is not empty. Skipping WordPress installation."
    fi
else
    echo "WP_PREINSTALL is not True. Skipping pre-installation."
    chown -R 7777:7777 /usr/share/nginx/html
fi
