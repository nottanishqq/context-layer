---
title: Rate limiting
kind: concept
tags: [api, reliability]
---
The [[checkout-api]] limits each merchant to a token bucket of 100 requests per second with a burst of 200. Merchants on the enterprise plan can request a higher limit through their account manager.

When a merchant exceeds the limit the API returns HTTP 429 with error code `ERR_CHECKOUT_429` and a `Retry-After` header. Limits are enforced at the edge gateway, so throttled requests never reach the card networks or the ledger.

Rate-limit counters live in a Redis cluster shared across gateway replicas. If Redis is unavailable the gateway fails open (it stops limiting) rather than rejecting traffic, and pages the platform on-call.
