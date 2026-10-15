---
title: "Runbook: ledger replica lag"
kind: runbook
tags: [runbook, payments, database, on-call]
visibility: team
team: payments
---
Use this runbook when `LedgerReplicaLagHigh` pages.

Replica lag means the read replicas of the [[postgres-cluster]] are behind the primary. Balance reads served from a lagging replica can be stale, which shows up as merchants seeing a payout that has not reflected in their balance.

## Steps

1. Check whether a long-running analytical query is holding back replay on the replica (`pg_stat_activity`, look for queries older than five minutes). Cancel it.
2. Check primary write volume. A bulk backfill without throttling is the usual cause; pause it.
3. If one replica is far behind the others, remove it from the read pool with `pgpool detach <replica>` and let it catch up.
4. While lag exceeds 120 seconds, set the ledger flag `READS_FROM_PRIMARY=true` so balance reads go to the primary.

Revert `READS_FROM_PRIMARY` once lag is under 10 seconds for fifteen minutes.
