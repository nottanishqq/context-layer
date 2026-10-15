---
title: Incident severity levels
kind: policy
tags: [on-call, process, incidents]
---
- **SEV1**: money is moving incorrectly (double charges, lost payouts) or the checkout API is down for all merchants. All hands; executive notified within 15 minutes.
- **SEV2**: a major feature is degraded for many merchants, or customer messages are delayed by more than an hour. Incident commander required.
- **SEV3**: a minor feature is degraded, or a single merchant is affected. Handled by the on-call during working hours.
- **SEV4**: cosmetic issues and near misses. Tracked as a ticket.

Every SEV1 and SEV2 needs a written incident report within five working days.
