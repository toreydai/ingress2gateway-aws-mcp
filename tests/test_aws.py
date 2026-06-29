"""
Tests for the src/aws/ layer:
  - annotation_map: classification, helpers, CORS reclassification (Fix-2)
  - gateway_class: GatewayClass injection
  - tls_fixup: certificateRefs removal, ACM checklist
  - l4_l7_splitter: L4/L7 split, parentRef rewrite, DNS warnings
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aws.annotation_map import (
    classify_annotation, classify_annotations,
    is_grpc, is_ssl_passthrough, is_canary, get_canary_weight,
    get_cors_config, get_whitelist_cidrs,
    AUTO, PARTIAL, WARNING, ERROR, NGINX_PREFIX,
)
from aws.gateway import inject, ALB_CONTROLLER, NLB_CONTROLLER
from aws.gateway import fix_tls, build_acm_checklist
from aws.gateway import split, compute_dns_changes


# ── annotation_map ────────────────────────────────────────────────────────────

def test_classify_rewrite_target():
    cat, _ = classify_annotation(NGINX_PREFIX + "rewrite-target")
    assert cat == AUTO


def test_classify_auth_url():
    cat, _ = classify_annotation(NGINX_PREFIX + "auth-url")
    assert cat == PARTIAL


def test_classify_limit_rps():
    cat, _ = classify_annotation(NGINX_PREFIX + "limit-rps")
    assert cat == WARNING


def test_classify_config_snippet():
    cat, _ = classify_annotation(NGINX_PREFIX + "configuration-snippet")
    assert cat == ERROR


def test_classify_annotations_dict():
    ann = {
        NGINX_PREFIX + "rewrite-target": "/",
        NGINX_PREFIX + "limit-rps": "100",
        NGINX_PREFIX + "configuration-snippet": "add_header X-Custom true;",
        NGINX_PREFIX + "auth-url": "https://auth.example.com",
    }
    result = classify_annotations(ann)
    assert len(result["auto"]) == 1
    assert len(result["partial"]) == 1
    assert len(result["warning"]) == 1
    assert len(result["error"]) == 1


def test_is_grpc():
    assert is_grpc({NGINX_PREFIX + "backend-protocol": "GRPC"})
    assert is_grpc({NGINX_PREFIX + "backend-protocol": "GRPCS"})
    assert not is_grpc({NGINX_PREFIX + "backend-protocol": "HTTP"})
    assert not is_grpc({})


def test_is_ssl_passthrough():
    assert is_ssl_passthrough({NGINX_PREFIX + "ssl-passthrough": "true"})
    assert not is_ssl_passthrough({NGINX_PREFIX + "ssl-passthrough": "false"})
    assert not is_ssl_passthrough({})


def test_is_canary():
    assert is_canary({NGINX_PREFIX + "canary": "true"})
    assert not is_canary({NGINX_PREFIX + "canary": "false"})


def test_get_canary_weight():
    assert get_canary_weight({NGINX_PREFIX + "canary-weight": "30"}) == 30
    assert get_canary_weight({}) == 0


def test_get_cors_config():
    ann = {
        NGINX_PREFIX + "cors-enable": "true",
        NGINX_PREFIX + "cors-allow-origin": "https://example.com",
    }
    cfg = get_cors_config(ann)
    assert cfg is not None
    assert cfg["allow_origin"] == "https://example.com"


def test_get_whitelist_cidrs():
    ann = {NGINX_PREFIX + "whitelist-source-range": "10.0.0.0/8,192.168.0.0/16"}
    assert get_whitelist_cidrs(ann) == ["10.0.0.0/8", "192.168.0.0/16"]


# Fix-2: CORS reclassified from AUTO → WARNING
def test_cors_annotations_are_warnings():
    cors_suffixes = [
        "cors-enable", "cors-allow-origin", "cors-allow-methods",
        "cors-allow-headers", "cors-allow-credentials", "cors-max-age",
        "cors-expose-headers",
    ]
    for suffix in cors_suffixes:
        cat, desc = classify_annotation(NGINX_PREFIX + suffix)
        assert cat == WARNING, f"{suffix}: expected WARNING, got {cat}"
        assert any(kw in desc for kw in ("ResponseHeaderModifier", "WAF", "application")), (
            f"{suffix}: description should explain why it needs manual migration: {desc}"
        )


# ── gateway_class ─────────────────────────────────────────────────────────────

def _make_gateway(name: str, protocols: list[str]) -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {"name": name, "namespace": "default"},
        "spec": {
            "gatewayClassName": "nginx",
            "listeners": [{"name": f"l-{p.lower()}", "port": 80, "protocol": p} for p in protocols],
        },
    }


def test_inject_alb_for_http():
    resources, _ = inject([_make_gateway("my-gw", ["HTTP"])])
    gws = [r for r in resources if r["kind"] == "Gateway"]
    assert gws[0]["spec"]["gatewayClassName"] == "aws-alb"


def test_inject_nlb_for_tcp():
    resources, _ = inject([_make_gateway("my-nlb", ["TCP"])])
    gws = [r for r in resources if r["kind"] == "Gateway"]
    assert gws[0]["spec"]["gatewayClassName"] == "aws-nlb"


def test_injects_alb_gateway_class():
    resources, _ = inject([_make_gateway("gw", ["HTTP", "HTTPS"])])
    alb_classes = [
        r for r in resources
        if r["kind"] == "GatewayClass" and r["spec"]["controllerName"] == ALB_CONTROLLER
    ]
    assert len(alb_classes) == 1
    assert alb_classes[0]["metadata"]["name"] == "aws-alb"


def test_drops_existing_gateway_class():
    old_gc = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "GatewayClass",
        "metadata": {"name": "old-gc"},
        "spec": {"controllerName": "old.controller"},
    }
    resources, _ = inject([old_gc, _make_gateway("gw", ["HTTP"])])
    names = [r["metadata"]["name"] for r in resources if r["kind"] == "GatewayClass"]
    assert "old-gc" not in names
    assert "aws-alb" in names


def test_custom_class_names():
    resources, _ = inject([_make_gateway("gw", ["HTTP"])], alb_class_name="my-alb", nlb_class_name="my-nlb")
    gws = [r for r in resources if r["kind"] == "Gateway"]
    assert gws[0]["spec"]["gatewayClassName"] == "my-alb"


# ── tls_fixup ─────────────────────────────────────────────────────────────────

def _make_gateway_with_cert_refs(hostname: str) -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {"name": "test-gw", "namespace": "default"},
        "spec": {
            "gatewayClassName": "aws-alb",
            "listeners": [{
                "name": "https",
                "port": 443,
                "protocol": "HTTPS",
                "hostname": hostname,
                "tls": {
                    "mode": "Terminate",
                    "certificateRefs": [{"kind": "Secret", "name": "my-cert"}],
                },
            }],
        },
    }


def test_removes_certificate_refs():
    resources, _, _ = fix_tls([_make_gateway_with_cert_refs("app.example.com")])
    listener = resources[0]["spec"]["listeners"][0]
    assert "certificateRefs" not in listener.get("tls", {})


def test_preserves_terminate_mode():
    resources, _, _ = fix_tls([_make_gateway_with_cert_refs("app.example.com")])
    assert resources[0]["spec"]["listeners"][0]["tls"]["mode"] == "Terminate"


def test_collects_tls_hostnames():
    _, tls_hosts, _ = fix_tls([_make_gateway_with_cert_refs("app.example.com")])
    assert "app.example.com" in tls_hosts


def test_warns_when_cert_refs_removed():
    _, _, warnings = fix_tls([_make_gateway_with_cert_refs("app.example.com")])
    assert any("certificateRefs" in w for w in warnings)


def test_acm_checklist_wildcard():
    checklist = build_acm_checklist(["app.example.com", "api.example.com", "admin.example.com"])
    wildcard_items = [i for i in checklist if i["type"] == "wildcard"]
    assert len(wildcard_items) == 1
    assert wildcard_items[0]["hostname"] == "*.example.com"


def test_acm_checklist_single():
    checklist = build_acm_checklist(["unique.io"])
    assert len(checklist) == 1
    assert checklist[0]["type"] == "exact"


# ── l4_l7_splitter ────────────────────────────────────────────────────────────

def _gw(name: str, protocols: list[str], namespace: str = "default") -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "gatewayClassName": "generic",
            "listeners": [
                {"name": f"l{i}", "port": 80 + i, "protocol": p}
                for i, p in enumerate(protocols)
            ],
        },
    }


def _route(kind: str, name: str, parent_name: str, ns: str = "default") -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "parentRefs": [{"name": parent_name, "namespace": ns}],
            "hostnames": ["example.com"],
            "rules": [],
        },
    }


def test_l7_gateway_gets_alb():
    resources, _ = split([_gw("my-gw", ["HTTP", "HTTPS"])])
    gws = [r for r in resources if r["kind"] == "Gateway"]
    assert gws[0]["spec"]["gatewayClassName"] == "aws-alb"


def test_l4_gateway_gets_nlb():
    resources, _ = split([_gw("my-gw", ["TCP"])])
    gws = [r for r in resources if r["kind"] == "Gateway"]
    assert gws[0]["spec"]["gatewayClassName"] == "aws-nlb"


def test_mixed_gateway_splits():
    resources, _ = split([_gw("mixed-gw", ["HTTP", "TCP"])])
    classes = {r["spec"]["gatewayClassName"] for r in resources if r["kind"] == "Gateway"}
    assert "aws-alb" in classes
    assert "aws-nlb" in classes


def test_mixed_split_warns_dns():
    _, warnings = split([_gw("mixed-gw", ["HTTPS", "TCP"])])
    assert any("DNS" in w or "endpoint" in w.lower() for w in warnings)


def test_http_route_parent_ref_rewritten():
    resources, _ = split([_gw("mixed-gw", ["HTTP", "TCP"]), _route("HTTPRoute", "my-route", "mixed-gw")])
    routes = [r for r in resources if r["kind"] == "HTTPRoute"]
    assert routes[0]["spec"]["parentRefs"][0]["name"] == "mixed-gw-alb"


def test_tcp_route_parent_ref_rewritten():
    resources, _ = split([_gw("mixed-gw", ["HTTP", "TCP"]), _route("TCPRoute", "my-tcp-route", "mixed-gw")])
    routes = [r for r in resources if r["kind"] == "TCPRoute"]
    assert routes[0]["spec"]["parentRefs"][0]["name"] == "mixed-gw-nlb"
