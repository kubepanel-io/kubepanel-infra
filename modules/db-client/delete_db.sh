#!/bin/bash
mysql -h mariadb.kubepanel.svc.cluster.local -uroot -p$MARIADB_ROOT_PASSWORD -e "DROP DATABASE $DBNAME; REVOKE ALL PRIVILEGES ON $DBNAME.* FROM $DBUSER@'%'"
