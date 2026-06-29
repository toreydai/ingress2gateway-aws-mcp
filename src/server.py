"""
MCP Server: Nginx Ingress → AWS Gateway API 迁移工具
Run: mcp run src/server.py  或  python3 src/server.py
"""

from __future__ import annotations
import sys
import os
import json
import yaml

# Ensure src/ is importable when run as `python3 src/server.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import converter
from aws import annotation_map, gateway, crd
import preflight
import reporter
import validator

mcp = FastMCP(
    "ingress2gateway-aws-mcp",
    instructions=(
        "Nginx Ingress → AWS Gateway API (ALB/NLB) migration assistant. "
        "Converts Nginx Ingress YAML to AWS Load Balancer Controller Gateway API resources. "
        "Provides analysis, conversion, migration reports, and validation."
    ),
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_yaml_or_path(content: str) -> str:
    """If content looks like a file path, read it; otherwise return as-is."""
    stripped = content.strip()
    if stripped and "\n" not in stripped and os.path.isfile(stripped):
        with open(stripped) as f:
            return f.read()
    return content


def _parse_ingress_docs(yaml_str: str) -> list[dict]:
    docs = list(yaml.safe_load_all(yaml_str))
    return [d for d in docs if d and d.get("kind") == "Ingress"]


def _collect_all_annotations(ingress_docs: list[dict]) -> dict:
    merged: dict = {}
    for ing in ingress_docs:
        ann = ing.get("metadata", {}).get("annotations", {}) or {}
        merged.update(ann)
    return merged


def _dump_yaml(resources: list[dict]) -> str:
    parts = []
    for r in resources:
        parts.append(yaml.dump(r, default_flow_style=False, allow_unicode=True))
    return "---\n" + "\n---\n".join(parts)


def _run_conversion_pipeline(
    ingress_yaml: str,
    tcp_services_yaml: str,
    udp_services_yaml: str,
    options: dict,
) -> dict:
    """
    Full conversion pipeline:
    1. i2gw_runner (or fallback converter)
    2. AWS post-processing: GatewayClass injection, TLS fixup, L4/L7 split, Gateway merge
    3. AWS CRD generation: LoadBalancerConfiguration, TargetGroupConfiguration, ListenerRuleConfiguration
    """
    scheme = options.get("scheme", "internet-facing")
    alb_class_name = options.get("alb_gateway_class", "aws-alb")
    nlb_class_name = options.get("nlb_gateway_class", "aws-nlb")
    grouping = options.get("gateway_grouping", "by-class-scheme")

    all_errors: list[str] = []
    all_warnings: list[str] = []
    all_infos: list[str] = []

    # Step 1: base conversion
    conv = converter.run(ingress_yaml, tcp_services_yaml, udp_services_yaml)
    resources = conv.resources
    all_warnings.extend(conv.warnings)
    all_errors.extend(conv.errors)
    all_infos.extend(conv.infos)

    # Step 2a: inject GatewayClass
    resources, gc_resources = gateway.inject(resources, alb_class_name, nlb_class_name)

    # Step 2b: fix TLS
    resources, tls_hostnames, tls_warnings = gateway.fix_tls(resources)
    all_warnings.extend(tls_warnings)
    acm_checklist = gateway.build_acm_checklist(tls_hostnames)

    # Step 2c: split L4/L7 + rewrite parentRefs
    resources, split_warnings = gateway.split(resources, alb_class_name, nlb_class_name)
    all_warnings.extend(split_warnings)

    # Step 2d: merge Gateways by strategy
    resources, rename_map, before_gw, after_gw = gateway.merge(
        resources, strategy=grouping, scheme=scheme
    )
    if before_gw != after_gw:
        all_infos.append(
            f"Gateway 合并：{before_gw} → {after_gw}（策略: {grouping}）"
        )
    dns_changes = gateway.compute_dns_changes(resources)

    # Count ALB / NLB
    after_albs = sum(
        1 for r in resources
        if r.get("kind") == "Gateway" and alb_class_name in r.get("spec", {}).get("gatewayClassName", "")
    )
    after_nlbs = sum(
        1 for r in resources
        if r.get("kind") == "Gateway" and nlb_class_name in r.get("spec", {}).get("gatewayClassName", "")
    )

    # Step 3a: LoadBalancerConfiguration
    ingress_docs = _parse_ingress_docs(ingress_yaml)
    all_annotations = _collect_all_annotations(ingress_docs)
    resources, lb_configs = crd.generate_for_resources(resources, scheme, all_annotations)

    # Step 3b: TargetGroupConfiguration
    resources, tg_configs, tg_infos = crd.generate_for_routes(
        resources,
        all_annotations,
        health_check_path=options.get("health_check_path", "/"),
        health_check_interval=int(options.get("health_check_interval", 30)),
        healthy_threshold=int(options.get("healthy_threshold", 2)),
        unhealthy_threshold=int(options.get("unhealthy_threshold", 3)),
    )
    all_infos.extend(tg_infos)

    # Step 3c: ListenerRuleConfiguration
    lr_configs, lr_infos, lr_warnings = crd.generate_for_ingresses(ingress_docs)
    resources.extend(lr_configs)
    all_infos.extend(lr_infos)
    all_warnings.extend(lr_warnings)
    resources, attach_infos = crd.attach_listener_rule_configs(resources, ingress_docs)
    all_infos.extend(attach_infos)

    return {
        "resources": resources,
        "combined_yaml": _dump_yaml(resources),
        "tls_hostnames": tls_hostnames,
        "acm_checklist": acm_checklist,
        "dns_changes": dns_changes,
        "cost_impact": {
            "before_ingress": len(ingress_docs),
            "after_albs": after_albs,
            "after_nlbs": after_nlbs,
        },
        "errors": all_errors,
        "warnings": all_warnings,
        "infos": all_infos,
    }


# ── Tool 1: check_prerequisites ──────────────────────────────────────────────

@mcp.tool()
def check_prerequisites(
    lbc_version: str = "",
    needs_l4: bool = False,
    tls_hostnames: str = "",
) -> str:
    """
    Check if the target environment meets AWS Gateway API migration prerequisites.

    Args:
        lbc_version: AWS Load Balancer Controller version (e.g. "2.14.1"). Leave empty to skip.
        needs_l4: Set true if migration includes TCP/UDP/TLS routes (requires experimental channel).
        tls_hostnames: Comma-separated TLS hostnames to check ACM certificate coverage.
    """
    host_list = [h.strip() for h in tls_hostnames.split(",") if h.strip()]

    report = preflight.run_all(
        lbc_version=lbc_version,
        needs_l7=True,
        needs_l4=needs_l4,
        tls_hostnames=host_list,
    )

    # Also check i2gw binary availability
    binary_ver = converter.binary_version()
    if binary_ver:
        report.checks.insert(0, preflight.CheckResult(
            name="ingress2gateway-binary",
            status="ok",
            message=f"ingress2gateway binary found: {binary_ver}",
        ))
    else:
        report.checks.insert(0, preflight.CheckResult(
            name="ingress2gateway-binary",
            status="warning",
            message="ingress2gateway binary not found — using built-in Python converter",
            detail="Install for broader coverage: go install github.com/kubernetes-sigs/ingress2gateway@v1.1.0",
        ))

    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


# ── Tool 2: analyze_ingress ───────────────────────────────────────────────────

@mcp.tool()
def analyze_ingress(
    ingress_yaml: str,
    tcp_services_yaml: str = "",
    udp_services_yaml: str = "",
) -> str:
    """
    Analyze Nginx Ingress configuration and return a structured summary.
    Reports annotation compatibility, route type distribution, TLS hosts, cross-namespace refs, and cost estimate.

    Args:
        ingress_yaml: Nginx Ingress YAML content (multi-document supported) or file path.
        tcp_services_yaml: Optional tcp-services ConfigMap YAML for L4 TCP routes.
        udp_services_yaml: Optional udp-services ConfigMap YAML for L4 UDP routes.
    """
    try:
        ingress_yaml = _load_yaml_or_path(ingress_yaml)
        ingress_docs = _parse_ingress_docs(ingress_yaml)
    except Exception as e:
        return json.dumps({"error": f"Failed to parse YAML: {e}"}, ensure_ascii=False)

    if not ingress_docs:
        return json.dumps({"error": "No Ingress resources found in input"}, ensure_ascii=False)

    # Aggregate annotations across all Ingresses
    all_annotations = _collect_all_annotations(ingress_docs)
    classified = annotation_map.classify_annotations(all_annotations)

    # Count route types
    http_count = grpc_count = tls_passthrough = 0
    tls_hosts: list[str] = []
    cross_ns_refs: list[str] = []
    hosts: list[str] = []

    for ing in ingress_docs:
        ann = ing.get("metadata", {}).get("annotations", {}) or {}
        spec = ing.get("spec", {}) or {}
        ns = ing.get("metadata", {}).get("namespace", "default")

        for tls_entry in (spec.get("tls") or []):
            for h in (tls_entry.get("hosts") or []):
                if h not in tls_hosts:
                    tls_hosts.append(h)

        for rule in (spec.get("rules") or []):
            host = rule.get("host", "*")
            if host not in hosts:
                hosts.append(host)
            for path in (rule.get("http", {}).get("paths") or []):
                if annotation_map.is_grpc(ann):
                    grpc_count += 1
                elif annotation_map.is_ssl_passthrough(ann):
                    tls_passthrough += 1
                else:
                    http_count += 1
                svc = path.get("backend", {}).get("service", {})
                svc_ns = svc.get("namespace", "")
                if svc_ns and svc_ns != ns:
                    ref = f"{ns} → {svc_ns}/{svc.get('name', '?')}"
                    if ref not in cross_ns_refs:
                        cross_ns_refs.append(ref)

    # TCP/UDP from ConfigMaps
    tcp_count = 0
    udp_count = 0
    if tcp_services_yaml and tcp_services_yaml.strip():
        try:
            docs = list(yaml.safe_load_all(_load_yaml_or_path(tcp_services_yaml)))
            for d in docs:
                if d and d.get("kind") == "ConfigMap":
                    tcp_count = len(d.get("data", {}) or {})
        except Exception:
            pass

    if udp_services_yaml and udp_services_yaml.strip():
        try:
            docs = list(yaml.safe_load_all(_load_yaml_or_path(udp_services_yaml)))
            for d in docs:
                if d and d.get("kind") == "ConfigMap":
                    udp_count = len(d.get("data", {}) or {})
        except Exception:
            pass

    # Cost estimate (rough: 1 shared ALB, + NLB if L4)
    projected_albs = 1 if (http_count + grpc_count) > 0 else 0
    projected_nlbs = 1 if (tcp_count + udp_count + tls_passthrough) > 0 else 0

    result = {
        "ingress_count": len(ingress_docs),
        "hosts": hosts,
        "tls_hosts": tls_hosts,
        "route_types": {
            "http": http_count,
            "grpc": grpc_count,
            "tcp": tcp_count,
            "udp": udp_count,
            "tls_passthrough": tls_passthrough,
        },
        "annotations": classified,
        "cross_namespace_refs": cross_ns_refs,
        "cost_estimate": {
            "ingress_count": len(ingress_docs),
            "projected_albs": projected_albs,
            "projected_nlbs": projected_nlbs,
        },
        "summary": (
            f"{len(ingress_docs)} 个 Ingress / "
            f"{http_count + grpc_count + tcp_count + udp_count + tls_passthrough} 条路由 "
            f"({http_count} HTTP, {grpc_count} gRPC, {tcp_count} TCP, {udp_count} UDP, {tls_passthrough} TLS passthrough) → "
            f"预计 {projected_albs} ALB + {projected_nlbs} NLB。"
            f"{len(classified['warning'])} 个注解需人工处理，{len(classified['error'])} 个不兼容。"
        ),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


# ── Tool 3: convert_to_gateway_api ───────────────────────────────────────────

@mcp.tool()
def convert_to_gateway_api(
    ingress_yaml: str,
    tcp_services_yaml: str = "",
    udp_services_yaml: str = "",
    scheme: str = "internet-facing",
    namespace: str = "default",
    alb_gateway_class: str = "aws-alb",
    nlb_gateway_class: str = "aws-nlb",
    gateway_grouping: str = "by-class-scheme",
    health_check_path: str = "/",
    health_check_interval: int = 30,
    healthy_threshold: int = 2,
    unhealthy_threshold: int = 3,
) -> str:
    """
    Convert Nginx Ingress YAML to AWS Gateway API resources (full pipeline).

    Steps: base conversion → GatewayClass injection → TLS fixup → L4/L7 split
    → Gateway merge → LoadBalancerConfiguration → TargetGroupConfiguration → ListenerRuleConfiguration

    Args:
        ingress_yaml: Nginx Ingress YAML content or file path.
        tcp_services_yaml: Optional tcp-services ConfigMap YAML for TCP routes.
        udp_services_yaml: Optional udp-services ConfigMap YAML for UDP routes.
        scheme: ALB scheme — "internet-facing" or "internal".
        namespace: Default namespace if not specified in resources.
        alb_gateway_class: Name for the ALB GatewayClass (default: aws-alb).
        nlb_gateway_class: Name for the NLB GatewayClass (default: aws-nlb).
        gateway_grouping: Gateway consolidation strategy:
            "by-class-scheme" (default) | "by-namespace" | "by-host" | "single"
        health_check_path: TargetGroupConfiguration health check path.
        health_check_interval: TargetGroupConfiguration health check interval seconds.
        healthy_threshold: TargetGroupConfiguration healthy threshold count.
        unhealthy_threshold: TargetGroupConfiguration unhealthy threshold count.
    """
    try:
        ingress_yaml = _load_yaml_or_path(ingress_yaml)
        if tcp_services_yaml:
            tcp_services_yaml = _load_yaml_or_path(tcp_services_yaml)
        if udp_services_yaml:
            udp_services_yaml = _load_yaml_or_path(udp_services_yaml)
    except Exception as e:
        return json.dumps({"error": f"Failed to read input: {e}"}, ensure_ascii=False)

    options = {
        "scheme": scheme,
        "namespace": namespace,
        "alb_gateway_class": alb_gateway_class,
        "nlb_gateway_class": nlb_gateway_class,
        "gateway_grouping": gateway_grouping,
        "health_check_path": health_check_path,
        "health_check_interval": health_check_interval,
        "healthy_threshold": healthy_threshold,
        "unhealthy_threshold": unhealthy_threshold,
    }

    try:
        result = _run_conversion_pipeline(
            ingress_yaml, tcp_services_yaml or "", udp_services_yaml or "", options
        )
    except Exception as e:
        return json.dumps({"error": f"Conversion failed: {e}"}, ensure_ascii=False)

    # Return structured output with embedded YAML
    output = {
        "combined_yaml": result["combined_yaml"],
        "resource_count": len(result["resources"]),
        "cost_impact": result["cost_impact"],
        "dns_changes": result["dns_changes"],
        "acm_checklist": result["acm_checklist"],
        "tls_hostnames": result["tls_hostnames"],
        "errors": result["errors"],
        "warnings": result["warnings"],
        "infos": result["infos"],
    }
    return json.dumps(output, ensure_ascii=False, indent=2)


# ── Tool 4: generate_migration_report ────────────────────────────────────────

@mcp.tool()
def generate_migration_report(
    ingress_yaml: str,
    tcp_services_yaml: str = "",
    udp_services_yaml: str = "",
    scheme: str = "internet-facing",
    gateway_grouping: str = "by-class-scheme",
    lbc_version: str = "",
    health_check_path: str = "/",
    health_check_interval: int = 30,
    healthy_threshold: int = 2,
    unhealthy_threshold: int = 3,
) -> str:
    """
    Generate a complete Markdown migration report.
    Includes: summary, cost impact, DNS changes, annotation analysis,
    health check reminders, ACM checklist, migration checklist, and rollback plan.

    Args:
        ingress_yaml: Nginx Ingress YAML content or file path.
        tcp_services_yaml: Optional tcp-services ConfigMap YAML.
        udp_services_yaml: Optional udp-services ConfigMap YAML.
        scheme: ALB scheme — "internet-facing" or "internal".
        gateway_grouping: Gateway consolidation strategy.
        lbc_version: AWS LBC version for pre-flight checks (optional).
        health_check_path: TargetGroupConfiguration health check path.
        health_check_interval: TargetGroupConfiguration health check interval seconds.
        healthy_threshold: TargetGroupConfiguration healthy threshold count.
        unhealthy_threshold: TargetGroupConfiguration unhealthy threshold count.
    """
    try:
        ingress_yaml = _load_yaml_or_path(ingress_yaml)
        if tcp_services_yaml:
            tcp_services_yaml = _load_yaml_or_path(tcp_services_yaml)
        if udp_services_yaml:
            udp_services_yaml = _load_yaml_or_path(udp_services_yaml)
    except Exception as e:
        return f"# Error\n\nFailed to read input: {e}"

    # Analysis
    analysis_json = analyze_ingress(ingress_yaml, tcp_services_yaml or "", udp_services_yaml or "")
    analysis = json.loads(analysis_json)

    if "error" in analysis:
        return f"# Error\n\n{analysis['error']}"

    # Conversion
    options = {
        "scheme": scheme,
        "gateway_grouping": gateway_grouping,
        "alb_gateway_class": "aws-alb",
        "nlb_gateway_class": "aws-nlb",
        "health_check_path": health_check_path,
        "health_check_interval": health_check_interval,
        "healthy_threshold": healthy_threshold,
        "unhealthy_threshold": unhealthy_threshold,
    }
    try:
        conversion = _run_conversion_pipeline(
            ingress_yaml, tcp_services_yaml or "", udp_services_yaml or "", options
        )
    except Exception as e:
        return f"# Error\n\nConversion pipeline failed: {e}"

    # Pre-flight
    needs_l4 = any(
        analysis["route_types"].get(k, 0) > 0
        for k in ("tcp", "udp", "tls_passthrough")
    )
    preflight_report = preflight.run_all(
        lbc_version=lbc_version,
        needs_l7=True,
        needs_l4=needs_l4,
        tls_hostnames=analysis.get("tls_hosts", []),
    )

    return reporter.generate(
        analysis=analysis,
        conversion=conversion,
        preflight=preflight_report.to_dict(),
        options=options,
    )


# ── Tool 5: validate_output ───────────────────────────────────────────────────

@mcp.tool()
def validate_output(yaml_content: str) -> str:
    """
    Validate Gateway API YAML against schema rules and AWS LBC constraints.
    Checks: required fields, L4/L7 mixing, certificateRefs misuse, parentRef consistency,
    experimental channel requirements, and cross-namespace reference coverage.

    Args:
        yaml_content: Gateway API YAML content to validate, or file path.
    """
    try:
        yaml_content = _load_yaml_or_path(yaml_content)
    except Exception as e:
        return json.dumps({"valid": False, "errors": [f"Failed to read input: {e}"], "warnings": []})

    report = validator.validate(yaml_content)
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
