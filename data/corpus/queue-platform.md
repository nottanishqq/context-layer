---
title: Relay queue platform
kind: service
tags: [infrastructure, messaging]
---
Relay is Lumen Pay's internal event streaming platform, a managed Kafka deployment with a thin client library on top. Producers publish to topics; consumer groups read them at their own pace.

## Topics that matter

- `ledger.entries`: every journal entry from the [[ledger-service]].
- `notify.outbound`: messages for the [[notification-service]], partitioned by region (`eu`, `us`, `apac`).
- `fraud.signals`: scores from the fraud models.

## Consumer lag

Consumer lag is the number of messages a consumer group has not yet processed. Lag is normal during traffic spikes but should drain within minutes. Sustained lag means consumers are slower than producers: too few consumers, a slow downstream dependency, or a poison message being retried. The alert `RelayConsumerLagHigh` fires when a group is more than 50,000 messages behind for ten minutes. Use [[runbook-consumer-lag]].

Owners: the platform team.
