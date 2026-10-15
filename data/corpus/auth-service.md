---
title: Auth service
kind: service
tags: [security, identity]
---
The auth service issues and verifies every credential at Lumen Pay: merchant API keys, dashboard sessions for humans, and short-lived service tokens for internal calls.

## Tokens

Internal service tokens are JWTs signed with an RS256 key. Verifiers fetch the public keys from the JWKS endpoint at `/.well-known/jwks.json` and cache them for ten minutes. Signing keys are rotated on a schedule described in [[token-rotation]].

## Sessions

Dashboard sessions follow the [[session-policy]]: they idle out after 30 minutes and expire after 12 hours regardless of activity.

## Failure modes

If verifiers cannot validate a token they reject the request with `ERR_AUTH_401`. A sudden spike of `ERR_AUTH_401` across many services almost always means a key problem rather than a credential problem; see [[runbook-auth-outage]].
