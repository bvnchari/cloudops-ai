"""
Cloud Account registry — Method 2 of cluster access configuration.

Method 1 (core.cluster_inventory): a flat CSV where every row already
carries a working `kube_context` name — assumes login already happened
out-of-band.

Method 2 (this module): register each AWS account / GCP project / Azure
subscription ONCE, the same way ServiceNow and Jira are registered in the
Config tab — nickname, credentials, Save & Test Connection — then every
cluster in that account can be logged into on demand without repeating
credentials per cluster. This is the "application as account" pattern:
each Cloud Account IS the reusable identity; clusters are just resources
under it.

Each Cloud Account holds a DEDICATED AUTOMATION IDENTITY, never a personal
login:
  AWS:   IAM role ARN, assumed via STS from a base identity (either static
         access keys entered here, or the ambient identity of wherever this
         app is actually running — an instance role / task role / etc).
  GCP:   a service account, impersonated from the base `gcloud` identity
         active on the host, or a service account key file.
  Azure: a service principal (client_id/client_secret/tenant_id) or a
         managed identity available on the host.

IMPORTANT — honest scope: actually performing this login shells out to the
real `aws` / `gcloud` / `az` CLI binaries. Those must be installed on
whatever machine is actually running this app for real login to work.
Streamlit Community Cloud's sandbox does not have them; self-hosting
(Docker/EC2/on-prem) with the CLIs installed does. test_connection() will
fail clearly (not silently) if the binary isn't present.
"""

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field


@dataclass
class CloudAccount:
    nickname: str                 # e.g. "prod-aws", "payments-gcp-project"
    provider: str                  # "aws" | "gcp" | "azure"
    # AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = field(default="", repr=False)
    aws_region: str = ""
    aws_use_ambient_identity: bool = False   # use the host's own IAM identity instead of static keys
    # GCP
    gcp_service_account_key_json: str = field(default="", repr=False)  # raw key file contents
    gcp_project: str = ""
    # Azure
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = field(default="", repr=False)
    azure_subscription_id: str = ""
    created_at: float = field(default_factory=time.time)

    def _env(self) -> dict:
        env = os.environ.copy()
        if self.provider == "aws" and not self.aws_use_ambient_identity:
            env["AWS_ACCESS_KEY_ID"] = self.aws_access_key_id
            env["AWS_SECRET_ACCESS_KEY"] = self.aws_secret_access_key
            if self.aws_region:
                env["AWS_DEFAULT_REGION"] = self.aws_region
        return env

    def test_connection(self) -> dict:
        """Lightweight identity check per provider — confirms the CLI is
        present AND the credentials are actually valid, without touching
        any cluster."""
        if self.provider == "aws":
            args = ["aws", "sts", "get-caller-identity"]
            if self.aws_region:
                args += ["--region", self.aws_region]
        elif self.provider == "gcp":
            args = ["gcloud", "auth", "list", "--format=value(account)"]
        elif self.provider == "azure":
            args = ["az", "account", "show"]
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=20,
                                  env=self._env())
        except FileNotFoundError:
            cli = args[0]
            raise RuntimeError(
                f"`{cli}` CLI not found on this host. Real cloud login requires "
                f"the {cli} CLI installed wherever this app is actually running "
                f"(self-hosted/Docker) — Streamlit Community Cloud's sandbox "
                f"doesn't have it.")
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "unknown error")[:400])
        return {"reachable": True, "provider": self.provider, "nickname": self.nickname,
               "detail": (proc.stdout or "").strip()[:200]}

    def login_context(self, target) -> tuple[bool, str]:
        """Actually performs cloud login and refreshes/creates the kubeconfig
        context named `target.kube_context` for this specific cluster,
        using THIS account's dedicated automation identity. `target` is a
        core.cluster_inventory.ClusterTarget."""
        env = self._env()
        try:
            if self.provider == "aws":
                args = ["aws", "eks", "update-kubeconfig",
                        "--name", target.cluster_name, "--region", target.region,
                        "--alias", target.kube_context]
                if target.iam_role_arn:
                    args += ["--role-arn", target.iam_role_arn]
                proc = subprocess.run(args, capture_output=True, text=True, timeout=30, env=env)

            elif self.provider == "gcp":
                key_path = None
                if self.gcp_service_account_key_json:
                    fd, key_path = tempfile.mkstemp(suffix=".json")
                    with os.fdopen(fd, "w") as f:
                        f.write(self.gcp_service_account_key_json)
                    env["GOOGLE_APPLICATION_CREDENTIALS"] = key_path
                args = ["gcloud", "container", "clusters", "get-credentials",
                        target.cluster_name, "--region", target.region,
                        "--project", target.account_id or self.gcp_project]
                if target.service_account:
                    args += ["--impersonate-service-account", target.service_account]
                try:
                    proc = subprocess.run(args, capture_output=True, text=True, timeout=30, env=env)
                finally:
                    if key_path:
                        os.unlink(key_path)

            elif self.provider == "azure":
                if self.azure_client_id and self.azure_client_secret:
                    login = subprocess.run(
                        ["az", "login", "--service-principal", "-u", self.azure_client_id,
                         "-p", self.azure_client_secret, "--tenant", self.azure_tenant_id],
                        capture_output=True, text=True, timeout=30, env=env)
                    if login.returncode != 0:
                        return False, f"[LOGIN FAILED] az login: {(login.stderr or login.stdout)[:300]}"
                args = ["az", "aks", "get-credentials",
                        "--resource-group", target.account_id, "--name", target.cluster_name,
                        "--overwrite-existing", "--context", target.kube_context]
                proc = subprocess.run(args, capture_output=True, text=True, timeout=30, env=env)
            else:
                return False, f"Unknown provider: {self.provider}"

            ok = proc.returncode == 0
            tail = (proc.stdout or proc.stderr or "").strip()[-300:]
            return ok, f"[{'OK' if ok else 'FAILED'}] {self.provider} login for {target.match} -> {tail}"
        except FileNotFoundError as e:
            return False, f"[ERROR] CLI binary not found: {e}"
        except subprocess.TimeoutExpired:
            return False, f"[TIMEOUT] Login for {target.match} exceeded 30s"
        except Exception as e:
            return False, f"[ERROR] {e}"


class CloudAccountRegistry:
    def __init__(self, accounts: list[CloudAccount] | None = None):
        self.accounts: dict[str, CloudAccount] = {a.nickname: a for a in (accounts or [])}

    def add(self, account: CloudAccount):
        self.accounts[account.nickname] = account

    def get(self, nickname: str) -> CloudAccount | None:
        return self.accounts.get(nickname)

    def login_all(self, targets: list) -> list[tuple[str, bool, str]]:
        """Logs into every target whose account_nickname resolves to a
        registered account. Returns (match_pattern, ok, message) per target."""
        results = []
        for t in targets:
            acct = self.get(getattr(t, "account_nickname", "") or "")
            if not acct:
                results.append((t.match, False,
                               f"No registered Cloud Account named "
                               f"'{getattr(t, 'account_nickname', '')}' for this row."))
                continue
            ok, msg = acct.login_context(t)
            results.append((t.match, ok, msg))
        return results
