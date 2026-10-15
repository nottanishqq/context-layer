---
title: Postgres cluster
kind: service
tags: [infrastructure, database]
visibility: team
team: platform
---
The primary Postgres cluster (`pg-core`) holds the ledger, merchant accounts and idempotency keys. It runs PostgreSQL 17 with one primary and three streaming read replicas across two availability zones.

## Failover

Failover is automatic: Patroni promotes the most up-to-date replica if the primary is unreachable for 30 seconds. Applications connect through PgBouncer, which follows the new primary within a few seconds. Expect writes to fail for up to 45 seconds during a failover.

## Backups

Full backups run nightly at 01:00 UTC with continuous WAL archiving, giving point-in-time recovery for 14 days. Restore drills run quarterly.

## Access

Direct access requires a break-glass request approved by a platform engineer. Read-only analytics should use the warehouse, never the replicas.
