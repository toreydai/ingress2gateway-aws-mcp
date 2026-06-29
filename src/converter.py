"""
Nginx Ingress → Gateway API converter.
Tries the ingress2gateway binary first; falls back to the built-in Python converter.
"""
from __future__ import annotations
import os
import re
import copy
import shutil
import subprocess
import tempfile
import yaml
from urllib.parse import urlparse

from aws.annotation_map import (
    NGINX_PREFIX, get_backend_protocol, is_grpc, is_ssl_passthrough,
    is_canary, get_canary_weight, get_rewrite_target,
    get_ssl_redirect, get_cors_config, get_whitelist_cidrs,
)

GW_API = "gateway.networking.k8s.io/v1"
GW_API_BETA = "gateway.networking.k8s.io/v1beta1"
GW_API_ALPHA = "gateway.networking.k8s.io/v1alpha2"
GW_API_ALPHA3 = "gateway.networking.k8s.io/v1alpha3"

_BINARY_CANDIDATES = ["ingress2gateway", "i2gw"]


# ── ConversionResult ──────────────────────────────────────────────────────────

class ConversionResult:
    def __init__(self) -> None:
        self.resources: list[dict] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.infos: list[str] = []


# ── helpers ───────────────────────────────────────────────────────────────────

def _meta(name: str, namespace: str) -> dict:
    m: dict = {"name": name}
    if namespace:
        m["namespace"] = namespace
    return m


def _path_type_map(pt: str) -> str:
    if pt == "Exact":
        return "Exact"
    return "PathPrefix"


def _route_match(path: str, path_type: str, headers: list[dict] | None = None) -> dict:
    match = {"path": {"type": _path_type_map(path_type), "value": path}}
    if headers:
        match["headers"] = headers
    return match


def _clean_path(path: str) -> str:
    clean = re.sub(r"\(.*", "", path).rstrip("/") or "/"
    clean = re.sub(r"[^a-zA-Z0-9/_\-.]", "", clean)
    return clean or "/"


def _safe_name(*parts: str) -> str:
    raw = "-".join(p for p in parts if p)
    return re.sub(r"[^a-z0-9-]", "-", raw.lower()).strip("-")[:63]


def _is_https_backend(annotations: dict) -> bool:
    return get_backend_protocol(annotations) == "HTTPS"


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _request_redirect_filter(target: str | None, status_code: int) -> dict:
    redirect: dict = {"statusCode": status_code}
    if target:
        parsed = urlparse(target)
        if parsed.scheme:
            redirect["scheme"] = parsed.scheme
        if parsed.netloc:
            redirect["hostname"] = parsed.hostname or parsed.netloc
        path = parsed.path or (target if target.startswith("/") else "")
        if path:
            redirect["path"] = {"type": "ReplaceFullPath", "replaceFullPath": path}
    return {"type": "RequestRedirect", "requestRedirect": redirect}


def _header_modifier_filter(annotations: dict) -> dict | None:
    headers: list[dict] = []
    upstream_host = annotations.get(NGINX_PREFIX + "upstream-vhost")
    if upstream_host:
        headers.append({"name": "Host", "value": upstream_host})
    if not headers:
        return None
    return {"type": "RequestHeaderModifier", "requestHeaderModifier": {"set": headers}}


def _mirror_backend_ref(raw: str, default_port: int, namespace: str) -> dict:
    value = raw.strip()
    if "://" in value:
        parsed = urlparse(value)
        host = parsed.hostname or value
        port = parsed.port or default_port
    else:
        host, _, port_str = value.partition(":")
        port = _parse_int(port_str or None, default_port)
    parts = host.split(".")
    ref: dict = {"name": parts[0], "port": port}
    if len(parts) > 1 and parts[1] not in ("svc", "cluster", "local") and parts[1] != namespace:
        ref["namespace"] = parts[1]
    return ref


# ── rule builders ─────────────────────────────────────────────────────────────

def _build_http_rule(
    path: str, path_type: str, service_name: str, service_port: int,
    annotations: dict, namespace: str, weight: int = 100,
    extra_backends: list[dict] | None = None,
    service_namespace: str | None = None,
    headers: list[dict] | None = None,
    apply_rewrite: bool = True,
) -> dict:
    matches = [_route_match(path, path_type, headers)]
    filters: list[dict] = []

    rewrite = get_rewrite_target(annotations)
    if apply_rewrite and rewrite and "$" not in rewrite:
        filters.append({
            "type": "URLRewrite",
            "urlRewrite": {"path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": rewrite}},
        })

    # AWS LBC Gateway API does not support ResponseHeaderModifier; CORS must be WAF/application layer.

    header_modifier = _header_modifier_filter(annotations)
    if header_modifier:
        filters.append(header_modifier)

    permanent_redirect = annotations.get(NGINX_PREFIX + "permanent-redirect")
    temporal_redirect = annotations.get(NGINX_PREFIX + "temporal-redirect")
    if permanent_redirect:
        status = _parse_int(annotations.get(NGINX_PREFIX + "permanent-redirect-code"), 301)
        filters.append(_request_redirect_filter(permanent_redirect, status))
    elif temporal_redirect:
        filters.append(_request_redirect_filter(temporal_redirect, 302))

    mirror_svc = annotations.get(NGINX_PREFIX + "mirror-target") or annotations.get(NGINX_PREFIX + "mirror")
    if mirror_svc:
        filters.append({
            "type": "RequestMirror",
            "requestMirror": {"backendRef": _mirror_backend_ref(mirror_svc, service_port, namespace)},
        })

    primary_backend: dict = {"name": service_name, "port": service_port}
    if service_namespace and service_namespace != namespace:
        primary_backend["namespace"] = service_namespace
    if extra_backends:
        primary_backend["weight"] = weight
    backends = [primary_backend] + (extra_backends or [])

    rule: dict = {"matches": matches, "backendRefs": backends}
    if filters:
        rule["filters"] = filters
    return rule


def _build_redirect_rule(path: str, target: str, status_code: int = 302) -> dict:
    return {
        "matches": [_route_match(path, "Exact")],
        "filters": [_request_redirect_filter(target, status_code)],
    }


def _build_grpc_rule(path: str, service_name: str, service_port: int, service_namespace: str | None = None, route_namespace: str | None = None) -> dict:
    method: dict = {}
    if path and path != "/":
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            method = {"service": parts[0], "method": parts[1]}
        elif len(parts) == 1:
            method = {"service": parts[0]}
    matches = [{"method": method}] if method else [{}]
    backend = {"name": service_name, "port": service_port}
    if service_namespace and route_namespace and service_namespace != route_namespace:
        backend["namespace"] = service_namespace
    return {"matches": matches, "backendRefs": [backend]}


# ── resource builders ─────────────────────────────────────────────────────────

def _build_gateway(name: str, namespace: str, hosts: list[str], tls_hosts: list[str]) -> dict:
    listeners: list[dict] = []
    for h in tls_hosts:
        listeners.append({
            "name": _safe_name("https", h.replace("*", "wildcard")),
            "port": 443, "protocol": "HTTPS", "hostname": h,
            "tls": {"mode": "Terminate"},
        })
    listeners.append({"name": "http", "port": 80, "protocol": "HTTP"})
    return {
        "apiVersion": GW_API, "kind": "Gateway",
        "metadata": _meta(name, namespace),
        "spec": {"gatewayClassName": "nginx", "listeners": listeners},
    }


def _listener_name_for_host(host: str) -> str:
    return _safe_name("https", host.replace("*", "wildcard"))


def _parent_ref(gateway_name: str, namespace: str, section_name: str | None = None) -> dict:
    ref = {"name": gateway_name, "namespace": namespace}
    if section_name:
        ref["sectionName"] = section_name
    return ref


def _build_reference_grant(from_ns: str, to_ns: str, route_kind: str = "HTTPRoute") -> dict:
    return {
        "apiVersion": GW_API_BETA, "kind": "ReferenceGrant",
        "metadata": _meta(f"allow-from-{from_ns}", to_ns),
        "spec": {
            "from": [{"group": "gateway.networking.k8s.io", "kind": route_kind, "namespace": from_ns}],
            "to": [{"group": "", "kind": "Service"}],
        },
    }


def _build_tls_route(name: str, namespace: str, host: str, service_name: str, service_port: int, gateway_name: str) -> dict:
    return {
        "apiVersion": GW_API_ALPHA, "kind": "TLSRoute",
        "metadata": _meta(name, namespace),
        "spec": {
            "parentRefs": [{"name": gateway_name, "namespace": namespace}],
            "hostnames": [host],
            "rules": [{"backendRefs": [{"name": service_name, "port": service_port}]}],
        },
    }


def _build_nlb_gateway(name: str, namespace: str, ports: list[int]) -> dict:
    return {
        "apiVersion": GW_API, "kind": "Gateway",
        "metadata": _meta(name, namespace),
        "spec": {
            "gatewayClassName": "nlb",
            "listeners": [{"name": f"tcp-{p}", "port": p, "protocol": "TCP"} for p in ports],
        },
    }


def _build_tcp_route(name: str, namespace: str, port: int, service_name: str, service_port: int, gateway_name: str, gateway_namespace: str) -> dict:
    return {
        "apiVersion": GW_API_ALPHA, "kind": "TCPRoute",
        "metadata": _meta(name, namespace),
        "spec": {
            "parentRefs": [{"name": gateway_name, "namespace": gateway_namespace, "sectionName": f"tcp-{port}"}],
            "rules": [{"backendRefs": [{"name": service_name, "port": service_port}]}],
        },
    }


def _build_udp_route(name: str, namespace: str, port: int, service_name: str, service_port: int, gateway_name: str, gateway_namespace: str) -> dict:
    return {
        "apiVersion": GW_API_ALPHA, "kind": "UDPRoute",
        "metadata": _meta(name, namespace),
        "spec": {
            "parentRefs": [{"name": gateway_name, "namespace": gateway_namespace, "sectionName": f"udp-{port}"}],
            "rules": [{"backendRefs": [{"name": service_name, "port": service_port}]}],
        },
    }


def _build_backend_tls_policy(name: str, namespace: str, service_name: str, service_port: int) -> dict:
    """
    Generate a BackendTLSPolicy for an HTTPS backend.
    caCertificateRefs intentionally omitted (uses system CA pool); add manually for private CAs.
    """
    hostname = f"{service_name}.{namespace}.svc.cluster.local"
    return {
        "apiVersion": GW_API_ALPHA3, "kind": "BackendTLSPolicy",
        "metadata": _meta(name, namespace),
        "spec": {
            "targetRefs": [{"group": "", "kind": "Service", "name": service_name}],
            "validation": {"hostname": hostname, "wellKnownCACertificates": "System"},
        },
    }


def _backend_ref_from_service(service: dict, default_port: int = 80) -> dict:
    backend: dict = {
        "name": service.get("name", ""),
        "port": service.get("port", {}).get("number", default_port),
    }
    if service.get("namespace"):
        backend["namespace"] = service["namespace"]
    return backend


# ── Python fallback converter ─────────────────────────────────────────────────

def convert(
    ingress_docs: list[dict],
    tcp_services: dict | None = None,
    udp_services: dict | None = None,
) -> ConversionResult:
    """Convert parsed Nginx Ingress documents to Gateway API resources."""
    result = ConversionResult()

    primary: list[dict] = []
    canaries: list[dict] = []
    for doc in ingress_docs:
        if doc.get("kind") != "Ingress":
            continue
        ann = doc.get("metadata", {}).get("annotations", {}) or {}
        (canaries if is_canary(ann) else primary).append(doc)

    canary_map: dict[tuple[str, str], list[dict]] = {}
    for ing in canaries:
        ann = ing.get("metadata", {}).get("annotations", {}) or {}
        ns = ing.get("metadata", {}).get("namespace", "default")
        weight = get_canary_weight(ann)
        header_name = ann.get(NGINX_PREFIX + "canary-by-header")
        header_value = ann.get(NGINX_PREFIX + "canary-by-header-value", "always")
        for rule in (ing.get("spec", {}).get("rules") or []):
            host = rule.get("host", "*")
            for path_item in (rule.get("http", {}).get("paths") or []):
                svc = path_item.get("backend", {}).get("service", {})
                svc_name = svc.get("name", "")
                svc_port = svc.get("port", {}).get("number", 80)
                if svc_name:
                    path = _clean_path(path_item.get("path", "/"))
                    canary_map.setdefault((host, path), []).append({
                        "name": svc_name,
                        "port": svc_port,
                        "weight": weight,
                        "namespace": ns,
                        "header_name": header_name,
                        "header_value": header_value,
                    })

    processed_gateways: dict[str, dict] = {}
    passthrough_gateways: dict[str, dict] = {}

    for ing in primary:
        meta = ing.get("metadata", {}) or {}
        name = meta.get("name", "ingress")
        namespace = meta.get("namespace", "default")
        ann = meta.get("annotations", {}) or {}
        spec = ing.get("spec", {}) or {}
        emitted_rewrite_warning = False

        tls_hosts: list[str] = []
        tls_host_set: set[str] = set()
        for tls_entry in (spec.get("tls") or []):
            for h in (tls_entry.get("hosts") or []):
                if h not in tls_host_set:
                    tls_hosts.append(h)
                    tls_host_set.add(h)

        rules = spec.get("rules") or []
        default_backend = (spec.get("defaultBackend") or {}).get("service")
        if not rules and not default_backend:
            result.warnings.append(f"Ingress {namespace}/{name}: no rules found, skipped")
            continue

        all_hosts = [r.get("host", "*") for r in rules]
        if not all_hosts:
            all_hosts = ["*"]

        gw_key = namespace
        if gw_key not in processed_gateways:
            gw_name = _safe_name(namespace, "gateway")
            gw = _build_gateway(gw_name, namespace, all_hosts, tls_hosts)
            processed_gateways[gw_key] = gw
            result.resources.append(gw)
        else:
            gw = processed_gateways[gw_key]
            gw_name = gw["metadata"]["name"]
            existing_hostnames = {l.get("hostname") for l in gw["spec"]["listeners"] if l.get("hostname")}
            for h in tls_hosts:
                if h not in existing_hostnames:
                    gw["spec"]["listeners"].insert(0, {
                        "name": _safe_name("https", h.replace("*", "wildcard")),
                        "port": 443, "protocol": "HTTPS", "hostname": h,
                        "tls": {"mode": "Terminate"},
                    })

        cross_ns_refs: dict[str, set[tuple[str, str]]] = {}
        backend_tls_generated: set[str] = set()

        for rule in rules:
            host = rule.get("host", "*")
            http_paths = (rule.get("http") or {}).get("paths") or []
            if not http_paths:
                continue

            grpc_mode = is_grpc(ann)
            passthrough = is_ssl_passthrough(ann)
            route_name = _safe_name(name, host.replace(".", "-").replace("*", "wildcard"))

            if passthrough:
                tls_gw_name = _safe_name(namespace, "nlb-gateway")
                if namespace not in passthrough_gateways:
                    tls_gw = {
                        "apiVersion": GW_API, "kind": "Gateway",
                        "metadata": _meta(tls_gw_name, namespace),
                        "spec": {
                            "gatewayClassName": "nlb",
                            "listeners": [{"name": "tls-passthrough", "port": 443, "protocol": "TLS", "tls": {"mode": "Passthrough"}}],
                        },
                    }
                    passthrough_gateways[namespace] = tls_gw
                    result.resources.append(tls_gw)
                for path_item in http_paths:
                    svc = path_item.get("backend", {}).get("service", {})
                    result.resources.append(_build_tls_route(
                        route_name, namespace, host,
                        svc.get("name", ""), svc.get("port", {}).get("number", 443), tls_gw_name
                    ))
                result.infos.append(
                    f"Ingress {namespace}/{name}: ssl-passthrough → TLSRoute (NLB). "
                    "Ensure Gateway API experimental channel is installed."
                )
                continue

            if grpc_mode:
                grpc_tls_backend = get_backend_protocol(ann) == "GRPCS"
                grpc_rules = []
                for path_item in http_paths:
                    svc = path_item.get("backend", {}).get("service", {})
                    svc_name = svc.get("name", "")
                    svc_port = svc.get("port", {}).get("number", 80)
                    svc_ns = namespace
                    if svc.get("namespace") and svc["namespace"] != namespace:
                        svc_ns = svc["namespace"]
                        cross_ns_refs.setdefault(svc_ns, set()).add(("GRPCRoute", namespace))
                    grpc_rules.append(_build_grpc_rule(path_item.get("path", "/"), svc_name, svc_port, svc_ns, namespace))
                    if grpc_tls_backend and svc_name:
                        tls_key = f"{svc_ns}/{svc_name}/{svc_port}"
                        if tls_key not in backend_tls_generated:
                            backend_tls_generated.add(tls_key)
                            result.resources.append(_build_backend_tls_policy(
                                _safe_name(svc_name, "backend-tls"), svc_ns, svc_name, svc_port
                            ))
                            result.infos.append(
                                f"BackendTLSPolicy generated for GRPCS backend {svc_ns}/{svc_name}:{svc_port}. "
                                "Requires Gateway API experimental channel (v1alpha3). "
                                "If the backend uses a private/self-signed cert, replace wellKnownCACertificates with spec.validation.caCertificateRefs manually. "
                                "Gateway API attaches BackendTLSPolicy to the Service as a whole unless you add sectionName for a named Service port."
                            )
                section = _listener_name_for_host(host) if host in tls_host_set else None
                result.resources.append({
                    "apiVersion": GW_API, "kind": "GRPCRoute",
                    "metadata": _meta(route_name, namespace),
                    "spec": {
                        "parentRefs": [_parent_ref(gw_name, namespace, section)],
                        "hostnames": [host], "rules": grpc_rules,
                    },
                })
                result.infos.append(
                    f"Ingress {namespace}/{name}: backend-protocol {'GRPCS' if grpc_tls_backend else 'GRPC'} "
                    f"→ GRPCRoute{' + BackendTLSPolicy' if grpc_tls_backend else ''} (requires LBC ≥2.14.0)"
                )
                continue

            https_backend = _is_https_backend(ann)
            http_rules = []
            app_root = ann.get(NGINX_PREFIX + "app-root")
            if app_root:
                http_rules.append(_build_redirect_rule("/", app_root, 302))
            for path_item in http_paths:
                path = _clean_path(path_item.get("path", "/"))
                path_type = path_item.get("pathType", "Prefix")
                svc = path_item.get("backend", {}).get("service", {})
                svc_name = svc.get("name", "")
                svc_port = svc.get("port", {}).get("number", 443 if https_backend else 80)
                svc_ns = namespace
                if svc.get("namespace") and svc["namespace"] != namespace:
                    svc_ns = svc["namespace"]
                    cross_ns_refs.setdefault(svc_ns, set()).add(("HTTPRoute", namespace))

                weighted_canary_backends = []
                header_canaries = []
                for canary in canary_map.get((host, path), []):
                    be: dict = {"name": canary["name"], "port": canary["port"]}
                    if canary["namespace"] != namespace:
                        be["namespace"] = canary["namespace"]
                        cross_ns_refs.setdefault(canary["namespace"], set()).add(("HTTPRoute", namespace))
                    if canary.get("header_name"):
                        header_canaries.append((canary, be))
                    else:
                        be["weight"] = canary["weight"]
                        weighted_canary_backends.append(be)

                for canary, be in header_canaries:
                    headers = [{
                        "name": canary["header_name"],
                        "value": canary.get("header_value") or "always",
                        "type": "Exact",
                    }]
                    http_rules.append(_build_http_rule(
                        path, path_type, be["name"], be["port"], ann, namespace,
                        service_namespace=be.get("namespace"), headers=headers,
                    ))

                weight = 100 - sum(b["weight"] for b in weighted_canary_backends) if weighted_canary_backends else 100
                http_rules.append(_build_http_rule(
                    path, path_type, svc_name, svc_port, ann, namespace,
                    weight=weight, extra_backends=weighted_canary_backends or None,
                    service_namespace=svc_ns,
                ))

                rewrite = get_rewrite_target(ann)
                if rewrite and "$" in rewrite and not emitted_rewrite_warning:
                    emitted_rewrite_warning = True
                    result.warnings.append(
                        f"Ingress {namespace}/{name}: rewrite-target '{rewrite}' uses capture groups. "
                        "Gateway API URLRewrite cannot express this directly; regex rewrite was not converted."
                    )

                if https_backend and svc_name:
                    tls_key = f"{svc_ns}/{svc_name}/{svc_port}"
                    if tls_key not in backend_tls_generated:
                        backend_tls_generated.add(tls_key)
                        result.resources.append(_build_backend_tls_policy(
                            _safe_name(svc_name, "backend-tls"), svc_ns, svc_name, svc_port
                        ))
                        result.infos.append(
                            f"BackendTLSPolicy generated for HTTPS backend {svc_ns}/{svc_name}:{svc_port}. "
                            "Requires Gateway API experimental channel (v1alpha3). "
                            "If the backend uses a private/self-signed cert, replace wellKnownCACertificates with spec.validation.caCertificateRefs manually. "
                            "Gateway API attaches BackendTLSPolicy to the Service as a whole unless you add sectionName for a named Service port."
                        )

            if https_backend:
                result.infos.append(
                    f"Ingress {namespace}/{name}: backend-protocol HTTPS → HTTPRoute + BackendTLSPolicy. "
                    "Ensure Gateway API experimental channel is installed."
                )

            route_parent_section = None
            if host in tls_host_set:
                route_parent_section = _listener_name_for_host(host)

            if get_ssl_redirect(ann) and host in tls_host_set:
                result.resources.append({
                    "apiVersion": GW_API, "kind": "HTTPRoute",
                    "metadata": _meta(_safe_name(route_name, "redirect"), namespace),
                    "spec": {
                        "parentRefs": [_parent_ref(gw_name, namespace, "http")],
                        "hostnames": [host],
                        "rules": [{
                            "matches": [_route_match("/", "Prefix")],
                            "filters": [{"type": "RequestRedirect", "requestRedirect": {"scheme": "https", "statusCode": 302}}],
                        }],
                    },
                })
            elif get_ssl_redirect(ann) and host not in tls_host_set:
                result.warnings.append(
                    f"Ingress {namespace}/{name}: ssl-redirect requested for host '{host}' but no TLS listener was generated."
                )

            result.resources.append({
                "apiVersion": GW_API, "kind": "HTTPRoute",
                "metadata": _meta(route_name, namespace),
                "spec": {
                    "parentRefs": [_parent_ref(gw_name, namespace, route_parent_section)],
                    "hostnames": [host],
                    "rules": http_rules,
                },
            })

        if default_backend:
            backend = _backend_ref_from_service(default_backend)
            if backend.get("name"):
                default_route: dict = {
                    "apiVersion": GW_API,
                    "kind": "HTTPRoute",
                    "metadata": _meta(_safe_name(name, "default-backend"), namespace),
                    "spec": {
                        "parentRefs": [_parent_ref(gw_name, namespace, "http")],
                        "rules": [{"backendRefs": [backend]}],
                    },
                }
                if rules:
                    hosts = [r.get("host") for r in rules if r.get("host")]
                    if hosts:
                        default_route["spec"]["hostnames"] = hosts
                result.resources.append(default_route)
                result.infos.append(
                    f"Ingress {namespace}/{name}: spec.defaultBackend → catch-all HTTPRoute backend {backend['name']}."
                )

        for target_ns, refs in cross_ns_refs.items():
            for route_kind, from_ns in refs:
                result.resources.append(_build_reference_grant(from_ns, target_ns, route_kind=route_kind))
                result.infos.append(
                    f"Cross-namespace ref: {route_kind} in {from_ns} → Service in {target_ns}. "
                    f"ReferenceGrant generated in {target_ns}."
                )

        for key, val in ann.items():
            from aws.annotation_map import classify_annotation, WARNING as W, ERROR as E
            cat, desc = classify_annotation(key)
            if cat == W:
                result.warnings.append(f"Ingress {namespace}/{name} [{key}]: {desc}")
            elif cat == E:
                result.errors.append(f"Ingress {namespace}/{name} [{key}]: {desc}")

    # ── L4: TCP services ──────────────────────────────────────────────────────
    if tcp_services:
        nlb_ports, nlb_namespace, tcp_pending = [], "default", []
        for port_str, target in tcp_services.items():
            try:
                port = int(port_str)
            except ValueError:
                continue
            if "/" in target:
                ns_svc, _, tport = target.partition(":")
                tns, _, tsvc = ns_svc.partition("/")
            else:
                tns, tsvc, tport = nlb_namespace, target, port_str
            tport_int = int(tport) if tport.isdigit() else port
            nlb_ports.append(port)
            tcp_pending.append((port, tns, tsvc, tport_int))
        if nlb_ports:
            nlb_gw_name = "nlb-gateway"
            nlb_gw = _build_nlb_gateway(nlb_gw_name, nlb_namespace, nlb_ports)
            for listener in nlb_gw["spec"]["listeners"]:
                listener["allowedRoutes"] = {"namespaces": {"from": "All"}}
            result.resources.append(nlb_gw)
            for port, tns, tsvc, tport in tcp_pending:
                result.resources.append(_build_tcp_route(f"tcp-{port}", tns, port, tsvc, tport, nlb_gw_name, nlb_namespace))
            result.infos.append(
                f"tcp-services ConfigMap → {len(nlb_ports)} TCPRoute(s) + NLB Gateway. "
                "Requires Gateway API experimental channel + LBC ≥2.13.3."
            )

    # ── L4: UDP services ──────────────────────────────────────────────────────
    if udp_services:
        udp_ports, udp_namespace, udp_pending = [], "default", []
        for port_str, target in udp_services.items():
            try:
                port = int(port_str)
            except ValueError:
                continue
            if "/" in target:
                ns_svc, _, tport = target.partition(":")
                tns, _, tsvc = ns_svc.partition("/")
            else:
                tns, tsvc, tport = udp_namespace, target, port_str
            tport_int = int(tport) if tport.isdigit() else port
            udp_ports.append(port)
            udp_pending.append((port, tns, tsvc, tport_int))
        if udp_ports:
            udp_gw_name = "nlb-udp-gateway"
            udp_gw = _build_nlb_gateway(udp_gw_name, "default", udp_ports)
            for l in udp_gw["spec"]["listeners"]:
                l["protocol"] = "UDP"
                l["name"] = l["name"].replace("tcp-", "udp-")
                l["allowedRoutes"] = {"namespaces": {"from": "All"}}
            result.resources.append(udp_gw)
            for port, tns, tsvc, tport in udp_pending:
                result.resources.append(_build_udp_route(f"udp-{port}", tns, port, tsvc, tport, udp_gw_name, "default"))
            result.infos.append(
                f"udp-services ConfigMap → {len(udp_ports)} UDPRoute(s) + NLB Gateway. "
                "Requires Gateway API experimental channel + LBC ≥2.13.3."
            )

    return result


# ── Binary runner ─────────────────────────────────────────────────────────────

def _find_binary() -> str | None:
    for name in _BINARY_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def binary_version() -> str | None:
    """Return ingress2gateway version string, or None if not installed."""
    binary = _find_binary()
    if not binary:
        return None
    try:
        out = subprocess.check_output([binary, "version"], stderr=subprocess.STDOUT, timeout=5)
        return out.decode().strip()
    except Exception:
        return None


def binary_available() -> bool:
    return _find_binary() is not None


def _run_i2gw(ingress_yaml: str, binary: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(ingress_yaml)
        input_path = f.name
    try:
        result = subprocess.run(
            [binary, "print", "--providers=ingress-nginx", f"--input-file={input_path}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ingress2gateway failed (exit {result.returncode}): {result.stderr}")
        return result.stdout
    finally:
        os.unlink(input_path)


def _parse_yaml_docs(yaml_str: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(yaml_str) if d]


def _parse_configmap_data(yaml_str: str | None) -> dict | None:
    if not yaml_str or not yaml_str.strip():
        return None
    for doc in yaml.safe_load_all(yaml_str):
        if doc and doc.get("kind") == "ConfigMap":
            return doc.get("data") or {}
    return None


def run(
    ingress_yaml: str,
    tcp_services_yaml: str = "",
    udp_services_yaml: str = "",
) -> ConversionResult:
    """
    Convert Nginx Ingress YAML to Gateway API resources.
    Uses ingress2gateway binary if available; otherwise uses the built-in Python converter.
    """
    tcp_data = _parse_configmap_data(tcp_services_yaml)
    udp_data = _parse_configmap_data(udp_services_yaml)
    binary = _find_binary()

    if binary:
        cr = ConversionResult()
        try:
            cr.resources = _parse_yaml_docs(_run_i2gw(ingress_yaml, binary))
            cr.infos.append(f"Converted using ingress2gateway binary: {binary}")
            if tcp_data or udp_data:
                l4 = convert([], tcp_services=tcp_data, udp_services=udp_data)
                cr.resources.extend(l4.resources)
                cr.warnings.extend(l4.warnings)
                cr.errors.extend(l4.errors)
                cr.infos.extend(l4.infos)
        except Exception as exc:
            cr.warnings.append(f"ingress2gateway binary failed ({exc}), falling back to Python converter")
            fb = convert(_parse_yaml_docs(ingress_yaml), tcp_services=tcp_data, udp_services=udp_data)
            cr.resources, cr.warnings[1:], cr.errors, cr.infos = fb.resources, fb.warnings, fb.errors, fb.infos
        return cr

    cr = convert(_parse_yaml_docs(ingress_yaml), tcp_services=tcp_data, udp_services=udp_data)
    cr.infos.insert(
        0,
        "ingress2gateway binary not found. Using built-in Python converter. "
        "Install binary for broader annotation coverage: "
        "go install github.com/kubernetes-sigs/ingress2gateway@v1.1.0"
    )
    return cr
