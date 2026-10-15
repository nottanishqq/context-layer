---
title: Idempotency keys
kind: concept
tags: [payments, reliability]
---
An idempotency key is a client-chosen unique string that makes a write safe to retry. If the server sees the same key twice with the same request body, it returns the original result instead of performing the operation again.

## How the ledger stores keys

The [[ledger-service]] keeps each key in the `idempotency_keys` table with the hash of the request body. A repeat with an identical body returns the stored response. A repeat with a different body is rejected with `ERR_LEDGER_409`, because the client has almost certainly reused a key by mistake.

## Key retention

Keys are retained for the duration set by `LEDGER_IDEMPOTENCY_TTL`. The default is 72 hours. The TTL must always be longer than the longest client retry window; if a key expires before the final retry arrives, the retry looks like a brand-new request and the customer is charged twice.

Changing `LEDGER_IDEMPOTENCY_TTL` requires a payments team review, because shortening it trades storage cost against double-charge risk.

## Guidance for clients

Generate one key per logical operation (a UUIDv4 is fine), store it with the order, and reuse it for every retry of that operation.
