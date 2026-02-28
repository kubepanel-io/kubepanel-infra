#!/bin/bash
#v1.0
# DJANGO_SECRET_KEY is set via environment variable from Kubernetes deployment
/usr/local/bin/python /kubepanel/manage.py runserver 0.0.0.0:8000 --insecure
