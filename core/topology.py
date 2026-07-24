"""
Phase 1/2 support — Topology & Service Dependency Map (CMDB-lite).

Models an enterprise environment: infra -> platform -> app -> business service.
Used by the correlation engine for topology-based event correlation
and by the ITSM layer for CMDB sync / service mapping.
"""

from dataclasses import dataclass, field


@dataclass
class ConfigurationItem:
    ci_id: str
    name: str
    ci_type: str          # node | pod | service | database | loadbalancer | network
    layer: str            # infrastructure | platform | application | data | network
    business_service: str
    depends_on: list = field(default_factory=list)  # upstream ci_ids


class TopologyMap:
    """CMDB-lite: service dependency graph for topology-based correlation."""

    def __init__(self):
        self.cis: dict[str, ConfigurationItem] = {}

    def add(self, ci: ConfigurationItem):
        self.cis[ci.ci_id] = ci

    def get(self, ci_id: str) -> ConfigurationItem | None:
        return self.cis.get(ci_id)

    def downstream_of(self, ci_id: str) -> list[str]:
        """All CIs that (transitively) depend on ci_id — blast radius."""
        impacted, stack = set(), [ci_id]
        while stack:
            current = stack.pop()
            for cid, ci in self.cis.items():
                if current in ci.depends_on and cid not in impacted:
                    impacted.add(cid)
                    stack.append(cid)
        return sorted(impacted)

    def shared_root(self, ci_ids: list[str]) -> str | None:
        """Find a common upstream dependency across a set of CIs (probable root cause)."""
        if not ci_ids:
            return None
        ancestor_sets = []
        for cid in ci_ids:
            ancestors, stack = set(), [cid]
            while stack:
                cur = stack.pop()
                ci = self.cis.get(cur)
                if not ci:
                    continue
                for dep in ci.depends_on:
                    if dep not in ancestors:
                        ancestors.add(dep)
                        stack.append(dep)
            ancestors.add(cid)
            ancestor_sets.append(ancestors)
        common = set.intersection(*ancestor_sets)
        if not common:
            return None
        # deepest common ancestor = the one with most dependencies of its own
        return max(common, key=lambda c: len(self.cis[c].depends_on) if c in self.cis else -1)


def build_demo_topology() -> TopologyMap:
    """Enterprise-style demo environment: EKS cluster running a payments platform."""
    t = TopologyMap()
    cis = [
        # Infrastructure layer
        ConfigurationItem("node-01", "eks-node-01", "node", "infrastructure", "Payments Platform"),
        ConfigurationItem("node-02", "eks-node-02", "node", "infrastructure", "Payments Platform"),
        ConfigurationItem("alb-01", "payments-alb", "loadbalancer", "network", "Payments Platform"),
        # Data layer
        ConfigurationItem("rds-01", "payments-rds-primary", "database", "data", "Payments Platform",
                          depends_on=["node-01"]),
        # Platform layer
        ConfigurationItem("pod-api-1", "payments-api-7d9f-1", "pod", "platform", "Payments Platform",
                          depends_on=["node-01"]),
        ConfigurationItem("pod-api-2", "payments-api-7d9f-2", "pod", "platform", "Payments Platform",
                          depends_on=["node-02"]),
        ConfigurationItem("pod-worker-1", "payments-worker-5c2a-1", "pod", "platform", "Payments Platform",
                          depends_on=["node-01"]),
        # Application layer
        ConfigurationItem("svc-api", "payments-api-svc", "service", "application", "Payments Platform",
                          depends_on=["pod-api-1", "pod-api-2", "alb-01"]),
        ConfigurationItem("svc-worker", "payments-worker-svc", "service", "application", "Payments Platform",
                          depends_on=["pod-worker-1", "rds-01"]),
        ConfigurationItem("svc-checkout", "checkout-frontend", "service", "application", "Payments Platform",
                          depends_on=["svc-api"]),
    ]
    for ci in cis:
        t.add(ci)
    return t
