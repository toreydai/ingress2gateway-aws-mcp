"""
Offline YAML schema validation for Gateway API resources + AWS LBC constraints.
Does not require cluster access.
"""
from __future__ import annotations
import yaml
from dataclasses import dataclass, field


@dataclass
class ValidationIssue:
    severity: str   # "error" | "warning"
    resource: str
    message: str


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def errors(self) -> list[str]:
        return [i.message for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[str]:
        return [i.message for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Required fields per kind ─────────────────────────────────────────────────

REQUIRED_FIELDS: dict[str, list[list[str]]] = {
    "GatewayClass": [
        ["spec", "controllerName"],
    ],
    "Gateway": [
        ["spec", "gatewayClassName"],
        ["spec", "listeners"],
    ],
    "HTTPRoute": [
        ["spec", "parentRefs"],
        ["spec", "rules"],
    ],
    "GRPCRoute": [
        ["spec", "parentRefs"],
        ["spec", "rules"],
    ],
    "TCPRoute": [
        ["spec", "parentRefs"],
        ["spec", "rules"],
    ],
    "UDPRoute": [
        ["spec", "parentRefs"],
        ["spec", "rules"],
    ],
    "TLSRoute": [
        ["spec", "parentRefs"],
        ["spec", "rules"],
    ],
    "ReferenceGrant": [
        ["spec", "from"],
        ["spec", "to"],
    ],
    "BackendTLSPolicy": [
        ["spec", "targetRefs"],
        ["spec", "validation"],
    ],
    "LoadBalancerConfiguration": [
        ["spec"],
    ],
    "TargetGroupConfiguration": [
        ["spec"],
    ],
    "ListenerRuleConfiguration": [
        ["spec"],
    ],
}

KNOWN_KINDS = set(REQUIRED_FIELDS.keys())

# Valid controller names for AWS LBC
VALID_ALB_CONTROLLER = "gateway.k8s.aws/alb"
VALID_NLB_CONTROLLER = "gateway.k8s.aws/nlb"

L7_PROTOCOLS = {"HTTP", "HTTPS"}
L4_PROTOCOLS = {"TCP", "UDP", "TLS"}
L7_ROUTE_KINDS = {"HTTPRoute", "GRPCRoute"}
L4_ROUTE_KINDS = {"TCPRoute", "UDPRoute", "TLSRoute"}

EXPERIMENTAL_KINDS = {"TCPRoute", "UDPRoute", "TLSRoute", "BackendTLSPolicy"}


def _get_nested(d: dict, path: list[str]):
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _resource_label(res: dict) -> str:
    meta = res.get("metadata", {}) or {}
    ns = meta.get("namespace", "")
    name = meta.get("name", "?")
    kind = res.get("kind", "?")
    return f"{kind}/{ns}/{name}" if ns else f"{kind}/{name}"


def _listener_allows_namespace(listener: dict, route_ns: str, gateway_ns: str) -> bool:
    if route_ns == gateway_ns:
        return True
    allowed = listener.get("allowedRoutes", {}) or {}
    namespaces = allowed.get("namespaces", {}) or {}
    return namespaces.get("from") == "All"


def _route_layer(kind: str) -> str:
    return "l7" if kind in L7_ROUTE_KINDS else "l4"


def _listener_layer(protocol: str) -> str:
    return "l7" if protocol in L7_PROTOCOLS else "l4"


def _validate_resource(res: dict, report: ValidationReport) -> None:
    kind = res.get("kind", "")
    label = _resource_label(res)

    if not kind:
        report.issues.append(ValidationIssue("error", label, "Missing 'kind' field"))
        return

    if kind not in KNOWN_KINDS:
        report.issues.append(ValidationIssue("warning", label, f"Unknown kind '{kind}' is not validated"))

    if not res.get("apiVersion"):
        report.issues.append(ValidationIssue("error", label, "Missing 'apiVersion' field"))

    if not res.get("metadata", {}).get("name"):
        report.issues.append(ValidationIssue("error", label, "Missing metadata.name"))

    # Required fields check
    for path in REQUIRED_FIELDS.get(kind, []):
        if _get_nested(res, path) is None:
            report.issues.append(ValidationIssue(
                "error", label, f"Missing required field: {'.'.join(path)}"
            ))

    # AWS-specific checks
    if kind == "GatewayClass":
        controller = _get_nested(res, ["spec", "controllerName"])
        if controller and controller not in (VALID_ALB_CONTROLLER, VALID_NLB_CONTROLLER):
            report.issues.append(ValidationIssue(
                "error", label,
                f"controllerName '{controller}' is not a valid AWS LBC controller. "
                f"Must be '{VALID_ALB_CONTROLLER}' (ALB) or '{VALID_NLB_CONTROLLER}' (NLB). "
                "AWS LBC will ignore this GatewayClass, leaving all attached routes unprovisioned."
            ))

    if kind == "Gateway":
        listeners = _get_nested(res, ["spec", "listeners"]) or []
        protocols = {l.get("protocol", "HTTP") for l in listeners}
        has_l7 = bool(protocols & L7_PROTOCOLS)
        has_l4 = bool(protocols & L4_PROTOCOLS)

        if has_l7 and has_l4:
            report.issues.append(ValidationIssue(
                "error", label,
                "Gateway mixes L4 (TCP/UDP/TLS) and L7 (HTTP/HTTPS) listeners. "
                "AWS LBC does not support this — split into separate ALB and NLB Gateways."
            ))

        for listener in listeners:
            if listener.get("protocol") == "HTTPS":
                tls_block = listener.get("tls", {})
                if tls_block.get("certificateRefs"):
                    report.issues.append(ValidationIssue(
                        "error", label,
                        f"Listener '{listener.get('name')}': certificateRefs is not supported by AWS LBC. "
                        "Use listener hostname for ACM certificate discovery instead."
                    ))
                if not listener.get("hostname"):
                    report.issues.append(ValidationIssue(
                        "warning", label,
                        f"HTTPS listener '{listener.get('name')}' has no hostname. "
                        "ACM certificate discovery requires a hostname on HTTPS listeners."
                    ))
                if not tls_block:
                    report.issues.append(ValidationIssue(
                        "error", label,
                        f"HTTPS listener '{listener.get('name')}' missing tls block"
                    ))

        infra = _get_nested(res, ["spec", "infrastructure"])
        if infra:
            params = infra.get("parametersRef", {})
            if params:
                if params.get("group") != "gateway.k8s.aws":
                    report.issues.append(ValidationIssue(
                        "warning", label,
                        f"infrastructure.parametersRef.group should be 'gateway.k8s.aws', got '{params.get('group')}'"
                    ))

    if kind in L7_ROUTE_KINDS | L4_ROUTE_KINDS:
        parent_refs = _get_nested(res, ["spec", "parentRefs"]) or []
        if not parent_refs:
            report.issues.append(ValidationIssue(
                "error", label, "Route has no parentRefs — will not be attached to any Gateway"
            ))

    if kind in EXPERIMENTAL_KINDS:
        report.issues.append(ValidationIssue(
            "warning", label,
            f"{kind} is in the Gateway API experimental channel. "
            "Ensure experimental CRDs are installed on the cluster."
        ))

    if kind == "BackendTLSPolicy":
        for target in _get_nested(res, ["spec", "targetRefs"]) or []:
            if "port" in target:
                report.issues.append(ValidationIssue(
                    "error", label,
                    "BackendTLSPolicy targetRefs do not support 'port'; use sectionName for a named Service port or target the Service"
                ))
        validation = _get_nested(res, ["spec", "validation"]) or {}
        if not validation.get("hostname"):
            report.issues.append(ValidationIssue(
                "error", label, "BackendTLSPolicy validation.hostname is required"
            ))
        if not validation.get("caCertificateRefs") and not validation.get("wellKnownCACertificates"):
            report.issues.append(ValidationIssue(
                "error", label,
                "BackendTLSPolicy validation requires caCertificateRefs or wellKnownCACertificates"
            ))

    if kind == "ReferenceGrant":
        from_list = _get_nested(res, ["spec", "from"]) or []
        to_list = _get_nested(res, ["spec", "to"]) or []
        for item in from_list:
            if not item.get("namespace"):
                report.issues.append(ValidationIssue(
                    "error", label, "ReferenceGrant spec.from entry missing namespace"
                ))
        if not to_list:
            report.issues.append(ValidationIssue(
                "error", label, "ReferenceGrant spec.to is empty"
            ))

    if kind == "ListenerRuleConfiguration":
        actions = _get_nested(res, ["spec", "actions"]) or []
        conditions = _get_nested(res, ["spec", "conditions"]) or []
        if "actions" in (res.get("spec") or {}) and not actions:
            report.issues.append(ValidationIssue("error", label, "ListenerRuleConfiguration actions cannot be empty"))
        if "conditions" in (res.get("spec") or {}) and not conditions:
            report.issues.append(ValidationIssue("error", label, "ListenerRuleConfiguration conditions cannot be empty"))
        for action in actions:
            action_type = action.get("type")
            if action_type == "fixed-response":
                status_code = _get_nested(action, ["fixedResponseConfig", "statusCode"])
                if not isinstance(status_code, int):
                    report.issues.append(ValidationIssue(
                        "error", label,
                        "fixedResponseConfig.statusCode must be an integer for AWS LBC ListenerRuleConfiguration"
                    ))
            if action_type == "authenticate-oidc":
                cfg = action.get("authenticateOIDCConfig", {}) or {}
                for required in ("issuer", "secret", "tokenEndpoint", "userInfoEndpoint", "authorizationEndpoint"):
                    if not cfg.get(required):
                        report.issues.append(ValidationIssue(
                            "error", label,
                            f"authenticateOIDCConfig missing required field: {required}"
                        ))
        for condition in conditions:
            if condition.get("field") == "source-ip":
                values = _get_nested(condition, ["sourceIPConfig", "values"])
                if not values:
                    report.issues.append(ValidationIssue(
                        "error", label, "source-ip condition requires sourceIPConfig.values"
                    ))


def validate(yaml_content: str) -> ValidationReport:
    """Parse YAML and validate all resources."""
    report = ValidationReport()

    try:
        docs = list(yaml.safe_load_all(yaml_content))
    except yaml.YAMLError as e:
        report.issues.append(ValidationIssue("error", "YAML", f"Invalid YAML: {e}"))
        return report

    docs = [d for d in docs if d]

    if not docs:
        report.issues.append(ValidationIssue("warning", "YAML", "No resources found in input"))
        return report

    for doc in docs:
        _validate_resource(doc, report)

    # Cross-resource checks
    gateway_names: set[str] = set()
    gateway_classes: set[str] = set()
    gateways: dict[str, dict] = {}
    reference_grants: set[tuple[str, str, str]] = set()
    listener_rule_configs: set[str] = set()

    for doc in docs:
        kind = doc.get("kind", "")
        ns = doc.get("metadata", {}).get("namespace", "default")
        name = doc.get("metadata", {}).get("name", "")
        if kind == "Gateway":
            gateway_names.add(f"{ns}/{name}")
            gateways[f"{ns}/{name}"] = doc
        if kind == "GatewayClass":
            gateway_classes.add(name)
        if kind == "ReferenceGrant":
            grant_ns = ns
            for from_item in _get_nested(doc, ["spec", "from"]) or []:
                from_ns = from_item.get("namespace")
                from_kind = from_item.get("kind")
                for to_item in _get_nested(doc, ["spec", "to"]) or []:
                    if to_item.get("kind") == "Service":
                        reference_grants.add((from_ns, grant_ns, from_kind))
        if kind == "ListenerRuleConfiguration":
            listener_rule_configs.add(f"{ns}/{name}")

    for doc in docs:
        if doc.get("kind") != "Gateway":
            continue
        label = _resource_label(doc)
        class_name = _get_nested(doc, ["spec", "gatewayClassName"])
        if class_name and gateway_classes and class_name not in gateway_classes:
            report.issues.append(ValidationIssue(
                "warning", label,
                f"gatewayClassName '{class_name}' not found among GatewayClass resources in this YAML"
            ))

    for doc in docs:
        kind = doc.get("kind", "")
        if kind not in L7_ROUTE_KINDS | L4_ROUTE_KINDS:
            continue
        ns = doc.get("metadata", {}).get("namespace", "default")
        label = _resource_label(doc)
        for pref in _get_nested(doc, ["spec", "parentRefs"]) or []:
            pref_name = pref.get("name", "")
            pref_ns = pref.get("namespace", ns)
            gw_key = f"{pref_ns}/{pref_name}"
            if gw_key not in gateway_names:
                report.issues.append(ValidationIssue(
                    "warning", label,
                    f"parentRef '{gw_key}' not found among Gateways in this YAML. "
                    "Ensure the Gateway exists in the cluster."
                ))
                continue
            gateway = gateways[gw_key]
            listeners = _get_nested(gateway, ["spec", "listeners"]) or []
            section = pref.get("sectionName")
            matched_listeners = [l for l in listeners if not section or l.get("name") == section]
            if section and not matched_listeners:
                report.issues.append(ValidationIssue(
                    "error", label,
                    f"parentRef sectionName '{section}' not found on Gateway {gw_key}"
                ))
                continue
            for listener in matched_listeners:
                protocol = listener.get("protocol", "HTTP")
                if _route_layer(kind) != _listener_layer(protocol):
                    report.issues.append(ValidationIssue(
                        "error", label,
                        f"{kind} cannot attach to Gateway listener '{listener.get('name')}' with protocol {protocol}"
                    ))
                if not _listener_allows_namespace(listener, ns, pref_ns):
                    report.issues.append(ValidationIssue(
                        "error", label,
                        f"Gateway listener '{listener.get('name')}' does not allow routes from namespace '{ns}'"
                    ))

        for rule in _get_nested(doc, ["spec", "rules"]) or []:
            for backend in rule.get("backendRefs", []) or []:
                backend_ns = backend.get("namespace", ns)
                if backend_ns != ns and (ns, backend_ns, kind) not in reference_grants:
                    report.issues.append(ValidationIssue(
                        "warning", label,
                        f"backendRef '{backend_ns}/{backend.get('name', '')}' crosses namespaces without a matching ReferenceGrant"
                    ))
            for flt in rule.get("filters", []) or []:
                if flt.get("type") != "ExtensionRef":
                    continue
                ext = flt.get("extensionRef", {}) or {}
                if ext.get("group") == "gateway.k8s.aws" and ext.get("kind") == "ListenerRuleConfiguration":
                    ref_key = f"{ns}/{ext.get('name', '')}"
                    if ref_key not in listener_rule_configs:
                        report.issues.append(ValidationIssue(
                            "error", label,
                            f"ListenerRuleConfiguration ExtensionRef '{ref_key}' not found in this YAML"
                        ))

    return report
