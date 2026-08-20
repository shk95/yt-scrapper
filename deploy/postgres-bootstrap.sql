-- The role and schema this service expects, on a database it shares.
--
-- Run once per database, by someone who owns it — not by the service, and not
-- from a migration. `docs/shared-postgres.md` is why each line is here; the
-- short version is that a logical boundary is only a boundary when the
-- database enforces it, and every rule below is one another service can be
-- damaged by if this one skips it.
--
-- Usage:
--   psql -v password="'…'" -v database=<name> -f deploy/postgres-bootstrap.sql
--
-- `tests/test_postgres_migrations.py` runs against exactly this setup, so what
-- CI checks is the shape production has.

CREATE ROLE tubedepth LOGIN PASSWORD :password;
CREATE SCHEMA tubedepth AUTHORIZATION tubedepth;

-- Not cosmetic. Unqualified names resolve here, which is what puts this
-- service's `alembic_version` in its own schema instead of the one row every
-- service on this database would otherwise overwrite in turn.
ALTER ROLE tubedepth SET search_path = tubedepth;

-- autovacuum cannot clean up dead tuples newer than the oldest open
-- transaction, database-wide. A scraper holding one open across a network call
-- makes another service's tables bloat, with nothing pointing back here.
ALTER ROLE tubedepth SET idle_in_transaction_session_timeout = '30s';

-- The default `public` schema is writable by everyone. Leaving it that way
-- makes the schema separation above a naming convention rather than a boundary.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
