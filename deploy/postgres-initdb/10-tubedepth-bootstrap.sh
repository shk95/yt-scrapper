#!/bin/sh
# Run deploy/postgres-bootstrap.sql inside the initdb phase, with the psql
# variables it requires.
#
# This wrapper exists because the bootstrap file is *not* directly mountable
# into /docker-entrypoint-initdb.d. The entrypoint runs `.sql` files there with
# a plain `psql -f` and no `-v` assignments, and that file needs three:
#
#   :password           the tubedepth_migrator password
#   :runtime_password   the tubedepth_runtime password
#   :database           the database the ALTER ROLE ... IN DATABASE lines target
#
# `:password` and `:runtime_password` are interpolated bare into
# `CREATE ROLE ... PASSWORD :password`, so they have to arrive already wrapped
# in single quotes — the same way tool/checks/test and .github/workflows/ci.yml
# pass them. `:database` is used as an identifier and is passed unquoted.
#
# The point of the wrapper is that the SQL stays the single copy: a
# containerised variant of it would be a second file to keep in step with the
# one production runs, and the whole reason for mounting it is that they are
# the same file.
#
# Deliberately NOT here: the `GRANT CREATE ON DATABASE ... TO
# tubedepth_migrator` that tool/checks/test and CI add after this. That grant
# is a test affordance — the suite drops and recreates a schema per test for
# isolation — and in a deployment the migrator has no business creating
# schemas.

set -eu

: "${TUBEDEPTH_MIGRATOR_PASSWORD:?the postgres service must pass the tubedepth_migrator password}"
: "${TUBEDEPTH_RUNTIME_PASSWORD:?the postgres service must pass the tubedepth_runtime password}"

# `-v database="$POSTGRES_DB"` rather than a second variable naming the same
# thing: the ALTER ROLE ... IN DATABASE statements have to target the database
# initdb actually created, and reading POSTGRES_DB here makes that true by
# construction instead of by two values agreeing.
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v password="'$TUBEDEPTH_MIGRATOR_PASSWORD'" \
     -v runtime_password="'$TUBEDEPTH_RUNTIME_PASSWORD'" \
     -v database="$POSTGRES_DB" \
     -f /opt/tubedepth/postgres-bootstrap.sql
