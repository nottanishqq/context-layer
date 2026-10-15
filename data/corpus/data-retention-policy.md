---
title: Data retention policy
kind: policy
tags: [policy, compliance, privacy]
---
Lumen Pay keeps data only as long as there is a legal or operational reason to.

| Data | Retention |
| --- | --- |
| Ledger journal entries | 10 years (financial record-keeping law) |
| Card data | Never stored; tokenised by the acquirer |
| Customer email and phone | Until account deletion plus 30 days |
| Notification delivery receipts | 30 days |
| Application logs | 14 days |
| Idempotency keys | Governed by the ledger TTL setting, see [[idempotency-keys]] |

Requests to delete personal data are handled by the privacy team within 30 days. Ledger entries are exempt from deletion but are pseudonymised.
