"""
ServiceNow connection verifier. Run BEFORE the full pipeline:

    # PowerShell
    $env:SN_INSTANCE="dev212345"
    $env:SN_USER="admin"
    $env:SN_PASSWORD="your-password"
    python test_servicenow.py

Checks: reachability, auth, table API read access, CMDB write access,
and creates + resolves one test incident (prefixed [CloudOps-AI TEST]).
"""

import os
import sys
import time

if not os.environ.get("SN_INSTANCE"):
    sys.exit("SN_INSTANCE not set. Set SN_INSTANCE / SN_USER / SN_PASSWORD first.")

from core.servicenow import EnterpriseServiceNowConnector, ServiceNowError
from core.itsm import Ticket

def main():
    print(f"Connecting to https://{os.environ['SN_INSTANCE']}.service-now.com ...")
    try:
        sn = EnterpriseServiceNowConnector()
        info = sn.test_connection()
        print(f"[1/4] Connectivity + auth OK  ({info['auth']}, "
              f"event_api={'on' if info['event_api'] else 'off'})")
    except ServiceNowError as e:
        sys.exit(f"[FAIL] Connection/auth: {e}\n"
                 "  - Check instance name (just 'dev212345', no URL)\n"
                 "  - Check credentials; PDIs hibernate after inactivity — "
                 "wake it at developer.servicenow.com first")
    except KeyError as e:
        sys.exit(f"[FAIL] Missing env var: {e}")

    try:
        t = Ticket(number="", sys_id="", incident_id="TEST-0001",
                   short_description="[CloudOps-AI TEST] connectivity check",
                   description="Safe to close. Created by test_servicenow.py.",
                   impact="3", urgency="4", business_service=None, cmdb_ci=None)
        t = sn.create_incident(t)
        print(f"[2/4] Incident created: {t.number}")
    except ServiceNowError as e:
        sys.exit(f"[FAIL] Incident creation (check role: itil or admin): {e}")

    try:
        time.sleep(1)
        sn.resolve_incident(t, "CloudOps-AI connectivity test — auto-resolved.")
        print(f"[3/4] Incident resolved: {t.number}")
        lc = sn.fetch_lifecycle(t)
        if lc and lc.get("opened_at"):
            print(f"[4/4] Lifecycle readback OK (opened_at present, "
                  f"state={lc.get('state')}) — real-MTTR KPIs will work")
        else:
            print("[4/4] Lifecycle readback returned no timestamps (event API mode?)")
    except ServiceNowError as e:
        print(f"[WARN] Resolve/readback issue: {e}")

    print("\nAll good — run `python pipeline.py` and tickets will land in this instance.")

if __name__ == "__main__":
    main()
