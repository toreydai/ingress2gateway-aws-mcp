"""
Nginx Ingress annotation classification from an AWS Gateway API migration perspective.
Categories: auto | partial | warning | error
"""
from __future__ import annotations

NGINX_PREFIX = "nginx.ingress.kubernetes.io/"

# auto: ingress2gateway or fallback converter handles this fully
AUTO = "auto"
# partial: AWS CRDs provide native capability but need human-supplied parameters
PARTIAL = "partial"
# warning: no direct equivalent; requires manual redesign
WARNING = "warning"
# error: fundamentally incompatible; no migration path
ERROR = "error"

ANNOTATION_RULES: list[tuple[str, str, str]] = [
    # (annotation_suffix, category, description)

    # --- AUTO ---
    ("rewrite-target",              AUTO,    "→ HTTPRoute URLRewrite filter"),
    ("app-root",                    AUTO,    "→ HTTPRoute RequestRedirect from / to app-root"),
    ("use-regex",                   WARNING, "regex path matching is not converted by the fallback converter; rewrite rules with capture groups require manual redesign"),
    ("ssl-redirect",                AUTO,    "→ Gateway HTTP listener + HTTPRoute RequestRedirect"),
    ("force-ssl-redirect",          AUTO,    "→ Gateway HTTP listener + HTTPRoute RequestRedirect"),
    ("permanent-redirect",          AUTO,    "→ HTTPRoute RequestRedirect with target URL"),
    ("temporal-redirect",           AUTO,    "→ HTTPRoute RequestRedirect with target URL"),
    ("permanent-redirect-code",     AUTO,    "→ HTTPRoute RequestRedirect statusCode"),
    ("backend-protocol",            AUTO,    "GRPC/GRPCS → GRPCRoute; HTTPS → BackendTLSPolicy; HTTP default"),
    ("ssl-passthrough",             AUTO,    "→ TLSRoute Passthrough (NLB)"),
    ("cors-enable",                 WARNING, "AWS LBC Gateway API does not support ResponseHeaderModifier → configure CORS via WAF, CloudFront, or application layer"),
    ("cors-allow-origin",           WARNING, "AWS LBC Gateway API does not support ResponseHeaderModifier → handle at application layer"),
    ("cors-allow-methods",          WARNING, "AWS LBC Gateway API does not support ResponseHeaderModifier → handle at application layer"),
    ("cors-allow-headers",          WARNING, "AWS LBC Gateway API does not support ResponseHeaderModifier → handle at application layer"),
    ("cors-allow-credentials",      WARNING, "AWS LBC Gateway API does not support ResponseHeaderModifier → handle at application layer"),
    ("cors-max-age",                WARNING, "AWS LBC Gateway API does not support ResponseHeaderModifier → handle at application layer"),
    ("cors-expose-headers",         WARNING, "AWS LBC Gateway API does not support ResponseHeaderModifier → handle at application layer"),
    ("canary",                      AUTO,    "→ HTTPRoute weighted backendRefs (primary/canary split)"),
    ("canary-weight",               AUTO,    "→ HTTPRoute backendRefs[].weight"),
    ("canary-by-header",            AUTO,    "→ HTTPRoute matches[].headers exact match"),
    ("canary-by-header-value",      AUTO,    "→ HTTPRoute matches[].headers exact value"),
    ("mirror",                      AUTO,    "→ HTTPRoute RequestMirror filter"),
    ("mirror-target",               AUTO,    "→ HTTPRoute RequestMirror backendRef"),
    ("proxy-read-timeout",          AUTO,    "→ LoadBalancerConfiguration idle_timeout.timeout_seconds"),
    ("proxy-send-timeout",          AUTO,    "→ LoadBalancerConfiguration idle_timeout.timeout_seconds when proxy-read-timeout is absent"),
    ("proxy-body-size",             WARNING, "ALB has fixed request size limits; no direct per-Ingress body-size equivalent"),
    ("proxy-next-upstream",         WARNING, "Nginx retry semantics do not map directly to ALB TargetGroupConfiguration"),
    ("session-cookie-name",         PARTIAL, "→ TargetGroupConfiguration stickiness enabled; ALB lb_cookie does not preserve the nginx cookie name"),
    ("session-cookie-expires",      PARTIAL, "→ TargetGroupConfiguration stickiness.lb_cookie.duration_seconds when session-cookie-name is set"),
    ("session-cookie-max-age",      PARTIAL, "→ TargetGroupConfiguration stickiness.lb_cookie.duration_seconds when session-cookie-name is set"),
    ("session-cookie-path",         WARNING, "cookie path attributes are not configurable with ALB lb_cookie stickiness"),
    ("session-cookie-change-on-failure", WARNING, "no ALB equivalent"),
    ("session-cookie-samesite",     WARNING, "cookie SameSite attributes are not configurable with ALB lb_cookie stickiness"),
    ("session-cookie-conditional-samesite-none", WARNING, "cookie SameSite attributes are not configurable with ALB lb_cookie stickiness"),

    # --- PARTIAL (AWS CRDs cover it, but need human-supplied params) ---
    ("auth-url",                    PARTIAL, "→ ListenerRuleConfiguration OIDC action (need OIDC issuer/client config)"),
    ("auth-signin",                 PARTIAL, "→ ListenerRuleConfiguration OIDC signin URL"),
    ("auth-method",                 PARTIAL, "→ ListenerRuleConfiguration OIDC method"),
    ("auth-response-headers",       PARTIAL, "→ ListenerRuleConfiguration OIDC response headers"),
    ("auth-tls-secret",             PARTIAL, "→ ListenerRuleConfiguration (mTLS; need cert ARN)"),
    ("whitelist-source-range",      PARTIAL, "→ ListenerRuleConfiguration source IP conditions"),
    ("denylist-source-range",       PARTIAL, "→ ListenerRuleConfiguration source IP deny conditions"),
    ("default-backend",             WARNING, "nginx annotation is not converted; use spec.defaultBackend for automatic catch-all HTTPRoute generation"),
    ("custom-http-errors",          WARNING, "custom error proxying has no direct ALB equivalent; fixed-response can only return a static body"),

    # --- WARNING (needs manual redesign) ---
    ("auth-type",                   WARNING, "basic auth: ALB has no native equivalent → use OIDC/Cognito or Lambda Authorizer"),
    ("auth-realm",                  WARNING, "basic auth realm: no ALB equivalent"),
    ("auth-secret",                 WARNING, "basic auth secret: no ALB equivalent"),
    ("limit-rps",                   WARNING, "rate limiting: no native ALB equivalent → use AWS WAF Rate-based Rule"),
    ("limit-rpm",                   WARNING, "rate limiting: no native ALB equivalent → use AWS WAF Rate-based Rule"),
    ("limit-connections",           WARNING, "connection limiting: no native ALB equivalent → use AWS WAF"),
    ("limit-burst-multiplier",      WARNING, "no ALB equivalent → AWS WAF"),
    ("canary-by-cookie",            WARNING, "cookie-based canary: ingress2gateway does not support → redesign as header-based"),
    ("canary-by-header-pattern",    WARNING, "header pattern canary: Gateway API lacks regex header match → use exact/prefix"),
    ("proxy-ssl-verify-depth",      WARNING, "backend TLS depth: BackendTLSPolicy lacks depth control"),
    ("proxy-ssl-protocols",         WARNING, "backend TLS protocols: BackendTLSPolicy does not configure protocol versions"),
    ("from-to-www-redirect",        WARNING, "www redirect: implement as separate HTTPRoute with RequestRedirect"),
    ("proxy-connect-timeout",       WARNING, "connect timeout maps loosely to ALB idle timeout (different semantics)"),
    ("proxy-redirect-from",         WARNING, "proxy redirect: no Gateway API equivalent"),
    ("proxy-redirect-to",           WARNING, "proxy redirect: no Gateway API equivalent"),
    ("proxy-set-headers",           WARNING, "ConfigMap-based header injection is not converted; model required headers explicitly in HTTPRoute or the application"),
    ("hide-headers",                WARNING, "response header removal is not supported by AWS LBC Gateway API"),
    ("proxy-cookie-domain",         WARNING, "cookie rewriting: no Gateway API equivalent"),
    ("proxy-cookie-path",           WARNING, "cookie path rewriting: no Gateway API equivalent"),
    ("upstream-vhost",              AUTO,    "→ HTTPRoute RequestHeaderModifier Host header"),
    ("grpc-backend",                WARNING, "deprecated: use backend-protocol: GRPC instead"),
    ("secure-verify-ca-secret",     WARNING, "client cert CA verification: no standard Gateway API equivalent"),

    # --- ERROR (fundamentally incompatible) ---
    ("configuration-snippet",       ERROR,   "Nginx config block: no ALB equivalent; requires complete redesign"),
    ("server-snippet",              ERROR,   "Nginx server config block: no ALB equivalent"),
    ("main-snippet",                ERROR,   "Nginx main config block: no ALB equivalent"),
    ("lua-resty-waf",               ERROR,   "Lua WAF extension: no ALB equivalent"),
    ("lua-resty-limit-req",         ERROR,   "Lua rate limiting: use AWS WAF instead"),
    ("stream-snippet",              ERROR,   "Nginx stream config block: no equivalent"),
    ("fastcgi-params-configmap",    ERROR,   "FastCGI: ALB does not support FastCGI"),
]

# Build lookup dict: full annotation key → (category, description)
_RULE_MAP: dict[str, tuple[str, str]] = {}
for _suffix, _cat, _desc in ANNOTATION_RULES:
    _RULE_MAP[NGINX_PREFIX + _suffix] = (_cat, _desc)
    # Also index without prefix for convenience
    _RULE_MAP[_suffix] = (_cat, _desc)


def classify_annotation(key: str) -> tuple[str, str]:
    """Return (category, description) for a given annotation key."""
    result = _RULE_MAP.get(key)
    if result:
        return result
    # Strip prefix and try again
    bare = key.replace(NGINX_PREFIX, "")
    result = _RULE_MAP.get(bare)
    if result:
        return result
    # Unknown nginx annotation → treat as warning
    if NGINX_PREFIX in key or key.startswith("nginx."):
        return (WARNING, f"Unknown nginx annotation: {key}")
    return (AUTO, f"Non-nginx annotation: passed through")


def classify_annotations(annotations: dict) -> dict:
    """
    Classify all annotations from an Ingress metadata.
    Returns {"auto": [...], "partial": [...], "warning": [...], "error": [...]}
    Each item: {"key": str, "value": str, "description": str}
    """
    result: dict[str, list] = {"auto": [], "partial": [], "warning": [], "error": []}
    for key, value in (annotations or {}).items():
        cat, desc = classify_annotation(key)
        result[cat].append({"key": key, "value": str(value), "description": desc})
    return result


def get_backend_protocol(annotations: dict) -> str:
    """Extract backend-protocol annotation value (HTTP/HTTPS/GRPC/GRPCS)."""
    return annotations.get(NGINX_PREFIX + "backend-protocol", "HTTP").upper()


def is_grpc(annotations: dict) -> bool:
    proto = get_backend_protocol(annotations)
    return proto in ("GRPC", "GRPCS")


def is_ssl_passthrough(annotations: dict) -> bool:
    val = annotations.get(NGINX_PREFIX + "ssl-passthrough", "false")
    return str(val).lower() == "true"


def is_canary(annotations: dict) -> bool:
    val = annotations.get(NGINX_PREFIX + "canary", "false")
    return str(val).lower() == "true"


def get_canary_weight(annotations: dict) -> int:
    try:
        return int(annotations.get(NGINX_PREFIX + "canary-weight", 0))
    except (ValueError, TypeError):
        return 0


def get_rewrite_target(annotations: dict) -> str | None:
    return annotations.get(NGINX_PREFIX + "rewrite-target")


def get_ssl_redirect(annotations: dict) -> bool:
    val = annotations.get(NGINX_PREFIX + "ssl-redirect",
          annotations.get(NGINX_PREFIX + "force-ssl-redirect", "false"))
    return str(val).lower() == "true"


def get_cors_config(annotations: dict) -> dict | None:
    if annotations.get(NGINX_PREFIX + "cors-enable", "false").lower() != "true":
        if not any(k.startswith(NGINX_PREFIX + "cors-") for k in annotations):
            return None
    return {
        "allow_origin":      annotations.get(NGINX_PREFIX + "cors-allow-origin", "*"),
        "allow_methods":     annotations.get(NGINX_PREFIX + "cors-allow-methods", "GET, PUT, POST, DELETE, PATCH, OPTIONS"),
        "allow_headers":     annotations.get(NGINX_PREFIX + "cors-allow-headers", "DNT,Keep-Alive,User-Agent,X-Requested-With,If-Modified-Since,Cache-Control,Content-Type,Range,Authorization"),
        "allow_credentials": annotations.get(NGINX_PREFIX + "cors-allow-credentials", "true"),
        "max_age":           annotations.get(NGINX_PREFIX + "cors-max-age", "1728000"),
        "expose_headers":    annotations.get(NGINX_PREFIX + "cors-expose-headers", ""),
    }


def get_whitelist_cidrs(annotations: dict) -> list[str]:
    raw = annotations.get(NGINX_PREFIX + "whitelist-source-range", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def get_denylist_cidrs(annotations: dict) -> list[str]:
    raw = annotations.get(NGINX_PREFIX + "denylist-source-range", "")
    return [c.strip() for c in raw.split(",") if c.strip()]
