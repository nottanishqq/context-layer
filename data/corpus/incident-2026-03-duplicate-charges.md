---
title: "Incident report: duplicate charges (March 2026)"
kind: incident
tags: [incident, payments, sev1]
date: 2026-03-14
---
## Summary

Between 11 and 14 March 2026, 1,284 customers were charged twice for a single purchase. Total over-collection was about 212,000 EUR, all refunded by 16 March. Severity: SEV1.

## What happened

A merchant SDK release changed the retry schedule so that the final retry fired 80 hours after the original request. Requests that had timed out at the acquirer were retried by the SDK with the same idempotency key, but by the time the final retry reached the [[checkout-api]] the ledger no longer recognised the key, so the retry was processed as a new payment.

## Root cause

The idempotency key retention window was shorter than the client retry window. See [[idempotency-keys]] for how retention is configured.

## Actions

- Raise retention so it always exceeds the longest SDK retry window.
- Add a release check that compares SDK retry schedules against the retention setting.
- Alert when a payment matches an earlier payment's amount, card and merchant within 7 days.
