# Architecture and boundaries

```
Browser (operator-held token, no browser storage)
       |
FastAPI server + SQLite (persistent workflow state, event log)
       +-- SLA engine: UTC ranges, overlapping incidents, maintenance exclusions, integer cents
       +-- AI investigator: external OpenAI API, JSON + evidence IDs; advisory only
       +-- PayPal sandbox: OAuth, create order, buyer approval, capture, partial refund
```

## Payment lifecycle
1. Merchant operator creates the PayPal checkout order; order ID is stored in SQLite.
2. Customer logs in using a sandbox Personal account on PayPal's site and approves.
3. Return callback validates a known order ID; the backend captures with a stable `PayPal-Request-Id`, validates the capture amount/currency/status and saves its ID.
4. An actual model-generated investigation is required for review, but the model does NOT calculate money or authorize refunds.
5. An operator explicitly approves the deterministic SLA credit, which is capped by the captured payment. A unique DB record is created BEFORE the external refund call to prevent concurrent duplicate requests.
6. API timeouts or uncertain responses result in `RECONCILE` state and do NOT automatically retry.

## Limitations and next steps
This is a time-boxed prototype, not real contract enforcement. Synthetic event data is not independent monitoring evidence. Hardening requires proper tenant and role identity, server-side authenticated monitoring ingestion, policy change versioning, durable transaction reconciliation, refund status polling, capture-amount verification against PayPal's actual capture record before refund, webhook signature verification, customer-specific authorization, external DB hosting, rate limiting, CSRF mitigations where cookies are introduced, and secure deployment review. The single static operator token is demo-only and MUST NOT be distributed publicly.
