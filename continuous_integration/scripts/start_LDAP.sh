#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${LDAP_COMPOSE_FILE:-$ROOT_DIR/continuous_integration/docker-configs/ldap-docker-compose.yml}"
COMPOSE_PROJECT="${LDAP_COMPOSE_PROJECT:-}"
LDAP_HOST="${LDAP_HOST:-127.0.0.1}"
LDAP_PORT="${LDAP_PORT:-1389}"
LDAP_ADMIN_DN="cn=admin,dc=example,dc=org"
LDAP_ADMIN_PASSWORD="adminpassword"
LDAP_BASE_DN="dc=example,dc=org"

compose_cmd() {
    if [[ -n "$COMPOSE_PROJECT" ]]; then
        docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" "$@"
    else
        docker compose -f "$COMPOSE_FILE" "$@"
    fi
}

get_openldap_container_id() {
    compose_cmd ps -q openldap | tr -d '[:space:]'
}

wait_for_ldap() {
    local timeout_seconds="${1:-60}"
    local deadline=$((SECONDS + timeout_seconds))

    while (( SECONDS < deadline )); do
        if python - <<PY >/dev/null 2>&1
import socket

with socket.create_connection(("${LDAP_HOST}", ${LDAP_PORT}), timeout=1):
    pass
PY
        then
            return 0
        fi
        sleep 1
    done

    return 1
}

wait_for_ldap_bind() {
    local container_id="$1"
    local timeout_seconds="${2:-60}"
    local deadline=$((SECONDS + timeout_seconds))
    local rc=0

    while (( SECONDS < deadline )); do
        rc=0
        docker exec "$container_id" ldapsearch \
            -x \
            -H "ldap://127.0.0.1:389" \
            -D "$LDAP_ADMIN_DN" \
            -w "$LDAP_ADMIN_PASSWORD" \
            -b "$LDAP_BASE_DN" \
            -s base \
            "(objectclass=*)" dn >/dev/null 2>&1 || rc=$?
        if [[ "$rc" -eq 0 ]]; then
            return 0
        fi
        sleep 1
    done

    return 1
}

wait_for_ldap_test_user_bind() {
    local container_id="$1"
    local timeout_seconds="${2:-60}"
    local deadline=$((SECONDS + timeout_seconds))
    local rc=0

    while (( SECONDS < deadline )); do
        rc=0
        docker exec "$container_id" ldapwhoami \
            -x \
            -H "ldap://127.0.0.1:389" \
            -D "cn=user01,ou=users,$LDAP_BASE_DN" \
            -w "password1" >/dev/null 2>&1 || rc=$?
        if [[ "$rc" -eq 0 ]]; then
            return 0
        fi
        sleep 1
    done

    return 1
}

print_ldap_diagnostics() {
    local container_id="${1:-}"

    echo "LDAP startup diagnostics:" >&2
    compose_cmd ps >&2 || true

    if [[ -z "$container_id" ]]; then
        container_id="$(get_openldap_container_id)"
    fi

    if [[ -n "$container_id" ]]; then
        docker logs --tail 200 "$container_id" >&2 || true
    else
        compose_cmd logs --tail 200 openldap >&2 || true
    fi
}

ldap_entry_exists() {
    local container_id="$1"
    local dn="$2"

    docker exec "$container_id" ldapsearch \
        -x \
        -H "ldap://127.0.0.1:389" \
        -D "$LDAP_ADMIN_DN" \
        -w "$LDAP_ADMIN_PASSWORD" \
        -b "$dn" \
        -s base \
        "(objectclass=*)" dn >/dev/null 2>&1
}

ldap_add_if_missing() {
    local container_id="$1"
    local dn="$2"
    local ldif="$3"

    if ldap_entry_exists "$container_id" "$dn"; then
        return 0
    fi

    docker exec -i "$container_id" ldapadd \
        -x \
        -H "ldap://127.0.0.1:389" \
        -D "$LDAP_ADMIN_DN" \
        -w "$LDAP_ADMIN_PASSWORD" >/dev/null <<EOF
${ldif}
EOF
}

seed_ldap_test_users() {
    local container_id="$1"

    ldap_add_if_missing "$container_id" "ou=users,$LDAP_BASE_DN" "dn: ou=users,$LDAP_BASE_DN
objectClass: organizationalUnit
ou: users"

    ldap_add_if_missing "$container_id" "cn=user01,ou=users,$LDAP_BASE_DN" "dn: cn=user01,ou=users,$LDAP_BASE_DN
objectClass: inetOrgPerson
cn: user01
sn: user01
uid: user01
userPassword: password1"

    ldap_add_if_missing "$container_id" "cn=user02,ou=users,$LDAP_BASE_DN" "dn: cn=user02,ou=users,$LDAP_BASE_DN
objectClass: inetOrgPerson
cn: user02
sn: user02
uid: user02
userPassword: password2"
}

# Start LDAP server in docker container
compose_cmd up -d
CONTAINER_ID="$(get_openldap_container_id)"
if [[ -z "$CONTAINER_ID" ]]; then
    echo "Unable to determine LDAP container id from compose project." >&2
    print_ldap_diagnostics
    exit 1
fi

if ! wait_for_ldap 120; then
    echo "LDAP port ${LDAP_HOST}:${LDAP_PORT} did not become reachable in time." >&2
    print_ldap_diagnostics "$CONTAINER_ID"
    exit 1
fi

echo "LDAP port ${LDAP_HOST}:${LDAP_PORT} is reachable. Waiting for slapd initialization..."
sleep 3

if ! wait_for_ldap_bind "$CONTAINER_ID" 120; then
    echo "LDAP admin bind did not become ready in time." >&2
    print_ldap_diagnostics "$CONTAINER_ID"
    exit 1
fi

seed_ldap_test_users "$CONTAINER_ID"

if ! wait_for_ldap_test_user_bind "$CONTAINER_ID" 60; then
    echo "LDAP test-user bind did not become ready in time." >&2
    print_ldap_diagnostics "$CONTAINER_ID"
    exit 1
fi

docker ps
