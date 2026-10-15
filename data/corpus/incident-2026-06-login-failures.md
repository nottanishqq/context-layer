---
title: "Incident report: internal login failures (June 2026)"
kind: incident
tags: [incident, security, sev2]
date: 2026-06-01
---
## Summary

On 1 June 2026 from 02:04 to 02:51 UTC, most internal services rejected calls from each other, and staff could not reach the admin dashboard. Merchant-facing payments were unaffected because the checkout path uses API keys. Severity: SEV2.

## What happened

The monthly key rotation ran with a configuration change that had accidentally set the overlap window to zero. The [[auth-service]] began signing with a key that verifiers had never seen, and every verifier with a cached key set rejected the new tokens.

## Resolution

The on-call followed [[runbook-auth-outage]], republished the previous key and flushed verifier caches.

## Actions

- Validate that the overlap window is at least 24 hours before rotation proceeds (see [[token-rotation]]).
- Lower the JWKS cache lifetime from ten minutes to five.
