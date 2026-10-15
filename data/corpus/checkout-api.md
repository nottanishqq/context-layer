---
title: Checkout API
kind: service
tags: [payments, api, public]
---
The checkout API is the public HTTP API merchants call to create payment intents, confirm them and issue refunds. It is the busiest service at Lumen Pay, peaking at roughly 4,000 requests per second during the end-of-month billing window.

## Request flow

1. The merchant calls `POST /v2/payment_intents` with an `Idempotency-Key` header.
2. Checkout authenticates the merchant's API key against the [[auth-service]].
3. Checkout applies per-merchant [[rate-limiting]].
4. The card network is called through the acquirer adapter.
5. On success, checkout asks the [[ledger-service]] to record the movement, passing the merchant's idempotency key through unchanged.

## Retries

Merchant SDKs retry on network errors and HTTP 5xx with exponential backoff for up to 48 hours. Because a retry reuses the original idempotency key, a retried request must never create a second charge. This only holds if the ledger still remembers the key when the retry arrives.

## Error codes

- `ERR_CHECKOUT_402`: the card was declined by the issuer.
- `ERR_CHECKOUT_429`: the merchant exceeded its rate limit.
- `ERR_LEDGER_409`: passed through from the ledger when an idempotency key is reused with a different body.
