-- The roles and schema this service expects, on a database it shares.
--
-- Run once per database, by someone who owns it — not by the service, and not
-- from a migration. `docs/shared-postgres.md` is why each line is here; the
-- short version is that a logical boundary is only a boundary when the
-- database enforces it, and every rule below is one another service can be
-- damaged by if this one skips it.
--
-- Usage:
--   psql -v password="'…'" -v runtime_password="'…'" -v database=<name> \
--        -f deploy/postgres-bootstrap.sql
--
-- `tests/test_postgres_migrations.py` and `tests/test_postgres_privileges.py`
-- run against exactly this setup, so what CI checks is the shape production
-- has.

-- Rule 1: three roles, not one. A runtime credential that owns its own
-- schema turns a bad DELETE into a possible DROP — the same session that runs
-- the application's DML could also alter or drop the tables it should be
-- confined to. Splitting into owner (schema owner, never logs in), migrator
-- (deployment only) and runtime (what the application logs in as) makes the
-- database itself enforce that boundary, not just convention.
CREATE ROLE tubedepth_owner NOLOGIN;
CREATE ROLE tubedepth_migrator LOGIN NOINHERIT PASSWORD :password;
CREATE ROLE tubedepth_runtime  LOGIN NOINHERIT PASSWORD :runtime_password;

-- The migrator acts as the owner via SET ROLE (migrations/env.py does this),
-- so objects it creates are owned by tubedepth_owner uniformly rather than by
-- whichever migrator happened to run — without this the ownership audit in
-- rule 1 finds rows, and the next migrator cannot ALTER what the last one
-- created.
GRANT tubedepth_owner TO tubedepth_migrator;

-- Rule 0: one schema, one owner. Unqualified names resolve here via
-- search_path, which is what puts this service's alembic_version in its own
-- schema instead of the one row every service on this database would
-- otherwise overwrite in turn (rule 3).
CREATE SCHEMA tubedepth AUTHORIZATION tubedepth_owner;
REVOKE ALL ON SCHEMA tubedepth FROM PUBLIC;

-- The default `public` schema is writable by everyone. Leaving it that way
-- makes the schema separation above a naming convention rather than a
-- boundary.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Rule 1: the runtime role gets DML only — no CREATE, ALTER, DROP, TRUNCATE,
-- REFERENCES or TRIGGER. It can use the schema and touch the rows in it, and
-- nothing else.
--
-- SCHEMA-SCOPED-GRANTS-BEGIN
-- These statements are ACL entries tied to the schema's own OID (the GRANTs
-- directly, and the ALTER DEFAULT PRIVILEGES entries, which are keyed by
-- (role, namespace)). `tests/test_postgres_privileges.py` drops and recreates
-- `tubedepth` between test runs for isolation, which takes a schema's ACL
-- entries with it, so that file re-applies exactly this block — parsed out by
-- these markers, not retyped — after every reset. Keep the block
-- self-contained (no `:database`/`:password` psql variables) so it can be
-- extracted and replayed verbatim.
GRANT USAGE ON SCHEMA tubedepth TO tubedepth_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA tubedepth TO tubedepth_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA tubedepth TO tubedepth_runtime;

-- Not optional: without these, a table a *future* migration creates is
-- unreachable by runtime, and the failure appears at the first request after
-- a deploy rather than during it.
ALTER DEFAULT PRIVILEGES FOR ROLE tubedepth_owner IN SCHEMA tubedepth
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tubedepth_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE tubedepth_owner IN SCHEMA tubedepth
  GRANT USAGE, SELECT ON SEQUENCES TO tubedepth_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE tubedepth_owner IN SCHEMA tubedepth
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
-- SCHEMA-SCOPED-GRANTS-END

-- Not cosmetic. Unqualified names resolve here, which is what puts this
-- service's alembic_version in its own schema instead of the one row every
-- service on this database would otherwise overwrite in turn.
--
-- Divergence from the regulation's example, recorded in docs/status.md
-- ("규정 적용"): the regulation sets the migrator's search_path to
-- pg_catalog alone (fail-closed), which is correct under its default
-- strategy where migrations name their schema explicitly. This repository
-- uses the search_path strategy under rule 2's exception clause — its five
-- existing revisions are schema-unqualified — so a pg_catalog-only migrator
-- would write every table into public. The migrator's search_path here
-- therefore includes tubedepth, same as the runtime's.
ALTER ROLE tubedepth_runtime  IN DATABASE :database SET search_path = tubedepth, pg_catalog;
ALTER ROLE tubedepth_migrator IN DATABASE :database SET search_path = tubedepth, pg_catalog;

-- Rule 5: role-scoped timeouts, sized against statements rather than jobs —
-- a transcript or comment harvest runs for minutes while each individual
-- statement stays short. lock_timeout is kept shorter than statement_timeout.
--
-- autovacuum cannot clean up dead tuples newer than the oldest open
-- transaction, database-wide. A scraper holding one open across a network
-- call makes another service's tables bloat, with nothing pointing back here.
ALTER ROLE tubedepth_runtime IN DATABASE :database SET statement_timeout = '15s';
ALTER ROLE tubedepth_runtime IN DATABASE :database SET lock_timeout = '3s';
ALTER ROLE tubedepth_runtime IN DATABASE :database SET idle_in_transaction_session_timeout = '30s';
-- transaction_timeout is PostgreSQL 17+. The deployment target is 18, so this
-- is set unconditionally: a bootstrap against an older server should fail
-- loudly rather than silently skip a required setting.
ALTER ROLE tubedepth_runtime IN DATABASE :database SET transaction_timeout = '60s';

-- Rule 9: pin the session TimeZone to UTC on both roles that connect. The
-- regulation asks for this as a predictability property in its own right —
-- implicit conversions (a bare `timestamp` cast, `now()`, display) all read
-- through the session zone — and Task 4 measured what happens without it: a
-- `timestamptz -> timestamp` downgrade with no explicit `AT TIME ZONE` ran
-- under a non-UTC session zone and shifted every stored instant by the
-- session's offset. Migrator gets it too — it is the role that runs
-- migrations, including ones with exactly that kind of implicit cast.
ALTER ROLE tubedepth_runtime  IN DATABASE :database SET TimeZone = 'UTC';
ALTER ROLE tubedepth_migrator IN DATABASE :database SET TimeZone = 'UTC';

-- Rule 4: connection budget. deploy/service-manifest.yaml declares 20 for
-- this service — that ceiling is what the fleet was asked for and granted;
-- raising it is a fleet-level decision, not something this service's own
-- pool arithmetic gets to change unilaterally (docs/status.md's "규정 적용"
-- table, rule 4, explains why TUBEDEPTH_CONCURRENCY is capped at 2 rather
-- than the connection budget being raised to fit a bigger concurrency).
-- CONNECTION LIMIT makes the database enforce the declared number rather
-- than trust every pool configuration to add up correctly.
ALTER ROLE tubedepth_runtime CONNECTION LIMIT 20;
