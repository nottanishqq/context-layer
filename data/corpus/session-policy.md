---
title: Session policy
kind: policy
tags: [security, identity, policy]
---
This policy covers dashboard sessions for people (merchants and Lumen Pay staff). It does not cover API keys or service tokens.

- A session idles out after 30 minutes without activity.
- A session expires 12 hours after sign-in, even if active.
- Staff sessions require hardware-key MFA; merchant sessions require any second factor.
- Signing in from a new country forces re-authentication with MFA.
- Sessions are revoked immediately when a staff member is offboarded.

Session state is held by the [[auth-service]].
