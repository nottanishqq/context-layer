---
title: Signing key rotation
kind: procedure
tags: [security, identity, keys]
---
JWT signing keys for internal service tokens are rotated every 30 days by the `keyrotator` cron job, which runs at 02:00 UTC on the first day of each month.

## The overlap window

Rotation is a two-step process. First, the new public key is published to the JWKS document while the old key keeps signing. Only after an overlap window of at least 24 hours does the [[auth-service]] start signing with the new key. The overlap exists because verifiers cache the JWKS document; a verifier that has not refreshed its cache must still be able to validate tokens.

If the overlap is skipped, any verifier with a stale cache rejects every new token until its cache expires.

## Manual rotation

Emergency rotation (for a suspected key compromise) is triggered with `keyrotator rotate --emergency`. Emergency rotation skips the overlap window, so expect a short burst of authentication failures and warn the on-call channel first.

## Ownership

The identity team owns `keyrotator`. Its configuration lives in the `infra/identity` repository.
