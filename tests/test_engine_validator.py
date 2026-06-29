"""
Tests for src/engine/, src/validator/, and src/preflight/ layers:
  - fallback_converter: BackendTLSPolicy generation for HTTPS backends (Fix-1)
  - schema_check: controllerName validation severity (Fix-3)
  - preflight/checks: install URL version (Fix-4)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import yaml
from typing import Optional
from converter import convert
from validator import validate
from preflight import check_gateway_api_channels


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_ingress(yaml_str: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(yaml_str) if d and d.get("kind") == "Ingress"]


def _ingress(name: str = "my-ingress", namespace: str = "default",
             annotations: Optional[dict] = None, backend_port: int = 80,
             host: str = "app.example.com", svc: str = "web-svc") -> str:
    ann_block = ""
    if annotations:
        lines = "\n".join(f"    {k}: \"{v}\"" for k, v in annotations.items())
        ann_block = f"  annotations:\n{lines}\n"
    return f"""
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {name}
  namespace: {namespace}
{ann_block}spec:
  rules:
    - host: {host}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {svc}
                port:
                  number: {backend_port}
"""


# ── fallback_converter: BackendTLSPolicy (Fix-1) ─────────────────────────────

HTTPS_ANN = {"nginx.ingress.kubernetes.io/backend-protocol": "HTTPS"}


def test_https_backend_generates_backend_tls_policy():
    docs = _parse_ingress(_ingress(annotations=HTTPS_ANN, backend_port=8443, svc="api-svc"))
    result = convert(docs)
    kinds = [r["kind"] for r in result.resources]
    assert "BackendTLSPolicy" in kinds, f"BackendTLSPolicy not generated. Got: {kinds}"


def test_backend_tls_policy_structure():
    docs = _parse_ingress(_ingress(annotations=HTTPS_ANN, backend_port=8443, svc="api-svc"))
    result = convert(docs)
    policies = [r for r in result.resources if r["kind"] == "BackendTLSPolicy"]
    assert len(policies) == 1
    p = policies[0]
    assert p["apiVersion"] == "gateway.networking.k8s.io/v1alpha3"
    assert p["spec"]["targetRefs"][0]["name"] == "api-svc"
    assert "port" not in p["spec"]["targetRefs"][0]
    assert "hostname" in p["spec"]["validation"]
    assert p["spec"]["validation"]["wellKnownCACertificates"] == "System"
    assert "api-svc" in p["spec"]["validation"]["hostname"]


def test_https_backend_also_generates_http_route():
    docs = _parse_ingress(_ingress(annotations=HTTPS_ANN, backend_port=8443, svc="api-svc"))
    result = convert(docs)
    kinds = [r["kind"] for r in result.resources]
    assert "HTTPRoute" in kinds
    assert "BackendTLSPolicy" in kinds


def test_https_backend_info_mentions_experimental_channel():
    docs = _parse_ingress(_ingress(annotations=HTTPS_ANN, backend_port=8443, svc="api-svc"))
    result = convert(docs)
    combined = " ".join(result.infos)
    assert "BackendTLSPolicy" in combined
    assert "experimental" in combined


def test_https_backend_no_duplicate_policies():
    yaml_str = """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: multi-path
  namespace: default
  annotations:
    nginx.ingress.kubernetes.io/backend-protocol: "HTTPS"
spec:
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: backend-svc
                port:
                  number: 443
          - path: /v2
            pathType: Prefix
            backend:
              service:
                name: backend-svc
                port:
                  number: 443
"""
    result = convert(_parse_ingress(yaml_str))
    policies = [r for r in result.resources if r["kind"] == "BackendTLSPolicy"]
    assert len(policies) == 1, f"Expected 1 BackendTLSPolicy, got {len(policies)}"


def test_http_backend_no_backend_tls_policy():
    docs = _parse_ingress(_ingress(backend_port=80, svc="web-svc"))
    result = convert(docs)
    assert "BackendTLSPolicy" not in [r["kind"] for r in result.resources]


# ── schema_check: controllerName severity (Fix-3) ────────────────────────────

def _gateway_class_yaml(name: str, controller: str) -> str:
    return f"""
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: {name}
spec:
  controllerName: {controller}
"""


def test_wrong_controller_name_makes_valid_false():
    report = validate(_gateway_class_yaml("my-alb", "eks.amazonaws.com/alb"))
    assert not report.valid
    assert any("controllerName" in e for e in report.errors)


def test_correct_alb_controller_no_error():
    report = validate(_gateway_class_yaml("aws-alb", "gateway.k8s.aws/alb"))
    assert not any("controllerName" in e for e in report.errors)


def test_correct_nlb_controller_no_error():
    report = validate(_gateway_class_yaml("aws-nlb", "gateway.k8s.aws/nlb"))
    assert not any("controllerName" in e for e in report.errors)


def test_arbitrary_wrong_controller_is_error():
    report = validate(_gateway_class_yaml("bad-gc", "some.other.io/nlb"))
    assert not report.valid
    assert any("controllerName" in e for e in report.errors)


# ── preflight/checks: install URL version (Fix-4) ────────────────────────────

def test_standard_channel_install_url_is_v1_5_0():
    results = check_gateway_api_channels(
        standard_channel_installed=False,
        experimental_channel_installed=None,
        needs_experimental=False,
    )
    details = " ".join(r.detail for r in results)
    assert "v1.5.0" in details, f"Expected v1.5.0 in install URL, got: {details}"


def test_experimental_channel_install_url_is_v1_5_0():
    results = check_gateway_api_channels(
        standard_channel_installed=True,
        experimental_channel_installed=False,
        needs_experimental=True,
    )
    details = " ".join(r.detail for r in results)
    assert "v1.5.0" in details, f"Expected v1.5.0 in experimental install URL, got: {details}"
