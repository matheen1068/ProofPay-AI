# Acceptance test plan

## Automated (run `pytest -q`)
- 30-day UTC period is exactly 43,200 minutes.
- 120 eligible downtime minutes yields 99.7222% uptime and a $20 contractual credit against a $200 fee.
- Overlapping incidents are counted once.
- Planned maintenance exclusions subtract only the overlapping interval.
- Zero eligible downtime yields a zero credit.
- Invalid temporal intervals are rejected.
- Contractual refund percentage is bounded.
- Missing or incorrect operator token cannot invoke writes.
- Production PayPal base URL is rejected.

## Manual integration (requires your actual credentials)
- Create $200 order in PayPal sandbox; buyer uses Personal sandbox account to approve.
- Confirm Order status COMPLETED and recorded Capture ID in app and seller activity.
- Configure actual model key; investigation cites real fixture incident IDs and explicitly says evidence is synthetic.
- Approve $20 credit and confirm PayPal refund ID/status in seller activity.
- Click refund again; application blocks duplicate request.
- Simulate payment network timeout; verify captured payment or refund is marked for reconciliation rather than silently retried.
- Verify secrets are absent from repo, logs, browser source, and recorded demo video.

## Known gaps
Automated tests do not prove a successful PayPal capture or refund; that can only be verified using configured sandbox credentials. AI and network error-path tests should be expanded before any production use. No PayPal webhook verification is implemented; payment truth is confirmed from synchronous provider responses only.
