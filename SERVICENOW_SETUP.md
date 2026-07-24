# Real ServiceNow Setup — CloudOps-AI

## Step 1 — Get a free instance (10 min)
1. Sign up at developer.servicenow.com (free).
2. Request a **Personal Developer Instance (PDI)** — you get a URL like
   `https://dev212345.service-now.com` and an admin password.
3. Note: PDIs **hibernate** after ~30 min idle and are reclaimed after ~10 days
   of inactivity. Wake it from the developer portal before demos.

## Step 2 — Create an integration user (recommended over admin)
In your PDI: **User Administration → Users → New**
- User ID: `cloudops.integration`
- Set a password, uncheck "Web service access only" is fine either way
- Roles: add **itil** (incident create/update) and **cmdb_ci** access
  (or just use `admin` for a quick demo — fine on a PDI, never in production).

## Step 3 — Set environment variables

PowerShell (per session):
```powershell
$env:SN_INSTANCE = "dev212345"          # instance name only, no URL
$env:SN_USER     = "cloudops.integration"
$env:SN_PASSWORD = "your-password"
```

Persistent (PowerShell, survives restarts):
```powershell
[Environment]::SetEnvironmentVariable("SN_INSTANCE","dev212345","User")
[Environment]::SetEnvironmentVariable("SN_USER","cloudops.integration","User")
[Environment]::SetEnvironmentVariable("SN_PASSWORD","your-password","User")
```
(Open a new terminal after setting persistent vars.)

Optional:
```powershell
$env:SN_USE_EVENT_API = "1"   # ITOM Event Management path (needs Event Mgmt
                              # plugin — NOT on PDIs; use on licensed instances)
$env:SN_CLIENT_ID     = "..." # switch to OAuth2 (System OAuth → Application
$env:SN_CLIENT_SECRET = "..." #   Registry → New → OAuth API endpoint)
```

## Step 4 — Verify, then run
```powershell
.\venv\Scripts\Activate.ps1
python test_servicenow.py     # 4-step connectivity check
python pipeline.py            # full run — tickets land in your PDI
```
Then check in ServiceNow: **Incident → All**, filter Category = AIOps.
You'll see the correlated incidents with RCA + folded-alert details in the
description, auto-resolved with close notes referencing the runbook.

## What "enterprise-level" means in this connector
- **Retries + backoff** on 429/5xx/network errors (3 attempts: 1s/2s/4s)
- **OAuth2** password-grant with automatic token refresh on 401
- **Idempotent CMDB sync** — upserts by name, safe to re-run
- **ITOM-correct event path** — on licensed instances, pushes to `em_event`
  with a `message_key` dedup key and clearing events, letting ServiceNow's
  own event rules correlate (the pattern real enterprises deploy)
- **KPI truth** — MTTR is computed from `opened_at`/`resolved_at` read back
  from ServiceNow, not from simulated timing

## Interview framing
"The platform integrates with ServiceNow through a resilient REST connector —
OAuth2, retry with exponential backoff, idempotent CMDB upserts. On licensed
instances it publishes to the ITOM Event Management API with dedup keys and
clearing events so ServiceNow's event rules do the correlation; on standard
instances it falls back to Table API incident lifecycle management. MTTR
reporting reads actual ticket timestamps back from ServiceNow rather than
trusting the sender."
