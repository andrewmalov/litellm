# Forced password change UI fix

Released 2026-11-16, source commit `3d3b67795d`

Image: `cr.selcloud.ru/ttkdc/litellm@sha256:2248dc77ce41b5b80df2a33125c80f7ce380e620eb348df95c308b75cfe918e2`

A restricted local login could keep the preceding React auth context after updating its cookie. Protected dashboard consumers then mounted and received HTTP 403, including organization budget summary. The login hook now updates context before navigation. Dashboard and chat wait for resolved claims, redirect restricted sessions to change-password and keep management consumers unmounted. Trailing slashes on the password page no longer cause a redirect loop

Validation: 43 tests in seven files pass, production TypeScript and build pass, ESLint has zero errors and two pre-existing warnings. Logs are `/tmp/litellm-forced-password-{tests,types,lint,build}.log`. The requested independent reviewer was unavailable due to an external 502 response; code was inspected locally and the full flow was exercised against production

Browser acceptance used a temporary ordinary internal user. An admin-set temporary password caused login to redirect to `/ui/change-password/`, with zero HTTP 403 dashboard requests. Submitting the current and new passwords signed the user out. A new login with the new password opened API Keys and showed the organization's 100 USD budget and server reset time. Test organization, budget, invitation, user and sessions were removed using supported APIs. The reported user's password, reset flag, organization, role and counters were not modified

The previous image is `cr.selcloud.ru/ttkdc/litellm@sha256:bc58b4b5cff1554596e251415877ffe3ef44c4bf20b2d70bc86f8eef59d4e23f`. Deployment configuration and PostgreSQL backup are saved under `/root/litellm-backups/20261116-password-reset-ui`. The earlier verified Redis backup is retained. Deployment drained ingress, observed zero pool operations and replaced only proxy with graceful stop. Rollback script `/root/litellm-backups/20261116-password-reset-ui/rollback.py` restores the previous image using the same drain procedure

An existing browser tab may retain old JavaScript until a full reload. Reload, then use the normal password-change flow and sign in again. The existing session restrictions intentionally remain enforced
