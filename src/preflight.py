"""
Pre-flight checks for Nginx Ingress → AWS Gateway API migration.
Validates LBC version, Gateway API channels, AWS CRDs, feature gates, and ACM certs.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from packaging.version import Version, InvalidVersion


@dataclass
class CheckResult:
    name: str
    status: str        # "ok" | "warning" | "error"
    message: str
    detail: str = ""


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(c.status == "error" for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.status == "warning" for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "blocking": self.blocking,
            "has_warnings": self.has_warnings,
            "checks": [
                {"name": c.name, "status": c.status, "message": c.message, "detail": c.detail}
                for c in self.checks
            ],
        }


# ── version helpers ───────────────────────────────────────────────────────────

def _parse_version(v: str) -> Version | None:
    try:
        return Version(v.lstrip("v"))
    except (InvalidVersion, TypeError):
        return None


def check_lbc_version(
    lbc_version: str,
    needs_l7: bool = True,
    needs_l4: bool = False,
) -> list[CheckResult]:
    results = []
    ver = _parse_version(lbc_version)

    if ver is None:
        results.append(CheckResult(
            name="lbc-version",
            status="error",
            message=f"Cannot parse LBC version: '{lbc_version}'",
            detail="Provide version in semver format, e.g. '2.14.1'",
        ))
        return results

    if needs_l7:
        min_l7 = Version("2.14.0")
        if ver < min_l7:
            results.append(CheckResult(
                name="lbc-version-l7",
                status="error",
                message=f"LBC {lbc_version} < 2.14.0: HTTPRoute/GRPCRoute not supported",
                detail="Upgrade AWS Load Balancer Controller to ≥2.14.0 before migrating L7 routes",
            ))
        else:
            results.append(CheckResult(
                name="lbc-version-l7",
                status="ok",
                message=f"LBC {lbc_version} ≥ 2.14.0: L7 (HTTPRoute/GRPCRoute) supported",
            ))

    if needs_l4:
        min_l4 = Version("2.13.3")
        if ver < min_l4:
            results.append(CheckResult(
                name="lbc-version-l4",
                status="error",
                message=f"LBC {lbc_version} < 2.13.3: TCPRoute/UDPRoute/TLSRoute not supported",
                detail="Upgrade AWS Load Balancer Controller to ≥2.13.3 for L4 routes",
            ))
        else:
            results.append(CheckResult(
                name="lbc-version-l4",
                status="ok",
                message=f"LBC {lbc_version} ≥ 2.13.3: L4 routes supported",
            ))

    return results


def check_gateway_api_channels(
    standard_channel_installed: bool | None,
    experimental_channel_installed: bool | None,
    needs_experimental: bool = False,
) -> list[CheckResult]:
    results = []

    if standard_channel_installed is False:
        results.append(CheckResult(
            name="gateway-api-standard-channel",
            status="error",
            message="Gateway API standard channel CRDs not installed",
            detail="Install: kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.5.0/standard-install.yaml",
        ))
    elif standard_channel_installed is True:
        results.append(CheckResult(
            name="gateway-api-standard-channel",
            status="ok",
            message="Gateway API standard channel CRDs installed",
        ))
    else:
        results.append(CheckResult(
            name="gateway-api-standard-channel",
            status="warning",
            message="Gateway API standard channel installation not verified",
            detail="Cannot check cluster state — verify manually",
        ))

    if needs_experimental:
        if experimental_channel_installed is False:
            results.append(CheckResult(
                name="gateway-api-experimental-channel",
                status="error",
                message="Gateway API experimental channel not installed (required for TCPRoute/UDPRoute/TLSRoute/BackendTLSPolicy)",
                detail="Install: kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.5.0/experimental-install.yaml",
            ))
        elif experimental_channel_installed is True:
            results.append(CheckResult(
                name="gateway-api-experimental-channel",
                status="ok",
                message="Gateway API experimental channel installed",
            ))
        else:
            results.append(CheckResult(
                name="gateway-api-experimental-channel",
                status="warning",
                message="Experimental channel installation not verified (needed for L4 routes)",
                detail="Verify: kubectl get crd tcproutes.gateway.networking.k8s.io",
            ))
    else:
        results.append(CheckResult(
            name="gateway-api-experimental-channel",
            status="ok",
            message="Experimental channel not required for this migration (no L4/BackendTLSPolicy resources)",
        ))

    return results


def check_aws_crds(
    crds_installed: dict[str, bool] | None = None,
) -> list[CheckResult]:
    """
    crds_installed: {"LoadBalancerConfiguration": True/False, ...}
    If None, returns warnings (cannot verify without cluster access).
    """
    required_crds = [
        "LoadBalancerConfiguration",
        "TargetGroupConfiguration",
        "ListenerRuleConfiguration",
    ]
    results = []

    for crd in required_crds:
        if crds_installed is None:
            results.append(CheckResult(
                name=f"aws-crd-{crd.lower()}",
                status="warning",
                message=f"Cannot verify {crd} CRD installation",
                detail=f"Verify: kubectl get crd {crd.lower()}s.gateway.k8s.aws",
            ))
        elif crds_installed.get(crd, False):
            results.append(CheckResult(
                name=f"aws-crd-{crd.lower()}",
                status="ok",
                message=f"{crd} CRD installed",
            ))
        else:
            results.append(CheckResult(
                name=f"aws-crd-{crd.lower()}",
                status="error",
                message=f"{crd} CRD not installed",
                detail=f"Install or upgrade AWS Load Balancer Controller. CRD: {crd}",
            ))

    return results


def check_feature_gates(
    alb_gateway_enabled: bool | None = None,
    nlb_gateway_enabled: bool | None = None,
    needs_l4: bool = False,
) -> list[CheckResult]:
    results = []

    if alb_gateway_enabled is False:
        results.append(CheckResult(
            name="feature-gate-alb",
            status="error",
            message="ALBGatewayAPI feature gate is disabled",
            detail="Enable: --feature-gates=ALBGatewayAPI=true on the LBC deployment",
        ))
    elif alb_gateway_enabled is None:
        results.append(CheckResult(
            name="feature-gate-alb",
            status="warning",
            message="ALBGatewayAPI feature gate status unknown",
            detail="Verify LBC deployment args do not include ALBGatewayAPI=false",
        ))
    else:
        results.append(CheckResult(
            name="feature-gate-alb", status="ok",
            message="ALBGatewayAPI feature gate enabled",
        ))

    if needs_l4:
        if nlb_gateway_enabled is False:
            results.append(CheckResult(
                name="feature-gate-nlb",
                status="error",
                message="NLBGatewayAPI feature gate is disabled",
                detail="Enable: --feature-gates=NLBGatewayAPI=true on the LBC deployment",
            ))
        elif nlb_gateway_enabled is None:
            results.append(CheckResult(
                name="feature-gate-nlb",
                status="warning",
                message="NLBGatewayAPI feature gate status unknown",
                detail="Verify LBC deployment args do not include NLBGatewayAPI=false",
            ))
        else:
            results.append(CheckResult(
                name="feature-gate-nlb", status="ok",
                message="NLBGatewayAPI feature gate enabled",
            ))

    return results


def check_acm_certs(tls_hostnames: list[str]) -> list[CheckResult]:
    """
    For each TLS hostname, generate a check reminder.
    (Actual ACM lookup requires AWS credentials — generate actionable warnings.)
    """
    if not tls_hostnames:
        return [CheckResult(
            name="acm-certs",
            status="ok",
            message="No TLS hostnames detected — no ACM certificates required",
        )]

    from collections import Counter
    domain_counts: Counter = Counter()
    for h in tls_hostnames:
        parts = h.split(".")
        if len(parts) >= 2:
            domain_counts[".".join(parts[1:])] += 1

    results = []
    covered: set[str] = set()
    for domain, count in domain_counts.items():
        if count > 1:
            wc = f"*.{domain}"
            hosts = [h for h in tls_hostnames if h.endswith(f".{domain}")]
            results.append(CheckResult(
                name=f"acm-cert-{domain.replace('.', '-')}",
                status="warning",
                message=f"ACM wildcard cert needed: {wc} (covers {count} hosts)",
                detail=f"Hosts: {', '.join(hosts)}. "
                       f"Issue/import in ACM and ensure it is in the same region as EKS.",
            ))
            covered.update(hosts)
        else:
            h = next((h for h in tls_hostnames if h.endswith(f".{domain}") or h == domain), None)
            if h and h not in covered:
                results.append(CheckResult(
                    name=f"acm-cert-{h.replace('.', '-').replace('*', 'wildcard')}",
                    status="warning",
                    message=f"ACM certificate needed: {h}",
                    detail="Issue/import in ACM. Cert is discovered automatically by hostname match.",
                ))

    return results


def run_all(
    lbc_version: str = "",
    needs_l7: bool = True,
    needs_l4: bool = False,
    standard_channel_installed: bool | None = None,
    experimental_channel_installed: bool | None = None,
    aws_crds_installed: dict | None = None,
    alb_gateway_enabled: bool | None = None,
    nlb_gateway_enabled: bool | None = None,
    tls_hostnames: list[str] | None = None,
) -> PreflightReport:
    report = PreflightReport()

    if lbc_version:
        report.checks.extend(check_lbc_version(lbc_version, needs_l7, needs_l4))
    else:
        report.checks.append(CheckResult(
            name="lbc-version",
            status="warning",
            message="LBC version not provided — skipping version checks",
            detail="Pass lbc_version parameter to enable version validation",
        ))

    report.checks.extend(check_gateway_api_channels(
        standard_channel_installed, experimental_channel_installed, needs_l4
    ))
    report.checks.extend(check_aws_crds(aws_crds_installed))
    report.checks.extend(check_feature_gates(alb_gateway_enabled, nlb_gateway_enabled, needs_l4))
    report.checks.extend(check_acm_certs(tls_hostnames or []))

    return report
