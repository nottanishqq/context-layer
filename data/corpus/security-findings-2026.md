---
title: Security findings register 2026
kind: register
tags: [security, confidential]
visibility: team
team: security
---
Open findings from the 2026 penetration test and internal reviews. Confidential: security team only.

- **SF-2026-04 (high)**: the `keyrotator --emergency` path can be triggered by any member of the identity team without a second approver. Fix: require two-person approval. Owner: identity team. Due: Q4 2026.
- **SF-2026-07 (medium)**: Redis used for rate limiting has no authentication inside the VPC. If an attacker gains a foothold they could reset rate-limit counters. Owner: platform team.
- **SF-2026-09 (low)**: notification templates allow arbitrary HTML in merchant-supplied fields. Owner: messaging team.

See [[token-rotation]] and [[rate-limiting]] for the affected systems.
