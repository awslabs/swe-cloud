#!/usr/bin/env bash
# Runtime dependencies for the pgcopydb reference (python:3.12-bookworm).
#
# * libgc1 + libpq5: shared libraries the reference ELF links against.
# * postgresql-client-16: pgcopydb resolves `psql` on PATH and runs pg_dump,
#   pg_restore and vacuumdb from the same directory (/usr/bin, the Debian
#   pg_wrapper) for schema dump/restore. These are standard PostgreSQL client
#   tools, matching the Floci RDS default engine (postgres:16).
# * psycopg (binary wheel): general-purpose PostgreSQL driver for Python used
#   to seed and inspect databases (fixtures, verification, solver tooling).
# Nothing from the pgcopydb source tree is installed here.
set -euo pipefail

PGDG_KEY_URL="https://www.postgresql.org/media/keys/ACCC4CF8.asc"
PGDG_KEY_FPR="B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"
PG16_VERSION="16.15-1.pgdg12+2"
LIBPQ_VERSION="18.6-1.pgdg12+2"
PG_COMMON_VERSION="293.pgdg12+1"
LIBGC_VERSION="1:8.2.2-3"
PSYCOPG_VERSION="3.2.13"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends ca-certificates curl gnupg >/dev/null
install -d /usr/share/keyrings
export GNUPGHOME=$(mktemp -d)
curl -fsSL "$PGDG_KEY_URL" -o /tmp/pgdg.asc
fpr=$(gpg --batch --with-colons --show-keys /tmp/pgdg.asc | awk -F: '/^fpr/ {print $10; exit}')
[ "$fpr" = "$PGDG_KEY_FPR" ] || { echo "unexpected PGDG key fingerprint: $fpr" >&2; exit 1; }
gpg --batch --yes --dearmor -o /usr/share/keyrings/pgdg.gpg /tmp/pgdg.asc
rm -rf /tmp/pgdg.asc "$GNUPGHOME"
unset GNUPGHOME
echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
    >/etc/apt/sources.list.d/pgdg.list
echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt-archive.postgresql.org/pub/repos/apt bookworm-pgdg-archive main" \
    >/etc/apt/sources.list.d/pgdg-archive.list
apt-get update -qq

pkgs=(
    "libgc1=${LIBGC_VERSION}"
    "libpq5=${LIBPQ_VERSION}"
    "postgresql-client-common=${PG_COMMON_VERSION}"
    "postgresql-client-16=${PG16_VERSION}"
)
# python:3.12-bookworm ships Debian's libpq-dev 15, which pins libpq5 15;
# move it to the same libpq version so the upgrade is consistent.
if dpkg-query -W -f='${Status}' libpq-dev 2>/dev/null | grep -q "install ok installed"; then
    pkgs+=("libpq-dev=${LIBPQ_VERSION}")
fi
apt-get install -y -qq --no-install-recommends "${pkgs[@]}"
apt-mark hold libpq5 postgresql-client-16 >/dev/null
apt-get clean
rm -rf /var/lib/apt/lists/*

python3 -m pip install --no-cache-dir "psycopg[binary]==${PSYCOPG_VERSION}" "typing-extensions==4.16.0"

psql --version
pg_dump --version
pg_restore --version
python3 -c 'import psycopg; print("psycopg", psycopg.__version__)'
