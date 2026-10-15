---
title: "Runbook: Relay consumer lag"
kind: runbook
tags: [runbook, messaging, on-call]
---
Use this runbook when `RelayConsumerLagHigh` pages, or when a consumer group is falling behind.

## 1. Find the group

Open the Relay dashboard and sort consumer groups by lag. Note which group and which partitions are behind. If only one partition is behind, suspect a poison message; if all partitions are behind, suspect capacity.

## 2. Poison message

Check the group's dead-letter count. If one offset is being retried in a loop, move it to the dead-letter topic with `relayctl skip --group <group> --partition <n> --offset <o>` and open a bug against the owning service.

## 3. Capacity

Scale the consumer deployment: `kubectl scale deploy/<group>-worker --replicas=<n>`. A group can use at most one consumer per partition, so scaling beyond the partition count does nothing; if you are already at the partition count, ask the platform team to add partitions.

## 4. Confirm

Lag should start falling within five minutes. Leave the incident open until the group is back under 1,000 messages.
