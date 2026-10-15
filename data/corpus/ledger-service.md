---
title: Ledger service
kind: service
tags: [payments, ledger, core]
---
The ledger service is the system of record for every money movement at Lumen Pay. Each payment, refund and payout becomes a pair of balanced journal entries: a debit on one account and a credit on another. Nothing else in the platform is allowed to change a balance directly.

## Architecture

The service is written in Go and runs as six replicas behind the internal gateway. Journal entries are written to the [[postgres-cluster]] in a single serializable transaction, then published to the `ledger.entries` topic on the [[queue-platform]] so downstream systems (reporting, notifications, fraud scoring) can react.

## Write path guarantees

Every write must carry an idempotency key. The ledger stores the key alongside the entry and rejects a second write with the same key and a different body with `ERR_LEDGER_409`. The full rules are in [[idempotency-keys]].

Entries are immutable. A mistake is corrected with a reversing entry, never an update, so the audit trail always reconciles.

## Operating it

Replication lag between the primary and the read replicas is the most common operational problem; the alert `LedgerReplicaLagHigh` fires above 30 seconds. Follow [[runbook-ledger-lag]] when it pages.

Owners: the payments team. Escalation: #payments-oncall.
