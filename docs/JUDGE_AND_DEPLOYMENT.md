# Judge walkthrough and deployment

## Judge: run a separate copy locally, without credentials

Use a fresh checkout of this revision. With Python 3.11 or newer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
PROOFPAY_MODE=judge .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

1. Open http://127.0.0.1:8000. Check the September 2026 historical synthetic fixture and the excluded maintenance window.
2. Click **Run simulated SLA review**. Expect 120 eligible downtime minutes out of 43,200, 99.7222% uptime, and a calculated $20 credit for a simulated $200 capture.
3. Verify that the page identifies the capture as simulated and offers no checkout or refund controls. No model, PayPal transaction, or merchant ledger is involved. The original submitted demo video can show the separate merchant-operated PayPal flow.

No environment file, buyer login, API key, or operator token is required. If a hosted judge walkthrough is needed, use a separate service in `PROOFPAY_MODE=judge` with no merchant environment group, credentials, or shared disk. Share the judge URL, never the merchant token or provider credentials. Do not switch the submitted service to judge mode.

## Merchant: preflight a separate staging service before any cutover

- Leave the submitted service and its deploy branch alone. The existing Blueprint is intentionally unchanged; use the dashboard to set up a separate paid web service for staging.
- Attach a Render persistent disk at `/var/data`, set `DATABASE_PATH=/var/data/proofpay.db`, and use one instance and one Uvicorn worker. The database and its SQLite WAL sidecar files must all be on the disk. The app checks the mount at startup in Render merchant mode. Free Render services cannot run this merchant configuration.
- Set `PUBLIC_BASE_URL` to that service's exact HTTPS origin (no path or query). Configure separate PayPal sandbox credentials, a private OpenAI API key, and a freshly generated private operator token. Store them in Render environment settings. Use a separate PayPal sandbox buyer for the walkthrough. Do not sync secrets into a judge service.
- Start with an empty staging DB. Before moving an existing merchant DB, pause new activity, reconcile in-flight records, and use SQLite's backup API (or the SQLite shell's `.backup`) to create a consistent copy. Copying just the database file while WAL is active can lose committed state. Do not overwrite a running database.
- On staging, run checkout, capture, investigation, and a single $20 refund. Restart the service and verify the same capture, review and refund state remains and a repeat refund is rejected. Verify the refund in PayPal sandbox activity. A review from before v0.2 has no capture/evidence binding and must be rerun; existing refunds still block duplicates.
- Attaching a disk can trigger a restart and prevents zero downtime deploys. Schedule any submitted-service change separately after staging verification. See [Render persistent disks](https://render.com/docs/disks).

Automated tests use temporary SQLite and mocked providers; they do not verify the submitted service or its disk.
