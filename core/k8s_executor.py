"""
Real execution backend — the actual "fixer" that core.remediation's
ReadOnlyExecutor deliberately is NOT.

This shells out to a real `kubectl` pointed at a real cluster (via a
kubeconfig you provide), runs the runbook's actions for real, and — this
is the part that matters — VERIFIES the fix worked with a real
`kubectl rollout status` check before RemediationEngine is allowed to mark
the incident resolved or let ITSM close the ticket. Running a command
isn't enough; if the rollout never becomes ready, this reports failure,
not success.

Scope, honestly stated:
  * Only supports actions whose command starts with `kubectl` — RB-001
    (pod restart/reschedule) and RB-003 (service restart) are pure kubectl
    and work end-to-end. RB-002 (node-level shell commands), RB-004 (AWS
    CLI node-group scaling), and RB-005 (SQL) are NOT executable by this
    backend — `supports()` returns False for them and RemediationEngine
    will not fabricate progress on a runbook it can't actually carry out.
  * Runbook commands use placeholders like `{node}` / `{deployment}`.
    This executor resolves them from the incident's `probable_root_cause`
    CI name by default — i.e. it assumes your CMDB CI name matches the
    real k8s resource name (node name / deployment name). Pass
    `ci_to_resource` to override that mapping for CIs whose k8s name
    differs from the CI id CloudOps-AI uses internally.
"""

import re
import shlex
import subprocess

from .remediation import Executor, RunbookAction


class KubernetesExecutor(Executor):
    is_real = True
    read_only = False

    def __init__(self, kubeconfig_path: str | None = None, context: str | None = None,
                 namespace: str = "default", timeout_s: int = 60,
                 ci_to_resource: dict[str, str] | None = None):
        self.kubeconfig_path = kubeconfig_path
        self.context = context
        self.namespace = namespace
        self.timeout_s = timeout_s
        self.ci_to_resource = ci_to_resource or {}

    def _base_args(self) -> list[str]:
        args = ["kubectl"]
        if self.kubeconfig_path:
            args += ["--kubeconfig", self.kubeconfig_path]
        if self.context:
            args += ["--context", self.context]
        return args

    def _resolve(self, ci_name: str | None) -> str:
        if not ci_name:
            return "unknown"
        return self.ci_to_resource.get(ci_name, ci_name)

    def supports(self, action: RunbookAction) -> bool:
        return action.command.strip().lower().startswith("kubectl")

    def build_context(self, incident) -> dict:
        resource = self._resolve(incident.probable_root_cause)
        return {"node": resource, "deployment": resource, "namespace": self.namespace}

    def run(self, action: RunbookAction, context: dict | None = None) -> tuple[bool, str]:
        if not self.supports(action):
            return False, (f"[UNSUPPORTED] {action.name}: `{action.command}` — no real "
                           f"executor available for this action type (not a kubectl "
                           f"command). Needs a human, or a different Executor "
                           f"(SSH/SSM for node-level, AWS CLI for scaling, DB client "
                           f"for SQL) wired in.")
        ctx = context or {}
        cmd_str = action.command.format(**{**ctx, "namespace": ctx.get("namespace", self.namespace)})
        # Ensure -n/--namespace is present for namespaced resources if not already given
        if "-n " not in cmd_str and "--namespace" not in cmd_str and "kubectl cordon" not in cmd_str \
                and "kubectl uncordon" not in cmd_str:
            cmd_str += f" -n {ctx.get('namespace', self.namespace)}"
        parts = self._base_args() + shlex.split(cmd_str)[1:]  # drop the literal "kubectl" from split
        try:
            proc = subprocess.run(parts, capture_output=True, text=True,
                                  timeout=min(action.timeout_s, self.timeout_s))
            ok = proc.returncode == 0
            tail = (proc.stdout or proc.stderr or "").strip()[-300:]
            return ok, f"[{'OK' if ok else 'FAILED'}] {action.name}: `{cmd_str}` -> {tail}"
        except subprocess.TimeoutExpired:
            return False, f"[TIMEOUT] {action.name}: `{cmd_str}` exceeded {action.timeout_s}s"
        except FileNotFoundError:
            return False, "[ERROR] kubectl not found on this host — install it or check PATH"
        except Exception as e:
            return False, f"[ERROR] {action.name}: `{cmd_str}` -> {e}"

    def verify(self, rb, incident, context: dict | None = None) -> tuple[bool, str]:
        """Real post-condition check: does the deployment actually report ready?
        This is what stops 'the command ran' from being confused with 'the
        problem is fixed' — a rollout can execute and still never converge."""
        ctx = context or self.build_context(incident)
        deployment = ctx.get("deployment", "unknown")
        namespace = ctx.get("namespace", self.namespace)
        args = self._base_args() + ["rollout", "status", f"deploy/{deployment}",
                                    "-n", namespace, f"--timeout={self.timeout_s}s"]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout_s + 10)
            ok = proc.returncode == 0
            tail = (proc.stdout or proc.stderr or "").strip()[-300:]
            return ok, f"[VERIFY {'PASSED' if ok else 'FAILED'}] rollout status deploy/{deployment} -> {tail}"
        except subprocess.TimeoutExpired:
            return False, f"[VERIFY TIMEOUT] deploy/{deployment} never reported ready within {self.timeout_s}s"
        except Exception as e:
            return False, f"[VERIFY ERROR] {e}"

    def test_connection(self) -> dict:
        args = self._base_args() + ["cluster-info"]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            raise RuntimeError(f"kubectl cluster-info failed: {(proc.stderr or proc.stdout)[:300]}")
        return {"reachable": True, "context": self.context or "(default)",
               "namespace": self.namespace, "info": proc.stdout.strip()[:200]}
