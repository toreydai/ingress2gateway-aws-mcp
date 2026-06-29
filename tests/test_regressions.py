import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from converter import convert
from validator import validate
from aws import crd, gateway


def _ingress_docs(yaml_str: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(yaml_str) if d and d.get("kind") == "Ingress"]


def _pipeline(ingress_yaml: str) -> tuple[list[dict], list[str]]:
    docs = _ingress_docs(ingress_yaml)
    conv = convert(docs)
    resources, _ = gateway.inject(conv.resources)
    resources, _, tls_warnings = gateway.fix_tls(resources)
    resources, split_warnings = gateway.split(resources)
    resources, _, _, _ = gateway.merge(resources)

    annotations = {}
    for doc in docs:
        annotations.update(doc.get("metadata", {}).get("annotations", {}) or {})
    resources, _ = crd.generate_for_resources(resources, "internet-facing", annotations)
    resources, _, tg_infos = crd.generate_for_routes(resources, annotations)
    lrcs, lr_infos, lr_warnings = crd.generate_for_ingresses(docs)
    resources.extend(lrcs)
    resources, attach_infos = crd.attach_listener_rule_configs(resources, docs)

    messages = conv.warnings + tls_warnings + split_warnings + tg_infos + lr_infos + lr_warnings + attach_infos
    return resources, messages


def _load_example(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "input", name)
    with open(path) as f:
        return f.read()


def test_canary_is_scoped_to_matching_path_and_header():
    resources, messages = _pipeline(_load_example("complex-ingress.yaml"))
    route = next(
        r for r in resources
        if r.get("kind") == "HTTPRoute" and r["metadata"]["name"] == "main-app-app-example-com"
    )

    static_rules = [
        rule for rule in route["spec"]["rules"]
        if rule["matches"][0]["path"]["value"] == "/static"
    ]
    assert static_rules
    assert all(
        backend["name"] != "api-service-v2"
        for rule in static_rules
        for backend in rule.get("backendRefs", [])
    )

    api_header_rules = [
        rule for rule in route["spec"]["rules"]
        if rule["matches"][0]["path"]["value"] == "/api" and rule["matches"][0].get("headers")
    ]
    assert len(api_header_rules) == 1
    assert api_header_rules[0]["matches"][0]["headers"][0] == {
        "name": "X-Canary",
        "value": "always",
        "type": "Exact",
    }
    assert sum("rewrite-target" in m for m in messages) == 1


def test_listener_rule_configs_are_attached_and_validate():
    resources, _ = _pipeline(_load_example("complex-ingress.yaml"))
    route = next(
        r for r in resources
        if r.get("kind") == "HTTPRoute" and r["metadata"]["name"] == "main-app-app-example-com"
    )
    backend_rules = [rule for rule in route["spec"]["rules"] if rule.get("backendRefs")]
    assert backend_rules
    for rule in backend_rules:
        refs = [
            f["extensionRef"]["name"]
            for f in rule.get("filters", [])
            if f.get("type") == "ExtensionRef"
        ]
        assert "main-app-oidc-auth" in refs
        assert "main-app-source-ip" in refs

    rendered = yaml.dump_all(resources)
    assert validate(rendered).valid


def test_fixed_response_status_code_is_integer_and_validator_rejects_string():
    cfg = crd.make_fixed_response_config("default-backend", "default")
    assert isinstance(cfg["spec"]["actions"][0]["fixedResponseConfig"]["statusCode"], int)

    cfg["spec"]["actions"][0]["fixedResponseConfig"]["statusCode"] = "404"
    report = validate(yaml.dump(cfg))
    assert not report.valid
    assert any("statusCode" in error for error in report.errors)


def test_tcp_route_cross_namespace_parent_ref_points_to_gateway_namespace():
    result = convert([], tcp_services={"9000": "backend/my-svc:80"})
    tcp_route = next(r for r in result.resources if r.get("kind") == "TCPRoute")
    gateway_res = next(r for r in result.resources if r.get("kind") == "Gateway")

    assert tcp_route["metadata"]["namespace"] == "backend"
    assert tcp_route["spec"]["parentRefs"][0]["namespace"] == gateway_res["metadata"]["namespace"]
    assert gateway_res["spec"]["listeners"][0]["allowedRoutes"]["namespaces"]["from"] == "All"
    assert validate(yaml.dump_all(result.resources)).valid


def test_validator_rejects_bad_parent_section_name():
    bad = """
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: aws-alb
spec:
  controllerName: gateway.k8s.aws/alb
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: gw
  namespace: default
spec:
  gatewayClassName: aws-alb
  listeners:
    - name: http
      port: 80
      protocol: HTTP
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: route
  namespace: default
spec:
  parentRefs:
    - name: gw
      namespace: default
      sectionName: missing
  rules:
    - backendRefs:
        - name: svc
          port: 80
"""
    report = validate(bad)
    assert not report.valid
    assert any("sectionName" in error for error in report.errors)


def test_validator_rejects_backend_tls_policy_target_ref_port():
    bad = """
apiVersion: gateway.networking.k8s.io/v1alpha3
kind: BackendTLSPolicy
metadata:
  name: bad
  namespace: default
spec:
  targetRefs:
    - group: ""
      kind: Service
      name: svc
      port: 8443
  validation:
    hostname: svc.default.svc.cluster.local
    wellKnownCACertificates: System
"""
    report = validate(bad)
    assert not report.valid
    assert any("targetRefs" in error and "port" in error for error in report.errors)


def test_grpcs_backend_generates_grpc_route_and_backend_tls_policy():
    ingress = """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: grpc-secure
  namespace: default
  annotations:
    nginx.ingress.kubernetes.io/backend-protocol: "GRPCS"
spec:
  rules:
    - host: grpc.example.com
      http:
        paths:
          - path: /pkg.Service/Method
            pathType: Prefix
            backend:
              service:
                name: grpc-svc
                port:
                  number: 8443
"""
    result = convert(_ingress_docs(ingress))
    kinds = [r["kind"] for r in result.resources]

    assert "GRPCRoute" in kinds
    assert "BackendTLSPolicy" in kinds
    policy = next(r for r in result.resources if r["kind"] == "BackendTLSPolicy")
    assert policy["spec"]["targetRefs"][0]["name"] == "grpc-svc"
    assert policy["spec"]["validation"]["wellKnownCACertificates"] == "System"


def test_proxy_send_timeout_maps_to_idle_timeout_when_read_timeout_absent():
    resources = [{
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {"name": "gw", "namespace": "default"},
        "spec": {"gatewayClassName": "aws-alb", "listeners": [{"name": "http", "port": 80, "protocol": "HTTP"}]},
    }]
    annotations = {"nginx.ingress.kubernetes.io/proxy-send-timeout": "45"}

    updated, configs = crd.generate_for_resources(resources, ingress_annotations=annotations)

    assert updated
    attrs = configs[0]["spec"]["loadBalancerAttributes"]
    assert {"key": "idle_timeout.timeout_seconds", "value": "45"} in attrs


def test_app_root_redirect_and_upstream_vhost_header_modifier():
    ingress = """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app-root
  namespace: default
  annotations:
    nginx.ingress.kubernetes.io/app-root: /console
    nginx.ingress.kubernetes.io/upstream-vhost: internal.example.local
spec:
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: api
                port:
                  number: 80
"""
    result = convert(_ingress_docs(ingress))
    route = next(r for r in result.resources if r.get("kind") == "HTTPRoute")
    rules = route["spec"]["rules"]

    redirect_rule = next(r for r in rules if r["matches"][0]["path"]["value"] == "/")
    redirect = redirect_rule["filters"][0]["requestRedirect"]
    assert redirect["path"]["replaceFullPath"] == "/console"

    api_rule = next(r for r in rules if r.get("backendRefs"))
    header_filter = next(f for f in api_rule["filters"] if f["type"] == "RequestHeaderModifier")
    assert {"name": "Host", "value": "internal.example.local"} in header_filter["requestHeaderModifier"]["set"]


def test_redirect_target_and_status_code_are_preserved():
    ingress = """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: redirect
  namespace: default
  annotations:
    nginx.ingress.kubernetes.io/permanent-redirect: https://new.example.com/new-path
    nginx.ingress.kubernetes.io/permanent-redirect-code: "308"
spec:
  rules:
    - host: old.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: old
                port:
                  number: 80
"""
    result = convert(_ingress_docs(ingress))
    route = next(r for r in result.resources if r.get("kind") == "HTTPRoute")
    redirect = route["spec"]["rules"][0]["filters"][0]["requestRedirect"]

    assert redirect["scheme"] == "https"
    assert redirect["hostname"] == "new.example.com"
    assert redirect["path"]["replaceFullPath"] == "/new-path"
    assert redirect["statusCode"] == 308


def test_mirror_target_parses_backend_ref():
    ingress = """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: mirror
  namespace: default
  annotations:
    nginx.ingress.kubernetes.io/mirror-target: mirror-svc.observability:8080
spec:
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: app
                port:
                  number: 80
"""
    result = convert(_ingress_docs(ingress))
    route = next(r for r in result.resources if r.get("kind") == "HTTPRoute")
    mirror = next(f for f in route["spec"]["rules"][0]["filters"] if f["type"] == "RequestMirror")

    assert mirror["requestMirror"]["backendRef"] == {
        "name": "mirror-svc",
        "namespace": "observability",
        "port": 8080,
    }


def test_default_backend_generates_catch_all_route():
    ingress = """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: fallback
  namespace: default
spec:
  defaultBackend:
    service:
      name: fallback-svc
      port:
        number: 8080
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: api
                port:
                  number: 80
"""
    resources, messages = _pipeline(ingress)
    route = next(r for r in resources if r.get("kind") == "HTTPRoute" and r["metadata"]["name"] == "fallback-default-backend")

    assert route["spec"]["rules"] == [{"backendRefs": [{"name": "fallback-svc", "port": 8080}]}]
    assert any("catch-all HTTPRoute" in message or "catch-all" in message for message in messages)
    assert validate(yaml.dump_all(resources)).valid


def test_health_check_options_and_stickiness_duration_are_generated():
    resources = [{
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": "route", "namespace": "default"},
        "spec": {"rules": [{"backendRefs": [{"name": "svc", "port": 80}]}]},
    }]
    annotations = {
        "nginx.ingress.kubernetes.io/session-cookie-name": "route",
        "nginx.ingress.kubernetes.io/session-cookie-max-age": "120",
    }

    _, configs, _ = crd.generate_for_routes(
        resources,
        annotations,
        health_check_path="/ready",
        health_check_interval=10,
        healthy_threshold=3,
        unhealthy_threshold=4,
    )

    default_config = configs[0]["spec"]["defaultConfiguration"]
    assert default_config["healthCheckConfig"]["healthCheckPath"] == "/ready"
    assert default_config["healthCheckConfig"]["healthCheckInterval"] == 10
    assert default_config["healthCheckConfig"]["healthyThresholdCount"] == 3
    assert default_config["healthCheckConfig"]["unhealthyThresholdCount"] == 4
    assert {"key": "stickiness.lb_cookie.duration_seconds", "value": "120"} in default_config["targetGroupAttributes"]
