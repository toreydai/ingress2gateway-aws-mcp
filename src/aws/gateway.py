"""
Gateway resource manipulation for AWS LBC compatibility.
Covers: GatewayClass injection, TLS fixup, L4/L7 splitting, Gateway merging.
"""
from __future__ import annotations
import copy
import fnmatch
import re

GW_API = "gateway.networking.k8s.io/v1"

# ── GatewayClass constants ────────────────────────────────────────────────────

ALB_CONTROLLER = "gateway.k8s.aws/alb"
NLB_CONTROLLER = "gateway.k8s.aws/nlb"

L7_PROTOCOLS = {"HTTP", "HTTPS"}
L4_PROTOCOLS = {"TCP", "UDP", "TLS"}
L7_ROUTE_KINDS = {"HTTPRoute", "GRPCRoute"}
L4_ROUTE_KINDS = {"TCPRoute", "UDPRoute", "TLSRoute"}

MERGE_STRATEGIES = {"by-class-scheme", "by-namespace", "by-host", "single"}


# ── shared helpers ────────────────────────────────────────────────────────────

def _safe_name(*parts: str) -> str:
    raw = "-".join(p for p in parts if p)
    return re.sub(r"[^a-z0-9-]", "-", raw.lower()).strip("-")[:63]


def _gw_key(gw: dict) -> str:
    ns = gw.get("metadata", {}).get("namespace", "default")
    name = gw.get("metadata", {}).get("name", "")
    return f"{ns}/{name}"


# ── GatewayClass injection ────────────────────────────────────────────────────

def make_gateway_class(name: str, controller: str) -> dict:
    return {
        "apiVersion": GW_API,
        "kind": "GatewayClass",
        "metadata": {"name": name},
        "spec": {"controllerName": controller},
    }


def inject(
    resources: list[dict],
    alb_class_name: str = "aws-alb",
    nlb_class_name: str = "aws-nlb",
) -> tuple[list[dict], list[dict]]:
    """
    Drop any existing GatewayClass resources and replace generic gatewayClassName values:
      HTTP/HTTPS listeners → alb_class_name (ALB)
      TCP/UDP/TLS listeners → nlb_class_name (NLB)
    Returns (updated_resources, gateway_class_resources).
    """
    alb_gc = make_gateway_class(alb_class_name, ALB_CONTROLLER)
    nlb_gc = make_gateway_class(nlb_class_name, NLB_CONTROLLER)

    updated = []
    needs_alb = needs_nlb = False

    for res in resources:
        if res.get("kind") == "GatewayClass":
            continue
        if res.get("kind") == "Gateway":
            protocols = {l.get("protocol", "HTTP") for l in res.get("spec", {}).get("listeners", [])}
            if bool(protocols & L4_PROTOCOLS) and not bool(protocols & L7_PROTOCOLS):
                res["spec"]["gatewayClassName"] = nlb_class_name
                needs_nlb = True
            else:
                res["spec"]["gatewayClassName"] = alb_class_name
                needs_alb = True
        updated.append(res)

    gc_resources = ([alb_gc] if needs_alb else []) + ([nlb_gc] if needs_nlb else [])
    return gc_resources + updated, gc_resources


# ── TLS fixup ─────────────────────────────────────────────────────────────────

def fix_tls(
    resources: list[dict],
    acm_cert_arns: list[str] | None = None,
) -> tuple[list[dict], list[str], list[str]]:
    """
    Remove certificateRefs from HTTPS listeners (unsupported by AWS LBC) and
    ensure the hostname field is set for ACM certificate discovery.
    Returns (updated_resources, tls_hostnames, warnings).
    """
    tls_hostnames: list[str] = []
    warnings: list[str] = []
    for res in resources:
        if res.get("kind") != "Gateway":
            continue
        gw_name = res.get("metadata", {}).get("name", "unknown")
        for listener in res.get("spec", {}).get("listeners", []):
            if listener.get("protocol") != "HTTPS":
                continue
            tls_block = listener.get("tls", {})
            if "certificateRefs" in tls_block:
                del tls_block["certificateRefs"]
                warnings.append(
                    f"Gateway {gw_name}: removed certificateRefs from listener "
                    f"'{listener.get('name')}' — AWS LBC uses ACM certificate discovery via hostname"
                )
            if not listener.get("tls"):
                listener["tls"] = {
                    "mode": "Terminate",
                    "options": {"gateway.k8s.aws/certificate-discovery": "acm"},
                }
            else:
                listener["tls"]["mode"] = "Terminate"
                listener["tls"].setdefault(
                    "options",
                    {"gateway.k8s.aws/certificate-discovery": "acm"},
                )

            hostname = listener.get("hostname")
            if hostname:
                if hostname not in tls_hostnames:
                    tls_hostnames.append(hostname)
            else:
                warnings.append(
                    f"Gateway {gw_name}: HTTPS listener '{listener.get('name')}' "
                    "has no hostname — ACM certificate discovery will fail."
                )

    return resources, tls_hostnames, warnings


def build_acm_checklist(tls_hostnames: list[str]) -> list[dict]:
    """
    Group TLS hostnames into wildcard vs exact ACM certificate needs.
    """
    from collections import Counter
    domain_counts: Counter = Counter()
    for h in tls_hostnames:
        parts = h.split(".")
        if len(parts) >= 2:
            domain_counts[".".join(parts[1:])] += 1

    checklist = []
    for domain, count in domain_counts.items():
        if count > 1:
            checklist.append({
                "type": "wildcard",
                "hostname": f"*.{domain}",
                "covers": [h for h in tls_hostnames if h.endswith(f".{domain}")],
                "note": f"Single wildcard cert covers {count} hostnames",
            })
        else:
            h = next(h for h in tls_hostnames if h.endswith(f".{domain}") or h == domain)
            checklist.append({
                "type": "exact",
                "hostname": h,
                "covers": [h],
                "note": "Exact cert needed",
            })
    return checklist


# ── L4/L7 splitter ───────────────────────────────────────────────────────────

def _gateway_layer(gateway: dict) -> str:
    protocols = {l.get("protocol", "HTTP") for l in gateway.get("spec", {}).get("listeners", [])}
    has_l7 = bool(protocols & L7_PROTOCOLS)
    has_l4 = bool(protocols & L4_PROTOCOLS)
    if has_l7 and has_l4:
        return "mixed"
    return "l4" if has_l4 else "l7"


def _split_mixed_gateway(gw: dict) -> tuple[dict | None, dict | None]:
    name = gw["metadata"]["name"]
    listeners = gw["spec"]["listeners"]
    l7_listeners = [l for l in listeners if l.get("protocol", "HTTP") in L7_PROTOCOLS]
    l4_listeners = [l for l in listeners if l.get("protocol", "HTTP") not in L7_PROTOCOLS]
    l7_gw = (copy.deepcopy(gw) | {"metadata": {**gw["metadata"], "name": f"{name}-alb"},
                                    "spec": {**gw["spec"], "listeners": l7_listeners}}) if l7_listeners else None
    l4_gw = (copy.deepcopy(gw) | {"metadata": {**gw["metadata"], "name": f"{name}-nlb"},
                                    "spec": {**gw["spec"], "listeners": l4_listeners}}) if l4_listeners else None
    return l7_gw, l4_gw


def split(
    resources: list[dict],
    alb_class_name: str = "aws-alb",
    nlb_class_name: str = "aws-nlb",
) -> tuple[list[dict], list[str]]:
    """
    Split mixed L4/L7 Gateways into separate ALB/NLB Gateways and rewrite Route parentRefs.
    Returns (updated_resources, warnings).
    """
    warnings: list[str] = []
    gateways = {_gw_key(r): r for r in resources if r.get("kind") == "Gateway"}

    new_gateways: list[dict] = []
    rename_map: dict[str, dict[str, str]] = {}
    replaced: set[str] = set()

    for key, gw in gateways.items():
        layer = _gateway_layer(gw)
        if layer == "mixed":
            l7_gw, l4_gw = _split_mixed_gateway(gw)
            old_name = gw["metadata"]["name"]
            rename_map[key] = {}
            warnings.append(
                f"Gateway {key}: mixed L4/L7 — splitting into ALB ({old_name}-alb) "
                f"and NLB ({old_name}-nlb). ⚠️ DNS: two separate endpoints."
            )
            if l7_gw:
                l7_gw["spec"]["gatewayClassName"] = alb_class_name
                new_gateways.append(l7_gw)
                rename_map[key]["l7"] = l7_gw["metadata"]["name"]
            if l4_gw:
                l4_gw["spec"]["gatewayClassName"] = nlb_class_name
                new_gateways.append(l4_gw)
                rename_map[key]["l4"] = l4_gw["metadata"]["name"]
        elif layer == "l4":
            gw["spec"]["gatewayClassName"] = nlb_class_name
            new_gateways.append(gw)
            warnings.append(f"Gateway {key}: L4-only → NLB. ⚠️ Separate NLB DNS endpoint.")
        else:
            gw["spec"]["gatewayClassName"] = alb_class_name
            new_gateways.append(gw)
        replaced.add(key)

    updated: list[dict] = []
    for res in resources:
        if res.get("kind") == "Gateway":
            continue
        if res.get("kind") in L7_ROUTE_KINDS | L4_ROUTE_KINDS:
            res = copy.deepcopy(res)
            new_refs = []
            for pref in res.get("spec", {}).get("parentRefs", []):
                pref_ns = pref.get("namespace", res["metadata"].get("namespace", "default"))
                pref_key = f"{pref_ns}/{pref.get('name', '')}"
                if pref_key in rename_map:
                    rmap = rename_map[pref_key]
                    target = "l7" if res["kind"] in L7_ROUTE_KINDS else "l4"
                    if target in rmap:
                        pref = {**pref, "name": rmap[target]}
                new_refs.append(pref)
            res["spec"]["parentRefs"] = new_refs
        updated.append(res)

    return new_gateways + updated, warnings


def compute_dns_changes(resources: list[dict]) -> list[dict]:
    """Identify hosts that span multiple LB endpoints after L4/L7 split."""
    gw_class = {
        f"{r['metadata'].get('namespace','default')}/{r['metadata']['name']}": r["spec"].get("gatewayClassName", "")
        for r in resources if r.get("kind") == "Gateway"
    }
    host_to_gws: dict[str, list[str]] = {}
    for res in resources:
        if res.get("kind") not in ("HTTPRoute", "GRPCRoute", "TLSRoute", "TCPRoute"):
            continue
        ns = res.get("metadata", {}).get("namespace", "default")
        for pref in res.get("spec", {}).get("parentRefs", []):
            gw_key = f"{pref.get('namespace', ns)}/{pref.get('name', '')}"
            cls = gw_class.get(gw_key, "unknown")
            for h in res.get("spec", {}).get("hostnames", []):
                entry = f"{pref.get('name')} ({cls})"
                if entry not in host_to_gws.get(h, []):
                    host_to_gws.setdefault(h, []).append(entry)

    changes = []
    for host, gws in host_to_gws.items():
        lb_types = {"ALB" if "aws-alb" in g else "NLB" if "aws-nlb" in g else "LB" for g in gws}
        changes.append({
            "host": host,
            "endpoints": sorted(lb_types),
            "gateways": gws,
            "note": "Multiple LB endpoints — update DNS CNAME for each" if len(lb_types) > 1 else "Single endpoint",
        })
    return changes


# ── Gateway merger ────────────────────────────────────────────────────────────

def _merge_listeners(target: dict, source: dict) -> None:
    existing = {
        (l.get("protocol"), l.get("port"), l.get("hostname", "")): True
        for l in target["spec"]["listeners"]
    }
    for l in source["spec"]["listeners"]:
        key = (l.get("protocol"), l.get("port"), l.get("hostname", ""))
        if key not in existing:
            target["spec"]["listeners"].append(l)
            existing[key] = True


def _allow_routes_from_all(gateway: dict) -> None:
    for listener in gateway.get("spec", {}).get("listeners", []):
        listener["allowedRoutes"] = {"namespaces": {"from": "All"}}


def merge(
    resources: list[dict],
    strategy: str = "by-class-scheme",
    scheme: str = "internet-facing",
) -> tuple[list[dict], dict[str, str], int, int]:
    """
    Consolidate Gateways to minimize ALB/NLB count (and cost).
    Returns (updated_resources, rename_map, before_count, after_count).
    """
    if strategy not in MERGE_STRATEGIES:
        strategy = "by-class-scheme"

    gateways = [r for r in resources if r.get("kind") == "Gateway"]
    others = [r for r in resources if r.get("kind") != "Gateway"]
    before_count = len(gateways)

    if not gateways:
        return resources, {}, 0, 0

    groups: dict[str, list[dict]] = {}
    for gw in gateways:
        ns = gw.get("metadata", {}).get("namespace", "default")
        cls = gw.get("spec", {}).get("gatewayClassName", "")
        gw_scheme = gw.get("_scheme", scheme)
        if strategy == "by-class-scheme":
            key = f"{cls}_{gw_scheme}_{ns}"
        elif strategy == "by-namespace":
            key = f"{cls}_{ns}"
        elif strategy == "single":
            key = cls
        else:  # by-host
            key = _gw_key(gw)
        groups.setdefault(key, []).append(gw)

    merged_gateways: list[dict] = []
    rename_map: dict[str, str] = {}

    for gws in groups.values():
        if len(gws) == 1:
            gw = gws[0]
            if strategy == "single":
                _allow_routes_from_all(gw)
            merged_gateways.append(gw)
            continue
        gws_sorted = sorted(gws, key=lambda g: g["metadata"]["name"])
        primary = copy.deepcopy(gws_sorted[0])
        ns = primary.get("metadata", {}).get("namespace", "default")
        cls = primary.get("spec", {}).get("gatewayClassName", "")
        lb_type = "alb" if "alb" in cls else "nlb"
        primary["metadata"]["name"] = _safe_name(ns, lb_type, "gateway")
        for secondary in gws_sorted[1:]:
            rename_map[_gw_key(secondary)] = f"{ns}/{primary['metadata']['name']}"
            _merge_listeners(primary, secondary)
        if strategy == "single":
            _allow_routes_from_all(primary)
        first_old = _gw_key(gws_sorted[0])
        new_key = f"{ns}/{primary['metadata']['name']}"
        if first_old != new_key:
            rename_map[first_old] = new_key
        merged_gateways.append(primary)

    updated_others = []
    for res in others:
        if res.get("kind") in L7_ROUTE_KINDS | L4_ROUTE_KINDS:
            res = copy.deepcopy(res)
            new_refs = []
            for pref in res.get("spec", {}).get("parentRefs", []):
                pref_ns = pref.get("namespace", res["metadata"].get("namespace", "default"))
                old_key = f"{pref_ns}/{pref.get('name', '')}"
                if old_key in rename_map:
                    new_ns, new_name = rename_map[old_key].split("/", 1)
                    pref = {**pref, "name": new_name, "namespace": new_ns}
                new_refs.append(pref)
            res["spec"]["parentRefs"] = new_refs
        updated_others.append(res)

    return merged_gateways + updated_others, rename_map, before_count, len(merged_gateways)
