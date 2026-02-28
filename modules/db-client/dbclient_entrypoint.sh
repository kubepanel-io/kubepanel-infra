#!/bin/bash
mysql -h mariadb.kubepanel.svc.cluster.local -uroot -p$MARIADB_ROOT_PASSWORD -e "CREATE DATABASE $DBNAME; GRANT ALL PRIVILEGES ON $DBNAME.* TO $DBUSER@'%' IDENTIFIED BY '$DBPASS'"
