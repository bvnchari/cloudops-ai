"""
Cluster/Namespace Inventory — the missing piece between "an incident
happened" and "which real cluster and namespace does the fix apply to."

Enterprise pattern this models: a platform team maintains a registry of
every cluster/namespace CloudOps-AI is allowed to touch, each pre-mapped to
a kubeconfig context that was set up via the CLOUD PROVIDER'S OWN auth flow
using a DEDICATED, LEAST-PRIVILEGE AUTOMATION IDENTITY:

  AWS (EKS):   a dedicated IAM role (not a personal/admin user) mapped into
               the cluster's aws-auth ConfigMap with RBAC scoped to the
               target namespace, activated via:
                 aws eks update-kubeconfig --name <cluster> --role-arn <arn>
  GCP (GKE):   a dedicated service account (not a personal Google login)
               granted `roles/container.developer` (or narrower, namespace-
               scoped via GKE RBAC) on the target project, activated via:
                 gcloud container clusters get-credentials <cluster> \\
                   --project <project> --impersonate-service-account <sa>
  Azure (AKS): a dedicated managed identity or service principal granted
               `Azure Kubernetes Service RBAC Reader/Writer` scoped to the
               target namespace, activated via:
                 az aks get-credentials --resource-group <rg> --name <cluster>

CloudOps-AI deliberately does NOT reimplement any of these cloud auth SDKs
itself — that's real credential-issuance machinery best left to the cloud
provider's own tooling and your platform team's IAM policy, run ahead of
time by a human or a CI/CD pipeline. What this module DOES do is:
  1. Hold the resulting mapping (CI/service pattern -> which already-
     authenticated kubeconfig context + namespace to use), so the AI knows
     WHERE to act, not just THAT it should act.
  2. Resolve the correct target for a given incident.
  3. Force a real inventory-existence check (not just a name match) before
     KubernetesExecutor is allowed to run any action against it — this is
     the "validate the inventory, then login, then fix" step that was
     missing.
"""

import csv
import io
from dataclasses import dataclass, field


@dataclass
class ClusterTarget:
    match: str                  # CI name, business_service, or prefix+"*" pattern
    provider: str                # "aws" | "gcp" | "azure" | "self-managed"
    account_id: str               # AWS account / GCP project / Azure resource group
    region: str
    cluster_name: str
    namespace: str
    kube_context: str            # kubeconfig context name (created/refreshed by login, or must pre-exist)
    account_nickname: str = ""    # links to a registered core.cloud_accounts.CloudAccount (Method 2)
    iam_role_arn: str = ""        # AWS: role assumed to reach this cluster
    service_account: str = ""     # GCP: automation service account used (impersonation target)
    managed_identity: str = ""    # Azure: managed identity / SP used (documentation/audit only)
    notes: str = ""

    def matches(self, ci_name: str, business_service: str | None) -> bool:
        candidates = [c for c in (ci_name, business_service) if c]
        for c in candidates:
            if self.match == c:
                return True
            if self.match.endswith("*") and c.startswith(self.match[:-1]):
                return True
        return False


class ClusterInventory:
    def __init__(self, targets: list[ClusterTarget] | None = None):
        self.targets: list[ClusterTarget] = targets or []

    def resolve(self, incident) -> ClusterTarget | None:
        """First matching target for this incident's root-cause CI or
        business service — order matters, first match in upload order wins,
        so put more specific patterns before wildcard fallbacks."""
        ci_name = getattr(incident, "probable_root_cause", None) or ""
        biz = getattr(incident, "business_service", None)
        for t in self.targets:
            if t.matches(ci_name, biz):
                return t
        return None

    @classmethod
    def from_csv(cls, text: str) -> "ClusterInventory":
        reader = csv.DictReader(io.StringIO(text))
        targets = []
        for row in reader:
            targets.append(ClusterTarget(
                match=row.get("match", "").strip(),
                provider=row.get("provider", "self-managed").strip(),
                account_id=row.get("account_id", "").strip(),
                region=row.get("region", "").strip(),
                cluster_name=row.get("cluster_name", "").strip(),
                namespace=row.get("namespace", "default").strip(),
                kube_context=row.get("kube_context", "").strip(),
                account_nickname=row.get("account_nickname", "").strip(),
                iam_role_arn=row.get("iam_role_arn", "").strip(),
                service_account=row.get("service_account", "").strip(),
                managed_identity=row.get("managed_identity", "").strip(),
                notes=row.get("notes", "").strip(),
            ))
        return cls(targets)

    @classmethod
    def from_records(cls, records: list[dict]) -> "ClusterInventory":
        return cls([ClusterTarget(
            match=r.get("match", ""), provider=r.get("provider", "self-managed"),
            account_id=r.get("account_id", ""), region=r.get("region", ""),
            cluster_name=r.get("cluster_name", ""), namespace=r.get("namespace", "default"),
            kube_context=r.get("kube_context", ""), account_nickname=r.get("account_nickname", ""),
            iam_role_arn=r.get("iam_role_arn", ""),
            service_account=r.get("service_account", ""),
            managed_identity=r.get("managed_identity", ""), notes=r.get("notes", ""),
        ) for r in records])

    def to_rows(self) -> list[dict]:
        return [t.__dict__ for t in self.targets]


CSV_TEMPLATE = (
    "match,provider,account_id,region,cluster_name,namespace,kube_context,"
    "account_nickname,iam_role_arn,service_account,managed_identity,notes\n"
    "payments-api-*,aws,123456789012,us-east-1,prod-payments-eks,payments,"
    "prod-payments-eks,prod-aws,"
    "arn:aws:iam::123456789012:role/cloudops-ai-automation,,,"
    "Method 1: kube_context must pre-exist OR Method 2: leave kube_context "
    "as the desired alias and set account_nickname to a registered Cloud "
    "Account to log in on demand\n"
    "checkout-*,gcp,my-gcp-project,us-central1,checkout-gke,checkout,"
    "checkout-gke,checkout-gcp,,"
    "cloudops-ai-automation@my-gcp-project.iam.gserviceaccount.com,,"
    "roles/container.developer scoped via GKE RBAC\n"
)
