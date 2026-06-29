# 架构文档

**项目**: ingress2gateway-aws-mcp  
**版本**: v0.1  
**日期**: 2026-06-29

---

## 1. 项目定位

`ingress2gateway-aws-mcp` 是一个 **MCP（Model Context Protocol）Server**，为 Kiro / Claude 提供对话式的 Nginx Ingress → AWS Gateway API 迁移能力。它不是一个独立运行的 CLI，而是挂载到 Kiro IDE、Kiro CLI、Claude Desktop 或 Claude Code，让用户以自然语言完成迁移全流程。

### 核心价值

| 痛点 | 本工具解决方式 |
|------|--------------|
| Ingress-NGINX 2026年3月退役，无路可走 | 自动转换到 AWS LBC Gateway API |
| 官方 `ingress2gateway` 不含 AWS 专属能力 | 叠加 AWS 后处理层（GatewayClass / TLS / CRD 生成）|
| 1:1 映射导致 ALB 数量/成本爆炸 | Gateway 合并策略，收敛负载均衡器数量 |
| 转换结果对不对、能不能 apply？ | 离线 schema 校验 + 迁移前置检查 |
| 手动翻注解文档耗时 | 对话式驱动，自动分类注解兼容性 |

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────┐
│                  用户（Kiro / Claude 对话）                   │
│  "帮我分析这个 ingress.yaml"                               │
│  "转换成 Gateway API YAML"                                │
│  "生成迁移报告"                                            │
└────────────────────────┬─────────────────────────────────┘
                         │ MCP 协议（stdio）
┌────────────────────────▼─────────────────────────────────┐
│              MCP Server (src/server.py)                   │
│                                                          │
│  ┌──────────────────┐  ┌──────────────────────────────┐  │
│  │ check_prerequisites│ │      analyze_ingress         │  │
│  └──────────────────┘  └──────────────────────────────┘  │
│  ┌──────────────────┐  ┌──────────────────────────────┐  │
│  │convert_to_gateway│  │  generate_migration_report   │  │
│  │      _api        │  └──────────────────────────────┘  │
│  └──────────────────┘  ┌──────────────────────────────┐  │
│                        │      validate_output          │  │
│                        └──────────────────────────────┘  │
└────────────────────────┬─────────────────────────────────┘
                         │
        ┌────────────────┼────────────────────┐
        │                │                    │
┌───────▼──────┐  ┌──────▼──────┐  ┌─────────▼────────┐
│  Converter   │  │  AWS Layer  │  │  Support Layer   │
│              │  │             │  │                  │
│ converter.py │  │  gateway.py │  │  preflight.py    │
│ run() /      │  │  (inject /  │  │  reporter.py     │
│ convert()    │  │  fix_tls /  │  │  validator.py    │
└───────┬──────┘  │  split /    │  └──────────────────┘
        │         │  merge)     │
┌───────▼──────┐  │             │
│ingress2gateway│  │  crd.py     │
│ Go binary    │  │  (lb/tg/lr  │
│(可选外部依赖) │  │  config)    │
└──────────────┘  │             │
                  │annotation_  │
                  │map.py       │
                  └─────────────┘
└──────────────┘
```

---

## 3. 模块职责

### 3.0 目录布局

```
ingress2gateway-aws-mcp/
├── src/
│   ├── server.py              # MCP 入口，注册 5 个工具
│   ├── converter.py           # 转换引擎：调用 ingress2gateway 二进制或内置 Python 转换器
│   ├── preflight.py           # 迁移前置检查（LBC 版本、CRD、feature gate、ACM）
│   ├── reporter.py            # Markdown 迁移报告生成器
│   ├── validator.py           # 离线 YAML schema 校验（不需要连集群）
│   └── aws/
│       ├── annotation_map.py  # Nginx 注解分类规则（auto / partial / warning / error）
│       ├── gateway.py         # Gateway 操作：GatewayClass 注入、TLS fixup、L4/L7 分离、合并
│       └── crd.py             # AWS CRD 生成：LoadBalancerConfiguration / TargetGroupConfiguration / ListenerRuleConfiguration
├── tests/
│   ├── test_aws.py            # aws/ 层单元测试（annotation_map、gateway、tls、splitter）
│   └── test_engine_validator.py  # converter、validator、preflight 单元测试
├── examples/
│   ├── input/                 # 示例 Nginx Ingress / ConfigMap YAML
│   └── output/                # 对应的 Gateway API 转换结果
├── infra/
│   ├── cluster.yaml           # eksctl 一键建集群（含 VPC、OIDC、LBC IRSA）
│   ├── lbc-values.yaml        # AWS LBC Helm values
│   ├── lbc-iam-policy.json    # LBC 所需 IAM Policy
│   └── test-backends.yaml     # 测试用后端 Deployment / Service
├── docs/
│   ├── architecture.md        # 架构说明、模块职责、转换管道数据流
│   ├── annotation-reference.md  # Nginx 注解兼容性速查表
│   └── ops.md                 # 部署手册 + 故障排查
├── Dockerfile
└── pyproject.toml
```

### 3.1 MCP Server (`src/server.py`)

入口层，注册 5 个工具，负责：
- 参数解析与文件路径读取（`_load_yaml_or_path`）
- 编排各模块的调用顺序
- 汇总 errors / warnings / infos 并返回 JSON / Markdown

### 3.2 转换层 (`src/converter.py`)

| 函数 | 职责 |
|------|------|
| `run()` | 调用 `ingress2gateway` Go 二进制（subprocess），传入 Ingress YAML 及 tcp/udp-services ConfigMap；二进制不存在时自动切换到 `convert()` |
| `convert()` | 纯 Python 内置转换器，覆盖常见 Ingress 场景，不依赖 Go 二进制 |

**关键设计**：转换层只负责基础结构转换，不含任何 AWS 专属逻辑，便于跟随官方工具升级。

### 3.3 AWS 后处理层 (`src/aws/`)

按顺序依次执行，每步输入上一步的资源列表：

```
ingress2gateway 输出
        │
        ▼
gateway.inject()       ← 注入 aws-alb / aws-nlb GatewayClass
        │
        ▼
gateway.fix_tls()      ← 移除 certificateRefs，改 hostname 证书发现
        │
        ▼
gateway.split()        ← L4/L7 分流 + 重写 parentRefs / gatewayClassName
        │
        ▼
gateway.merge()        ← 按策略合并 Gateway，控制 ALB/NLB 数量
        │
        ▼
crd.generate_for_resources()   ← 生成 LoadBalancerConfiguration
        │
        ▼
crd.generate_for_routes()      ← 生成 TargetGroupConfiguration（健康检查 / stickiness）
        │
        ▼
crd.generate_for_ingresses()   ← 生成 ListenerRuleConfiguration（认证/源IP）
        │
        ▼
  最终资源列表（可 kubectl apply）
```

| 模块 | AWS CRD / 能力 | 包含原功能 |
|------|--------------|-----------|
| `gateway.py` | GatewayClass 注入、TLS fixup、L4/L7 分离、Gateway 合并 | gateway_class / tls_fixup / l4_l7_splitter / gateway_merger |
| `crd.py` | LoadBalancerConfiguration、TargetGroupConfiguration、ListenerRuleConfiguration | lb_config / target_group_config / listener_rule_config |
| `annotation_map.py` | 注解分类（auto/partial/warn/error） | — |

### 3.4 Support 层

| 模块 | 职责 |
|------|------|
| `src/preflight.py` | 检查 LBC 版本、Gateway API Channel（standard/experimental）、AWS CRD、feature gate、ACM 证书覆盖 |
| `src/reporter.py` | 生成 Markdown 迁移报告（摘要、成本、DNS、Checklist、回滚说明）|
| `src/validator.py` | 离线校验生成的 YAML（schema + AWS 约束，无需连接集群）|

---

## 4. 数据流

### 4.1 `analyze_ingress` 数据流

```
用户输入 (ingress.yaml)
    │
    ├── YAML 解析 → Ingress 文档列表
    ├── annotation_map.classify_annotations() → auto/partial/warn/error
    ├── 路由类型统计（HTTP/gRPC/TCP/UDP/TLS passthrough）
    ├── TLS host 提取
    ├── 跨 namespace 引用检测
    └── 成本预估（projected ALB/NLB 数量）
    │
    ▼
JSON 结构化摘要（返回给 Claude）
```

### 4.2 `convert_to_gateway_api` 数据流

```
用户输入 (ingress.yaml + 可选 tcp/udp ConfigMap)
    │
    ▼ _run_conversion_pipeline()
    │
    ├── converter.run() → 基础资源列表
    ├── gateway.inject() → + GatewayClass 资源
    ├── gateway.fix_tls() → TLS listener 修正 + ACM checklist
    ├── gateway.split() → L4/L7 分流 + dns_changes
    ├── gateway.merge() → 合并后资源 + cost_impact
    ├── crd.generate_for_resources() → + LoadBalancerConfiguration
    ├── crd.generate_for_routes() → + TargetGroupConfiguration
    ├── crd.generate_for_ingresses() → + ListenerRuleConfiguration
    └── crd.attach_listener_rule_configs() → HTTPRoute ExtensionRef 绑定认证/IP 条件
    │
    ▼
{
  combined_yaml: "---\napiVersion:...",  ← 可直接 kubectl apply
  cost_impact: { before_ingress, after_albs, after_nlbs },
  dns_changes: [...],
  acm_checklist: [...],
  errors/warnings/infos: [...]
}
```

---

## 5. AWS 资源模型

### 5.1 L7 路径（ALB）

```
GatewayClass (aws-alb)
    └── Gateway (ALB)
          ├── LoadBalancerConfiguration  ← scheme/subnet/SG
          └── Listeners (HTTP:80, HTTPS:443)
                └── HTTPRoute / GRPCRoute
                      ├── TargetGroupConfiguration ← 健康检查/target type/stickiness
                      └── ListenerRuleConfiguration ← OIDC/源IP
```

### 5.2 L4 路径（NLB）

```
GatewayClass (aws-nlb)
    └── Gateway (NLB)
          ├── LoadBalancerConfiguration
          └── Listeners (TCP/UDP 端口)
                └── TCPRoute / UDPRoute / TLSRoute
                      └── TargetGroupConfiguration
```

### 5.3 跨 Namespace 场景

```
frontend-ns/HTTPRoute  ──→  backend-ns/Service
                             ↑
                    ReferenceGrant (backend-ns)
                    允许 frontend-ns 的 HTTPRoute 引用
```

---

## 6. 关键设计决策

### 6.1 不重复造转换引擎

**决策**：优先复用 `kubernetes-sigs/ingress2gateway` v1.1.0 官方工具，缺失时使用内置 Python 转换器。  
**理由**：官方工具覆盖标准 Gateway API 转换，本工具聚焦 AWS 专属后处理、AWS LBC 不支持能力的风险分类，以及可 apply 性校验。

### 6.2 Go 二进制降级策略

**决策**：`ingress2gateway` 二进制不存在时，自动切换内置 Python 转换器（`converter.convert()`）。  
**理由**：降低用户安装成本，保证工具可用；降级时在 `check_prerequisites` 输出 WARNING 提示。

### 6.3 Gateway 合并（默认 by-class-scheme）

**决策**：默认将同类 Ingress 合并到尽量少的 Gateway。  
**理由**：每个 Gateway = 一个 ALB/NLB = 独立计费。1:1 映射会导致成本失控。  
**可覆盖**：`gateway_grouping` 参数支持 `by-namespace`、`by-host`、`single`。

### 6.4 TLS 不用 certificateRefs

**决策**：AWS LBC 不支持 Gateway listener 的 `certificateRefs` 字段，改用 listener `hostname` 做 ACM 证书自动发现。  
**理由**：AWS LBC 硬约束，不可绕过。

### 6.5 L4/L7 强制分离

**决策**：TCPRoute/UDPRoute/TLSRoute 与 HTTPRoute/GRPCRoute 不能混挂同一 Gateway，强制拆分到 NLB/ALB。  
**运维后果**：一个原始 Ingress 迁移后可能对应两个 DNS 端点（ALB + NLB），报告中自动输出 `dns_changes` 警告。

---

## 7. 工具接口概览

| 工具 | 输入 | 输出 | 典型调用时机 |
|------|------|------|------------|
| `check_prerequisites` | lbc_version, needs_l4, tls_hostnames | JSON 检查报告 | 迁移前环境评估 |
| `analyze_ingress` | ingress_yaml, tcp/udp ConfigMap | JSON 分析摘要 | 了解现状、估成本 |
| `convert_to_gateway_api` | ingress_yaml + options | JSON（含 combined_yaml） | 生成可 apply 的 YAML |
| `generate_migration_report` | ingress_yaml + options | Markdown 报告 | 生成交付文档 |
| `validate_output` | gateway_yaml | JSON 校验结果 | apply 前最后检查 |

---

## 8. 外部依赖

| 依赖 | 版本要求 | 是否必须 | 说明 |
|------|---------|---------|------|
| Python | ≥ 3.11 | 必须 | 运行环境 |
| `mcp[cli]` | ≥ 1.0.0 | 必须 | FastMCP 框架 |
| `pyyaml` | ≥ 6.0 | 必须 | YAML 解析 |
| `packaging` | ≥ 24.0 | 必须 | 版本号比较（preflight 检查）|
| `ingress2gateway` Go binary | 1.1.0 | 可选 | 缺失时自动降级 Python 转换器；AWS LBC 不支持的能力仍需人工迁移 |
| AWS LBC | ≥ 2.14.0 (L7) / ≥ 2.13.3 (L4) | 目标集群必须 | 生成的 YAML 才能部署 |
| Gateway API CRDs | v1.5.0+ | 目标集群必须 | standard channel；L4 需 experimental |
