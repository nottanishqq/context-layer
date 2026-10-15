---
title: Notification service
kind: service
tags: [messaging, email, customers]
---
The notification service sends every customer-facing message: payment receipts, payment confirmation emails, refund notices, payout SMS and webhook deliveries to merchants.

It does not send anything synchronously. Other services publish requests to the `notify.outbound` topic on the [[queue-platform]], and the notification workers consume them, render the template, and hand the message to the email or SMS provider.

## Regions

Workers run per region, one consumer group each: `notif-eu`, `notif-us` and `notif-apac`. Each region reads only its own partitions of `notify.outbound`, so a backlog in one region does not delay the others.

## Delivery expectations

Receipts and confirmations should reach the customer within one minute of the payment. Delivery receipts from the email provider are recorded for 30 days.
