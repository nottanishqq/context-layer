---
title: "Runbook: authentication outage"
kind: runbook
tags: [runbook, security, on-call]
---
Use this runbook when many services report `ERR_AUTH_401` at once.

## Triage

1. Check the time. Failures that begin within an hour of 02:00 UTC on the first of the month point to the scheduled key rotation in [[token-rotation]].
2. Fetch `/.well-known/jwks.json` and compare the key IDs (`kid`) against the `kid` in a failing token's header. If the signing key is missing from the published set, verifiers cannot validate it.

## Mitigation

- Republish the previous public key to the JWKS document with `keyrotator publish --previous`.
- Force verifiers to refresh: `keyrotator flush-caches`.
- Do not rotate again until the cause is understood.

## Afterwards

File an incident report and link it from this runbook.
