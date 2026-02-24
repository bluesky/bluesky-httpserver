#!/bin/bash
set -e

# Start LDAP server in docker container
docker pull osixia/openldap:latest
docker compose -f continuous_integration/docker-configs/ldap-docker-compose.yml up -d
docker ps
