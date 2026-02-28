#!/bin/bash
chown vmail:vmail /var/mail/vmail
rm -rf /var/spool/postfix/pid/*
rm -f /var/spool/postfix/private/dovecot-lmtp
rm -f /var/spool/postfix/private/auth
/etc/init.d/postfix start
/etc/init.d/dovecot start
while true; do sleep 36000; done;
