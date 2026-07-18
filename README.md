# ingress2gateway-aws-mcp

MCP Server：将 **Nginx Ingress** 转换为 **AWS Gateway API**（ALB / NLB）资源的对话式迁移工具。

## 背景

Ingress-NGINX 已于 2026 年 3 月退役，AWS Load Balancer Controller v2.14+ 正式支持 Kubernetes Gateway API。本工具封装社区官方转换器 `ingress2gateway`，在其基础上叠加 AWS 专属后处理，提供对话式迁移体验。

## 工具列表

| 工具 | 功能 |
|------|------|
| `check_prerequisites` | 检查 LBC 版本、Gateway API CRD、feature gate、ACM 证书 |
| `analyze_ingress` | 分析 Nginx Ingress 注解分布、路由类型、成本预估 |
| `convert_to_gateway_api` | 完整转换管道 → 生成可 `kubectl apply` 的 YAML |
| `generate_migration_report` | 输出 Markdown 迁移报告（成本/DNS/健康检查/Checklist）|
| `validate_output` | 离线校验生成的 Gateway API YAML |

## 安装

### 方式一：本地 pip

```bash
pip install .
```

需要 Python 3.11+。可选：安装 `ingress2gateway` Go 二进制以获得官方转换器覆盖；AWS LBC 不支持的能力仍会由本工具报告为 warning/error（不安装时自动使用内置转换器）：

```bash
go install github.com/kubernetes-sigs/ingress2gateway@v1.1.0
```

### 方式二：Docker

```bash
docker build -t ingress2gateway-aws-mcp .
docker run --rm -i ingress2gateway-aws-mcp
```

## 连接到 AI 客户端

### Kiro

**Kiro CLI**：

```bash
kiro-cli mcp add --name ingress2gateway-aws-mcp \
  --scope global \
  --command python3 \
  --args "/path/to/ingress2gateway-aws-mcp/src/server.py"
```

**Kiro IDE**（工作区：`.kiro/settings/mcp.json`，全局：`~/.kiro/settings/mcp.json`）：

```json
{
  "mcpServers": {
    "ingress2gateway-aws-mcp": {
      "command": "python3",
      "args": ["/path/to/ingress2gateway-aws-mcp/src/server.py"]
    }
  }
}
```

Kiro IDE 支持热重载，修改 `mcp.json` 后无需重启，下次空闲时自动生效。

### Claude

**Claude Code CLI**：

```bash
claude mcp add ingress2gateway-aws-mcp python3 /path/to/ingress2gateway-aws-mcp/src/server.py
```

**Claude Desktop**（`~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "ingress2gateway-aws-mcp": {
      "command": "python3",
      "args": ["/path/to/ingress2gateway-aws-mcp/src/server.py"]
    }
  }
}
```

## EKS 集群前置要求

目标集群必须满足以下条件，生成的 YAML 才能成功 apply：

| 项目 | 要求 |
|------|------|
| AWS LBC 版本 | ≥ v2.14.0（L7）/ ≥ v2.13.3（L4）|
| LBC feature gate | `ALBGatewayAPI=true`（默认开启）|
| Gateway API standard channel | v1.5.0+（HTTPRoute / GRPCRoute / ReferenceGrant）|
| Gateway API experimental channel | 仅当有 TCP/UDP/TLSRoute 或 BackendTLSPolicy 时需要 |
| AWS 定制 CRD | `LoadBalancerConfiguration`、`TargetGroupConfiguration`、`ListenerRuleConfiguration` |
| ACM 证书 | 每个 TLS hostname 在同 Region 有对应证书 |
| Subnet 标签 | `kubernetes.io/role/elb: "1"`（公网）/ `kubernetes.io/role/internal-elb: "1"`（内网）|

快速搭建满足上述条件的集群，参见 [`infra/cluster.yaml`](infra/cluster.yaml)。

## 使用示例

```
你：帮我分析一下这个 Ingress 文件
<粘贴 ingress.yaml 内容>

AI 调用 analyze_ingress →
返回注解兼容性报告、路由类型分布、成本预估

你：转换成 Gateway API YAML
AI 调用 convert_to_gateway_api → 返回可直接 apply 的 YAML

你：生成完整迁移报告
AI 调用 generate_migration_report → 返回 Markdown 报告
```

示例输入文件见 [`examples/input/`](examples/input/)，对应转换结果见 [`examples/output/`](examples/output/)。

## 目录结构

详见 [`docs/architecture.md`](docs/architecture.md#3-模块职责)。

## 转换管道概览

```
Ingress YAML
    │
    ▼ converter.run()          ← ingress2gateway 二进制 / 内置 Python 转换器
    │
    ▼ gateway.inject()         ← 注入 aws-alb / aws-nlb GatewayClass
    ▼ gateway.fix_tls()        ← 移除 certificateRefs，改 hostname → ACM 自动发现
    ▼ gateway.split()          ← L4/L7 强制分离，重写 parentRefs
    ▼ gateway.merge()          ← 按策略合并 Gateway，控制 ALB/NLB 数量
    │
    ▼ crd.generate_for_resources()   ← LoadBalancerConfiguration（每个 Gateway）
    ▼ crd.generate_for_routes()      ← TargetGroupConfiguration（每个 Service / 健康检查 / stickiness）
    ▼ crd.generate_for_ingresses()   ← ListenerRuleConfiguration（OIDC / 源 IP）
    ▼ crd.attach_listener_rule_configs() ← 通过 HTTPRoute ExtensionRef 绑定认证 / 源 IP 条件
    │
    ▼ 可 kubectl apply 的 Gateway API YAML
```

## 文档

| 文档 | 内容 |
|------|------|
| [`docs/architecture.md`](docs/architecture.md) | 模块职责、转换管道数据流、AWS 资源模型、关键设计决策 |
| [`docs/annotation-reference.md`](docs/annotation-reference.md) | Nginx 注解兼容性速查表（auto / partial / warning / error 分类）|
| [`docs/deployment.md`](docs/deployment.md) | 部署手册（本地/Docker/Kiro+Claude 接入、EKS 前置条件）+ 故障排查 |

## 开发

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## License

MIT - see the [LICENSE](LICENSE) file for details.

## 免责声明

- 本项目仅供学习与技术参考，不构成生产部署方案。
- 使用本工具生成的 Gateway API YAML 在 apply 到集群前，请结合实际业务进行安全评估与调整。
- 部署过程中会在 AWS 上创建 EKS 集群、ALB/NLB 及相关资源并产生费用，请在实验结束后及时清理。
- 作者不对因使用本项目产生的任何费用或损失承担责任。
- 本项目与 Amazon Web Services 及 Kubernetes SIGs 无官方关联，相关服务的可用性与定价以各方官方文档为准。
