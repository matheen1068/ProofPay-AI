# ProofPay Guarantee

**From broken service promises to accountable payments.** A PayPal AI Hackathon prototype that uses deterministic SLA calculations and an AI evidence investigator to support a human-approved sandbox refund.

> **Sandbox only.** All PayPal money is test money. Monitoring evidence is **synthetic**, not independently verified. This demo is not a production financial or SLA adjudication service.

## The demonstration
A customer buys a **$200** managed service with a **99.9%** uptime guarantee for the completed September 2026 synthetic scenario. Synthetic incidents yield **120 eligible downtime minutes** in a 43,200-minute month, resulting in **99.7222% uptime**. The documented demo contract assigns a **10% ($20)** credit for this tier. A genuine AI model produces an evidence-citing summary; the operator approves a real partial refund of the captured PayPal sandbox payment.

### Why it is distinct
- Ties an SLA to actual PayPal transaction provenance, not a generic invoice.
- Models overlapping intervals and maintenance exclusions, with transparent reproducible money math.
- AI investigates evidence but cannot control amounts or issue payment commands.
- Merchant approval and single-refund-per-capture invariant.
- Ambiguous external financial outcomes lock workflow for manual reconciliation rather than risking a double refund.

## Quick start
Requires Python 3.11+, a [PayPal sandbox developer app](https://developer.paypal.com/dashboard/applications/sandbox), a Personal sandbox buyer account, and an OpenAI API key.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with sandbox CLIENT_ID, CLIENT_SECRET, OPENAI_API_KEY, ADMIN_TOKEN (20+ chars), PUBLIC_BASE_URL
set -a; source .env; set +a
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000. Enter the `ADMIN_TOKEN` in the Operator access field, create PayPal checkout, approve using a Personal sandbox account, let the return callback capture the payment, run the AI investigation for that capture, then click **Approve PayPal refund**. Verify the refund ID in PayPal sandbox merchant activity.

`PUBLIC_BASE_URL` must be a secure HTTPS URL for a public deployment, or `http://localhost:8000` for local testing. The `ADMIN_TOKEN` is a demo operator token: do not expose it to public users. Never share the sandbox Client Secret or OpenAI key in frontend code.

## Test
```bash
pytest -q
```

## Technical details
- Backend: Python, FastAPI, HTTPX, SQLite.
- Frontend: responsive server-hosted HTML, CSS, JavaScript.
- AI: real OpenAI Chat Completions JSON API; defaults to `gpt-4.1-mini`.
- Payments: PayPal Sandbox OAuth2, Orders v2, Payments v2 partial refunds.
- Safeguards: explicit buyer approval, server-side capture validation, integer-cent policy calculations, merchant authorization, stable request IDs and duplicate refund lockout.

## Documentation
- [Architecture and boundaries](docs/ARCHITECTURE.md)
- [Acceptance tests](docs/ACCEPTANCE_TESTS.md)
- [Demo video outline](docs/DEMO_SCRIPT.md)

## Important limitations
The app has no live-payment mode, no independent monitoring source verification, no webhook reconciliation service, no multi-tenant auth, and no managed durable hosted database. A PayPal timeout locks the relevant workflow for manual investigation. The **live sandbox integration cannot be certified until you supply keys and run the full transaction**. Keep an actual deployment's SQLite file on persistent storage. On free hosts with ephemeral disks, data can disappear after restarts.

## Hackathon submission
Include this public repository and MIT license, an operational demo or precise setup steps, an explanatory tool list, and a public YouTube demo shorter than three minutes. Deadline: November 12, 2026, 12 PM PST (November 13, 2026, 1:30 AM IST).

## License
MIT — see [LICENSE](LICENSE).


## Staged v0.2.1 hardening

The `hardening/v0.2-staged` branch binds each AI review to a capture and evidence fingerprint, checks PayPal capture status before a refund, and preserves historical records while excluding them from the new scenario. Use this branch for staging.

See [isolated judge access and rollback](docs/JUDGE_ACCESS.md). Judge access requires a separate deployment, persistent storage and isolated sandbox credentials. Judge mode defaults to disabled. Automated provider mocks validate application behavior. Hosted v0.2.1 financial tests remain pending.
