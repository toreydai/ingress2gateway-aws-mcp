"""
AWS Gateway API CRD generators: LoadBalancerConfiguration, TargetGroupConfiguration,
ListenerRuleConfiguration. All use apiVersion gateway.k8s.aws/v1beta1.
"""
from __future__ import annotations
import re

AWS_GATEWAY_API = "gateway.k8s.aws/v1beta1"
NGINX_PREFIX = "nginx.ingress.kubernetes.io/"


def _safe_name(s: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", s.lower()).strip("-")[:63]


def _listener_rule_filter(name: str) -> dict:
    return {
        "type": "ExtensionRef",
        "extensionRef": {
            "group": "gateway.k8s.aws",
            "kind": "ListenerRuleConfiguration",
            "name": name,
        },
    }


def _ingress_hosts(ing: dict) -> set[str]:
    hosts: set[str] = set()
    for rule in (ing.get("spec", {}) or {}).get("rules", []) or []:
        host = rule.get("host")
        if host:
            hosts.add(host)
    return hosts


def _route_matches_ingress(route: dict, ing: dict) -> bool:
    ns = (ing.get("metadata", {}) or {}).get("namespace", "default")
    if route.get("metadata", {}).get("namespace", "default") != ns:
        return False
    hosts = _ingress_hosts(ing)
    if not hosts:
        return True
    route_hosts = set(route.get("spec", {}).get("hostnames", []) or [])
    return bool(hosts & route_hosts)


def _append_filter(rule: dict, flt: dict) -> bool:
    filters = rule.setdefault("filters", [])
    ref = flt.get("extensionRef", {})
    for existing in filters:
        existing_ref = existing.get("extensionRef", {})
        if (
            existing.get("type") == flt.get("type")
            and existing_ref.get("group") == ref.get("group")
            and existing_ref.get("kind") == ref.get("kind")
            and existing_ref.get("name") == ref.get("name")
        ):
            return False
    filters.append(flt)
    return True


# ── LoadBalancerConfiguration ─────────────────────────────────────────────────

def make_lb_config(
    name: str,
    namespace: str,
    scheme: str = "internet-facing",
    extra_attributes: dict | None = None,
    subnets: list[str] | None = None,
    security_groups: list[str] | None = None,
    tags: dict | None = None,
) -> dict:
    spec: dict = {"scheme": scheme}
    if subnets:
        spec["subnets"] = {"ids": subnets}
    if security_groups:
        spec["securityGroups"] = security_groups
    lb_attrs = {}
    if extra_attributes:
        lb_attrs.update(extra_attributes)
    if lb_attrs:
        spec["loadBalancerAttributes"] = [
            {"key": k, "value": str(v)} for k, v in lb_attrs.items()
        ]
    if tags:
        spec["tags"] = [{"key": k, "value": str(v)} for k, v in tags.items()]
    return {
        "apiVersion": AWS_GATEWAY_API,
        "kind": "LoadBalancerConfiguration",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def _extract_proxy_timeout(annotations: dict) -> dict:
    attrs = {}
    read_timeout = annotations.get(NGINX_PREFIX + "proxy-read-timeout")
    send_timeout = annotations.get(NGINX_PREFIX + "proxy-send-timeout")
    timeout = read_timeout or send_timeout
    if timeout:
        try:
            attrs["idle_timeout.timeout_seconds"] = str(int(timeout))
        except ValueError:
            pass
    return attrs


def _extract_body_size(annotations: dict) -> dict:
    # ALB has no per-request body-size attribute; parse only to acknowledge the annotation
    body_size = annotations.get(NGINX_PREFIX + "proxy-body-size")
    if body_size and body_size not in ("0", ""):
        raw = body_size.lower().strip()
        try:
            multiplier = {"m": 1024 * 1024, "k": 1024, "g": 1024 ** 3}
            suffix = raw[-1]
            int(raw[:-1]) * multiplier.get(suffix, 1)
        except (ValueError, IndexError):
            pass
    return {}


def generate_for_resources(
    resources: list[dict],
    scheme: str = "internet-facing",
    ingress_annotations: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    For each Gateway resource, generate a LoadBalancerConfiguration and wire up parametersRef.
    Returns (updated_resources, lb_config_resources).
    """
    annotations = ingress_annotations or {}
    lb_attrs = {**_extract_proxy_timeout(annotations), **_extract_body_size(annotations)}

    lb_configs: list[dict] = []
    updated: list[dict] = []

    for res in resources:
        if res.get("kind") != "Gateway":
            updated.append(res)
            continue
        gw_name = res["metadata"]["name"]
        ns = res["metadata"].get("namespace", "default")
        config_name = _safe_name(f"{gw_name}-lb-config")
        lb_cfg = make_lb_config(
            name=config_name,
            namespace=ns,
            scheme=scheme,
            extra_attributes=lb_attrs or None,
        )
        lb_configs.append(lb_cfg)
        res.setdefault("spec", {}).setdefault("infrastructure", {})
        res["spec"]["infrastructure"]["parametersRef"] = {
            "group": "gateway.k8s.aws",
            "kind": "LoadBalancerConfiguration",
            "name": config_name,
        }
        updated.append(res)

    return updated + lb_configs, lb_configs


# ── TargetGroupConfiguration ──────────────────────────────────────────────────

def _get_backend_protocol(annotations: dict) -> str:
    proto = annotations.get(NGINX_PREFIX + "backend-protocol", "HTTP").upper()
    return proto if proto in ("HTTP", "HTTPS", "GRPC", "GRPCS") else "HTTP"


def _health_check_protocol(backend_proto: str) -> str:
    if backend_proto in ("GRPC", "GRPCS"):
        return "HTTP"  # ALB uses HTTP/2 health checks for gRPC
    return "HTTPS" if backend_proto == "HTTPS" else "HTTP"


def make_tg_config(
    name: str,
    namespace: str,
    service_name: str = "",
    health_check_path: str = "/",
    health_check_protocol: str = "HTTP",
    health_check_interval: int = 30,
    healthy_threshold: int = 2,
    unhealthy_threshold: int = 3,
    target_type: str = "ip",
    stickiness: dict | None = None,
) -> dict:
    default_config: dict = {
        "targetType": target_type,
        "healthCheckConfig": {
            "healthCheckPath": health_check_path,
            "healthCheckProtocol": health_check_protocol,
            "healthCheckInterval": health_check_interval,
            "healthyThresholdCount": healthy_threshold,
            "unhealthyThresholdCount": unhealthy_threshold,
        },
    }
    if stickiness:
        default_config["targetGroupAttributes"] = [
            {"key": "stickiness.enabled", "value": "true"},
            {"key": "stickiness.type", "value": stickiness.get("type", "lb_cookie")},
        ]
        if duration := stickiness.get("duration_seconds"):
            default_config["targetGroupAttributes"].append(
                {"key": "stickiness.lb_cookie.duration_seconds", "value": str(duration)}
            )
    spec: dict = {"defaultConfiguration": default_config}
    if service_name:
        spec["targetReference"] = {"name": service_name}
    return {
        "apiVersion": AWS_GATEWAY_API,
        "kind": "TargetGroupConfiguration",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def _extract_stickiness(annotations: dict) -> dict | None:
    cookie_name = annotations.get(NGINX_PREFIX + "session-cookie-name")
    if not cookie_name:
        return None
    stickiness: dict = {"type": "lb_cookie", "cookie_name": cookie_name}
    duration = (
        annotations.get(NGINX_PREFIX + "session-cookie-max-age")
        or annotations.get(NGINX_PREFIX + "session-cookie-expires")
    )
    if duration:
        try:
            stickiness["duration_seconds"] = int(duration)
        except (TypeError, ValueError):
            pass
    return stickiness


def generate_for_routes(
    resources: list[dict],
    ingress_annotations: dict | None = None,
    health_check_path: str = "/",
    health_check_interval: int = 30,
    healthy_threshold: int = 2,
    unhealthy_threshold: int = 3,
) -> tuple[list[dict], list[dict], list[str]]:
    """
    Generate TargetGroupConfiguration for each unique Service backend.
    Returns (updated_resources, tg_config_resources, infos).
    """
    annotations = ingress_annotations or {}
    backend_proto = _get_backend_protocol(annotations)
    hc_protocol = _health_check_protocol(backend_proto)
    stickiness = _extract_stickiness(annotations)

    infos: list[str] = []
    tg_configs: list[dict] = []
    seen_services: set[str] = set()

    for res in resources:
        if res.get("kind") not in ("HTTPRoute", "GRPCRoute"):
            continue
        ns = res.get("metadata", {}).get("namespace", "default")
        for rule in res.get("spec", {}).get("rules", []):
            for backend in rule.get("backendRefs", []):
                svc_name = backend.get("name", "")
                svc_ns = backend.get("namespace", ns)
                if not svc_name:
                    continue
                svc_key = f"{svc_ns}/{svc_name}"
                if svc_key in seen_services:
                    continue
                seen_services.add(svc_key)
                config_name = _safe_name(f"{svc_name}-tg-config")
                tg = make_tg_config(
                    name=config_name,
                    namespace=svc_ns,
                    service_name=svc_name,
                    health_check_path=health_check_path,
                    health_check_protocol=hc_protocol,
                    health_check_interval=health_check_interval,
                    healthy_threshold=healthy_threshold,
                    unhealthy_threshold=unhealthy_threshold,
                    stickiness=stickiness,
                )
                tg_configs.append(tg)
                infos.append(
                    f"TargetGroupConfiguration for Service {svc_key}: "
                    f"health check path='{health_check_path}' protocol={hc_protocol}. "
                    "⚠️ Verify health check path matches your application's readiness endpoint."
                )

    return resources + tg_configs, tg_configs, infos


# ── ListenerRuleConfiguration ─────────────────────────────────────────────────

def make_oidc_rule_config(
    name: str,
    namespace: str,
    issuer: str = "REPLACE_ME_OIDC_ISSUER",
    client_id: str = "REPLACE_ME_CLIENT_ID",
    client_secret_arn: str = "REPLACE_ME_SECRET_ARN",
    token_endpoint: str = "",
    user_info_endpoint: str = "",
    authorization_endpoint: str = "",
    on_unauthenticated: str = "authenticate",
) -> dict:
    return {
        "apiVersion": AWS_GATEWAY_API,
        "kind": "ListenerRuleConfiguration",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "actions": [{
                "type": "authenticate-oidc",
                "authenticateOIDCConfig": {
                    "issuer": issuer,
                    "secret": {"name": "REPLACE_ME_OIDC_SECRET"},
                    "tokenEndpoint": token_endpoint or f"{issuer}/oauth2/token",
                    "userInfoEndpoint": user_info_endpoint or f"{issuer}/oauth2/userInfo",
                    "authorizationEndpoint": authorization_endpoint or issuer,
                    "onUnauthenticatedRequest": on_unauthenticated,
                },
            }],
        },
    }


def make_source_ip_rule_config(
    name: str,
    namespace: str,
    allow_cidrs: list[str] | None = None,
    deny_cidrs: list[str] | None = None,
) -> dict:
    conditions = []
    if allow_cidrs:
        conditions.append({"field": "source-ip", "sourceIPConfig": {"values": allow_cidrs}})
    if deny_cidrs:
        conditions.append({"field": "source-ip", "sourceIPConfig": {"values": deny_cidrs}})
    spec: dict = {"conditions": conditions}
    if deny_cidrs:
        spec["actions"] = [{
            "type": "fixed-response",
            "fixedResponseConfig": {
                "statusCode": 403,
                "contentType": "text/plain",
                "messageBody": "Forbidden",
            },
        }]
    return {
        "apiVersion": AWS_GATEWAY_API,
        "kind": "ListenerRuleConfiguration",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def make_fixed_response_config(
    name: str,
    namespace: str,
    status_code: int = 404,
    content_type: str = "text/plain",
    message_body: str = "Not Found",
) -> dict:
    return {
        "apiVersion": AWS_GATEWAY_API,
        "kind": "ListenerRuleConfiguration",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "actions": [{
                "type": "fixed-response",
                "fixedResponseConfig": {
                    "statusCode": int(status_code),
                    "contentType": content_type,
                    "messageBody": message_body,
                },
            }],
        },
    }


def generate_for_ingresses(
    ingress_docs: list[dict],
) -> tuple[list[dict], list[str], list[str]]:
    """
    Scan Ingress annotations and emit ListenerRuleConfiguration resources for:
    auth-url (OIDC), whitelist/denylist-source-range.
    Returns (lr_config_resources, infos, warnings).
    """
    lr_configs: list[dict] = []
    infos: list[str] = []
    warnings: list[str] = []

    for ing in (ingress_docs or []):
        if ing.get("kind") != "Ingress":
            continue
        meta = ing.get("metadata", {}) or {}
        name = meta.get("name", "ingress")
        ns = meta.get("namespace", "default")
        ann = meta.get("annotations", {}) or {}

        # OIDC auth-url
        auth_url = ann.get(NGINX_PREFIX + "auth-url")
        auth_signin = ann.get(NGINX_PREFIX + "auth-signin")
        if auth_url:
            lr = make_oidc_rule_config(
                name=_safe_name(f"{name}-oidc-auth"),
                namespace=ns,
                issuer=auth_url.rstrip("/"),
                authorization_endpoint=auth_signin or "",
            )
            lr_configs.append(lr)
            infos.append(
                f"Ingress {ns}/{name}: auth-url → ListenerRuleConfiguration OIDC. "
                "⚠️ Fill in: issuer, clientId, clientSecret ARN, token/userInfo endpoints."
            )

        # Whitelist → source IP allow
        allow_cidrs = [c.strip() for c in ann.get(NGINX_PREFIX + "whitelist-source-range", "").split(",") if c.strip()]
        if allow_cidrs:
            lr_configs.append(make_source_ip_rule_config(
                name=_safe_name(f"{name}-source-ip"), namespace=ns, allow_cidrs=allow_cidrs
            ))
            infos.append(
                f"Ingress {ns}/{name}: whitelist-source-range → ListenerRuleConfiguration "
                f"source IP conditions (allow {allow_cidrs}). Attach to the relevant HTTPRoute rule."
            )

        # Denylist → source IP deny
        deny_cidrs = [c.strip() for c in ann.get(NGINX_PREFIX + "denylist-source-range", "").split(",") if c.strip()]
        if deny_cidrs:
            lr_configs.append(make_source_ip_rule_config(
                name=_safe_name(f"{name}-source-ip-deny"), namespace=ns, deny_cidrs=deny_cidrs
            ))
            infos.append(
                f"Ingress {ns}/{name}: denylist-source-range → ListenerRuleConfiguration "
                f"source IP deny conditions ({deny_cidrs})."
            )

        # spec.defaultBackend is converted to a catch-all HTTPRoute by converter.py.
        if (ing.get("spec") or {}).get("defaultBackend"):
            infos.append(
                f"Ingress {ns}/{name}: defaultBackend is handled as a catch-all HTTPRoute backend."
            )

        # custom-http-errors
        error_codes_raw = ann.get(NGINX_PREFIX + "custom-http-errors", "")
        if error_codes_raw:
            warnings.append(
                f"Ingress {ns}/{name}: custom-http-errors ({error_codes_raw}) — "
                "ListenerRuleConfiguration fixed-response can only return a static body, "
                "not proxy to a custom error service. Implement with Lambda if needed."
            )

    return lr_configs, infos, warnings


def attach_listener_rule_configs(
    resources: list[dict],
    ingress_docs: list[dict],
) -> tuple[list[dict], list[str]]:
    """
    Attach generated ListenerRuleConfiguration resources to matching HTTPRoute
    backend rules via Gateway API ExtensionRef filters.
    """
    infos: list[str] = []
    updated: list[dict] = []

    attach_by_ingress: dict[tuple[str, str], list[str]] = {}
    for ing in ingress_docs or []:
        if ing.get("kind") != "Ingress":
            continue
        meta = ing.get("metadata", {}) or {}
        ns = meta.get("namespace", "default")
        name = meta.get("name", "ingress")
        ann = meta.get("annotations", {}) or {}
        refs: list[str] = []
        if ann.get(NGINX_PREFIX + "auth-url"):
            refs.append(_safe_name(f"{name}-oidc-auth"))
        if ann.get(NGINX_PREFIX + "whitelist-source-range"):
            refs.append(_safe_name(f"{name}-source-ip"))
        if ann.get(NGINX_PREFIX + "denylist-source-range"):
            refs.append(_safe_name(f"{name}-source-ip-deny"))
        if refs:
            attach_by_ingress[(ns, name)] = refs

    for res in resources:
        if res.get("kind") != "HTTPRoute":
            updated.append(res)
            continue
        route = res
        attached: list[str] = []
        for ing in ingress_docs or []:
            meta = ing.get("metadata", {}) or {}
            refs = attach_by_ingress.get((meta.get("namespace", "default"), meta.get("name", "ingress")), [])
            if not refs or not _route_matches_ingress(route, ing):
                continue
            for rule in route.get("spec", {}).get("rules", []) or []:
                if not rule.get("backendRefs"):
                    continue
                for ref_name in refs:
                    if _append_filter(rule, _listener_rule_filter(ref_name)):
                        attached.append(ref_name)
        if attached:
            uniq = ", ".join(sorted(set(attached)))
            label = f"{route.get('metadata', {}).get('namespace', 'default')}/{route.get('metadata', {}).get('name', '')}"
            infos.append(f"Attached ListenerRuleConfiguration ({uniq}) to HTTPRoute {label}.")
        updated.append(route)

    return updated, infos
