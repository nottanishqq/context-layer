---
title: On-call handbook
kind: policy
tags: [on-call, process]
---
Every engineering team runs a weekly on-call rotation. The primary on-call acknowledges pages within 5 minutes; the secondary is paged if the primary has not acknowledged within 15.

## When you are paged

1. Acknowledge the page.
2. Decide the severity using [[severity-levels]].
3. Open the matching runbook. Runbooks for the most common pages are [[runbook-consumer-lag]], [[runbook-ledger-lag]] and [[runbook-auth-outage]].
4. For SEV1 and SEV2, open an incident channel and name an incident commander.

## Handover

At the end of the week, write a handover note listing open incidents, noisy alerts and anything the next person should watch.

On-call engineers are compensated with a fixed weekly stipend plus time off in lieu for any page between midnight and 06:00 local time.
