"""
Markdown migration report generator.
"""
from __future__ import annotations
from datetime import datetime


def _status_icon(status: str) -> str:
    return {"ok": "✅", "warning": "⚠️", "error": "❌"}.get(status, "ℹ️")


def _section(title: str, level: int = 2) -> str:
    return f"\n{'#' * level} {title}\n"


def generate(
    analysis: dict,
    conversion: dict,
    preflight: dict | None = None,
    options: dict | None = None,
) -> str:
    opts = options or {}
    parts: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    parts.append("# Nginx Ingress → AWS Gateway API 迁移报告\n")
    parts.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ")
    parts.append(f"**目标**: AWS Load Balancer Controller (Gateway API)")
    parts.append(f"**转换工具**: ingress2gateway-aws-mcp v0.1.0\n")

    # ── Executive Summary ─────────────────────────────────────────────────────
    parts.append(_section("摘要"))
    ingress_count = analysis.get("ingress_count", 0)
    route_types = analysis.get("route_types", {})
    http_count = route_types.get("http", 0)
    grpc_count = route_types.get("grpc", 0)
    tcp_count = route_types.get("tcp", 0)
    udp_count = route_types.get("udp", 0)
    tls_passthrough = route_types.get("tls_passthrough", 0)

    cost = conversion.get("cost_impact", {})
    before_lb = cost.get("before_ingress", ingress_count)
    after_albs = cost.get("after_albs", 0)
    after_nlbs = cost.get("after_nlbs", 0)

    ann = analysis.get("annotations", {})
    auto_count = len(ann.get("auto", []))
    partial_count = len(ann.get("partial", []))
    warning_count = len(ann.get("warning", []))
    error_count = len(ann.get("error", []))

    has_errors = bool(conversion.get("errors")) or error_count > 0
    has_warnings = bool(conversion.get("warnings")) or warning_count > 0

    overall = "❌ 需要人工干预" if has_errors else ("⚠️ 基本可迁移，有注意事项" if has_warnings else "✅ 可自动迁移")
    parts.append(f"**整体评估**: {overall}\n")

    parts.append("| 指标 | 数值 |")
    parts.append("|------|------|")
    parts.append(f"| Ingress 资源数 | {ingress_count} |")
    parts.append(f"| HTTP 路由 | {http_count} |")
    if grpc_count:
        parts.append(f"| gRPC 路由 | {grpc_count} |")
    if tcp_count:
        parts.append(f"| TCP 路由 | {tcp_count} |")
    if udp_count:
        parts.append(f"| UDP 路由 | {udp_count} |")
    if tls_passthrough:
        parts.append(f"| TLS Passthrough | {tls_passthrough} |")
    parts.append(f"| 注解：可自动转换 | {auto_count} |")
    parts.append(f"| 注解：需人工补全 | {partial_count} |")
    parts.append(f"| 注解：需重新设计 | {warning_count} |")
    parts.append(f"| 注解：不兼容 | {error_count} |")
    parts.append("")

    # ── Cost Impact ───────────────────────────────────────────────────────────
    parts.append(_section("成本影响"))
    parts.append("| | 迁移前 | 迁移后 |")
    parts.append("|--|--------|--------|")
    parts.append(f"| 负载均衡器数量 | {before_lb} 个 Ingress (共享 1 个 Nginx LB) | {after_albs} ALB + {after_nlbs} NLB |")
    lb_delta = (after_albs + after_nlbs) - 1
    if lb_delta > 0:
        parts.append(f"\n> ⚠️ **新增 {lb_delta} 个 LB**（每个 ALB/NLB 产生固定小时费用）。"
                     "如需降低成本，设置 `gateway_grouping=single` 合并到最少 LB 数量。")
    else:
        parts.append(f"\n> ✅ LB 数量不变或减少，无额外成本压力。")
    parts.append("")

    # ── DNS Changes ───────────────────────────────────────────────────────────
    dns_changes = conversion.get("dns_changes", [])
    if dns_changes:
        parts.append(_section("DNS 变更"))
        parts.append("| 域名 | 端点类型 | 操作 |")
        parts.append("|------|---------|------|")
        for dc in dns_changes:
            endpoints = " + ".join(dc.get("endpoints", []))
            note = dc.get("note", "")
            parts.append(f"| `{dc['host']}` | {endpoints} | {note} |")
        if any(len(dc.get("endpoints", [])) > 1 for dc in dns_changes):
            parts.append(
                "\n> ⚠️ **双端点场景**：原来一个 Nginx 入口拆成了 ALB（L7）和 NLB（L4），"
                "请为各端点分别配置 DNS CNAME 记录。"
            )
        parts.append("")

    # ── Pre-flight Checks ─────────────────────────────────────────────────────
    if preflight:
        parts.append(_section("前置检查"))
        checks = preflight.get("checks", [])
        blocking = preflight.get("blocking", False)
        if blocking:
            parts.append("> ❌ **存在阻断性问题，请先解决后再执行迁移。**\n")

        for c in checks:
            icon = _status_icon(c["status"])
            parts.append(f"- {icon} **{c['name']}**: {c['message']}")
            if c.get("detail"):
                parts.append(f"  > {c['detail']}")
        parts.append("")

    # ── Annotation Analysis ───────────────────────────────────────────────────
    parts.append(_section("注解分析"))

    if ann.get("auto"):
        parts.append(_section("✅ 可自动转换", 3))
        for a in ann["auto"]:
            parts.append(f"- `{a['key']}: {a['value']}` → {a['description']}")
        parts.append("")

    if ann.get("partial"):
        parts.append(_section("⚠️ 可部分转换（需补充 AWS 参数）", 3))
        for a in ann["partial"]:
            parts.append(f"- `{a['key']}: {a['value']}` → {a['description']}")
        parts.append("")

    if ann.get("warning"):
        parts.append(_section("🔶 需人工重新设计", 3))
        for a in ann["warning"]:
            parts.append(f"- `{a['key']}: {a['value']}` — {a['description']}")
        parts.append("")

    if ann.get("error"):
        parts.append(_section("❌ 不兼容（无法迁移）", 3))
        for a in ann["error"]:
            parts.append(f"- `{a['key']}: {a['value']}` — {a['description']}")
        parts.append("")

    # ── Health Check Reminders ────────────────────────────────────────────────
    tg_infos = [i for i in (conversion.get("infos", [])) if "TargetGroupConfiguration" in i]
    if tg_infos:
        parts.append(_section("健康检查确认"))
        parts.append("> ⚠️ **迁移后最易出现 503 的环节**：ALB Target Group 健康检查路径默认为 `/`，")
        parts.append("> 若应用就绪端点不是 `/`，请在 TargetGroupConfiguration 中修改 `defaultConfiguration.healthCheckConfig.healthCheckPath`。\n")
        for info in tg_infos:
            parts.append(f"- {info}")
        parts.append("")

    # ── Conversion Messages ───────────────────────────────────────────────────
    if conversion.get("errors"):
        parts.append(_section("转换错误"))
        for e in conversion["errors"]:
            parts.append(f"- ❌ {e}")
        parts.append("")

    if conversion.get("warnings"):
        parts.append(_section("转换警告"))
        for w in conversion["warnings"]:
            parts.append(f"- ⚠️ {w}")
        parts.append("")

    remaining_infos = [i for i in (conversion.get("infos", [])) if "TargetGroupConfiguration" not in i]
    if remaining_infos:
        parts.append(_section("提示信息"))
        for i in remaining_infos:
            parts.append(f"- ℹ️ {i}")
        parts.append("")

    # ── ACM Certificate Checklist ─────────────────────────────────────────────
    tls_hosts = analysis.get("tls_hosts", [])
    acm_checklist = conversion.get("acm_checklist", [])
    if tls_hosts or acm_checklist:
        parts.append(_section("ACM 证书准备清单"))
        parts.append("在部署 Gateway 前，确保以下证书已在 ACM 中签发或导入（**同区域**）：\n")
        if acm_checklist:
            for item in acm_checklist:
                parts.append(f"- `{item['hostname']}` — {item['note']}")
                if item.get("covers") and len(item["covers"]) > 1:
                    parts.append(f"  覆盖: {', '.join(item['covers'])}")
        else:
            for h in tls_hosts:
                parts.append(f"- `{h}`")
        parts.append("")

    # ── Migration Checklist ───────────────────────────────────────────────────
    parts.append(_section("迁移操作 Checklist"))
    checklist = [
        ("前置", "确认 AWS LBC 版本 ≥ 2.14.0（L7）/ ≥ 2.13.3（L4）"),
        ("前置", "确认 Gateway API standard channel CRDs 已安装"),
        ("前置", "如有 L4/BackendTLSPolicy：安装 Gateway API experimental channel"),
        ("前置", "确认 AWS LBC CRDs（LoadBalancerConfiguration 等）已安装"),
        ("前置", "ACM 证书准备完毕（见上方清单）"),
        ("执行", "apply GatewayClass 资源（aws-alb / aws-nlb）"),
        ("执行", "apply LoadBalancerConfiguration / TargetGroupConfiguration"),
        ("执行", "apply ListenerRuleConfiguration（认证/IP 条件）并补全 OIDC 参数"),
        ("执行", "apply Gateway 资源，确认 ALB/NLB 已创建"),
        ("执行", "apply HTTPRoute / GRPCRoute / L4 Routes"),
        ("执行", "apply ReferenceGrant（如有跨 namespace 引用）"),
        ("验证", "kubectl get gateway -A 确认状态为 Accepted"),
        ("验证", "kubectl get httproute -A 确认状态为 Accepted + ResolvedRefs"),
        ("验证", "curl / grpcurl 验证各路由规则正常响应"),
        ("验证", "检查 ALB Target Group 健康检查全部 healthy"),
        ("验证", "验证 TLS 证书正常（浏览器 / openssl）"),
        ("切换", "逐步将 DNS CNAME 从 Nginx Service LB 切换到 ALB/NLB DNS 名"),
        ("切换", "观察 ALB 请求指标 5 分钟，无异常后完成切换"),
        ("回滚", "如有问题，将 DNS CNAME 改回 Nginx Service LB（Nginx 资源保留，未删除）"),
        ("清理", "确认迁移稳定后删除旧 Nginx Ingress 资源和 Nginx Ingress Controller"),
    ]
    current_phase = ""
    for phase, item in checklist:
        if phase != current_phase:
            parts.append(f"\n**{phase}阶段**")
            current_phase = phase
        parts.append(f"- [ ] {item}")
    parts.append("")

    # ── Rollback Note ─────────────────────────────────────────────────────────
    parts.append(_section("回滚方案"))
    parts.append(
        "本工具**不删除**任何现有 Nginx Ingress 资源，迁移采用并行运行模式：\n"
        "1. 新的 Gateway API 资源与旧 Nginx Ingress 共存\n"
        "2. 通过 DNS 切换流量（CNAME 指向新 ALB/NLB）\n"
        "3. 如需回滚：将 DNS CNAME 改回 Nginx LoadBalancer Service，立即生效\n"
        "4. 确认无问题后再删除旧 Ingress 资源"
    )

    return "\n".join(parts)
