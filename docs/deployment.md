# 部署与运维手册

**项目**: ingress2gateway-aws-mcp  
**日期**: 2026-06-29

---

## 1. 部署模式选择

| 模式 | 适用场景 | 复杂度 |
|------|---------|--------|
| 本地 pip 安装 | 开发调试、个人使用 | 低 |
| Docker 容器 | 隔离环境、CI/CD 集成 | 中 |
| Kiro CLI MCP | 在终端直接与 Kiro 对话使用 | 低 |
| Kiro IDE | GUI IDE 对话式使用 | 低 |
| Claude Code MCP | 在终端直接与 Claude 对话使用 | 低 |
| Claude Desktop | GUI 桌面对话式使用 | 低 |

---

## 2. 前置条件

### 2.1 MCP Server 运行环境

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | ≥ 3.11 | `python3 --version` 确认（部分 Linux 环境没有 `python` 别名） |
| ingress2gateway（可选） | 1.1.0 | 缺失时自动降级 Python 转换器 |

### 2.2 安装 ingress2gateway Go 二进制（可选但推荐）

生产迁移建议安装，用官方转换器覆盖更多标准 Gateway API 转换场景。AWS LBC Gateway API 不支持的 Nginx 能力仍会由本工具报告为 warning/error，需要人工迁移。

```bash
# 方式 A：Go install（需 Go 1.21+）
go install github.com/kubernetes-sigs/ingress2gateway@v1.1.0

# 方式 B：直接下载预编译二进制（Linux amd64）
curl -L https://github.com/kubernetes-sigs/ingress2gateway/releases/download/v1.1.0/ingress2gateway_Linux_x86_64.tar.gz \
  | tar -xz -C /usr/local/bin ingress2gateway
chmod +x /usr/local/bin/ingress2gateway

# 验证
ingress2gateway --version
```

---

## 3. 本地安装

```bash
cd ingress2gateway-aws-mcp
pip install .                  # 生产
pip install -e ".[dev]"        # 开发（代码修改即时生效）
```

---

## 4. Docker 部署

```bash
# 构建
docker build -t ingress2gateway-aws-mcp:latest .

# 手动验证容器可用（MCP 使用 stdio，通常由 Claude 拉起）
echo '{}' | docker run --rm -i ingress2gateway-aws-mcp:latest

# 多架构构建
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ingress2gateway-aws-mcp:latest .
```

---

## 5. 连接到 AI 客户端

### 5.1 Kiro CLI

```bash
# 全局注册（所有项目可用）
kiro-cli mcp add --name ingress2gateway-aws-mcp \
  --scope global \
  --command python3 \
  --args "/absolute/path/to/ingress2gateway-aws-mcp/src/server.py"

# 仅当前工作区
kiro-cli mcp add --name ingress2gateway-aws-mcp \
  --scope workspace \
  --command python3 \
  --args "/absolute/path/to/ingress2gateway-aws-mcp/src/server.py"

# 使用 Docker 镜像
kiro-cli mcp add --name ingress2gateway-aws-mcp \
  --scope global \
  --command docker \
  --args "run --rm -i ingress2gateway-aws-mcp:latest"
```

### 5.2 Kiro IDE

手动编辑配置文件：

- 工作区级：`<project-root>/.kiro/settings/mcp.json`
- 全局级：`~/.kiro/settings/mcp.json`

```json
{
  "mcpServers": {
    "ingress2gateway-aws-mcp": {
      "command": "python3",
      "args": ["/absolute/path/to/ingress2gateway-aws-mcp/src/server.py"]
    }
  }
}
```

Kiro IDE 内置文件监听，修改 `mcp.json` 后无需重启，下次空闲边界自动生效。

### 5.3 Claude Code CLI

```bash
claude mcp add ingress2gateway-aws-mcp python3 /path/to/ingress2gateway-aws-mcp/src/server.py

# 使用 Docker 镜像
claude mcp add ingress2gateway-aws-mcp docker run --rm -i ingress2gateway-aws-mcp:latest

# 验证注册
claude mcp list
```

### 5.4 Claude Desktop

编辑配置文件（macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "ingress2gateway-aws-mcp": {
      "command": "python3",
      "args": ["/absolute/path/to/ingress2gateway-aws-mcp/src/server.py"]
    }
  }
}
```

Docker 方式将 `command` 改为 `"docker"`，`args` 改为 `["run", "--rm", "-i", "ingress2gateway-aws-mcp:latest"]`。修改后重启 Claude Desktop 生效。

### 5.5 验证连接

在对话框输入：`check_prerequisites 工具能用吗？`  
AI 客户端会调用 `check_prerequisites` 工具并返回环境检查结果。

---

## 6. 目标 EKS 集群前置条件

> 以下是生成的 YAML 能成功 `kubectl apply` 的必要条件，与 MCP Server 自身的运行环境无关。

### 6.1 AWS Load Balancer Controller

| 路由类型 | 最低 LBC 版本 |
|---------|------------|
| L7（HTTPRoute / GRPCRoute） | ≥ v2.14.0 |
| L4（TCPRoute / UDPRoute / TLSRoute） | ≥ v2.13.3 |

```bash
kubectl get deployment -n kube-system aws-load-balancer-controller \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

### 6.2 Gateway API CRD

```bash
# 必须：标准 channel
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.5.0/standard-install.yaml

# 包含 TCP/UDP/TLS passthrough / BackendTLSPolicy 时还需要 experimental channel
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.5.0/experimental-install.yaml

# 验证
kubectl get crd gateways.gateway.networking.k8s.io httproutes.gateway.networking.k8s.io
```

### 6.3 AWS 定制 CRD（随 LBC v2.14.0+ 自动安装）

```bash
kubectl get crd \
  loadbalancerconfigurations.gateway.k8s.aws \
  targetgroupconfigurations.gateway.k8s.aws \
  listenerruleconfigurations.gateway.k8s.aws
```

### 6.4 Subnet 标签

| 子网类型 | 必须有的标签 |
|---------|------------|
| 公网子网（internet-facing） | `kubernetes.io/role/elb: "1"` |
| 私网子网（internal） | `kubernetes.io/role/internal-elb: "1"` |

### 6.5 LBC IAM 权限（IRSA）

```bash
aws iam create-policy \
  --policy-name AWSLoadBalancerControllerIAMPolicy \
  --policy-document file://infra/lbc-iam-policy.json

eksctl create iamserviceaccount \
  --cluster=CLUSTER_NAME --namespace=kube-system \
  --name=aws-load-balancer-controller \
  --attach-policy-arn=arn:aws:iam::ACCOUNT_ID:policy/AWSLoadBalancerControllerIAMPolicy \
  --approve
```

### 6.6 ACM 证书

每个 TLS hostname 必须在同 Region 的 ACM 中有匹配证书（精确域名或通配符）。

```bash
aws acm list-certificates --region us-east-1 \
  --query 'CertificateSummaryList[*].{Domain:DomainName,ARN:CertificateArn}'
```

---

## 7. 快速搭建测试集群

```bash
eksctl create nodegroup -f infra/nodegroup.yaml
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system -f infra/lbc-values.yaml
kubectl apply -f infra/test-backends.yaml   # 可选
```

---

## 8. 升级与卸载

```bash
# 升级 MCP Server
cd ingress2gateway-aws-mcp && git pull && pip install .

# 升级 ingress2gateway 二进制
go install github.com/kubernetes-sigs/ingress2gateway@<new_version>

# 升级 Docker 镜像
docker build --no-cache --build-arg I2GW_VERSION=<new_version> -t ingress2gateway-aws-mcp:latest .

# 卸载
claude mcp remove ingress2gateway-aws-mcp
pip uninstall ingress2gateway-aws-mcp
docker rmi ingress2gateway-aws-mcp:latest
```

---

## 9. 故障排查

### 9.1 MCP Server 无法启动

```bash
python3 --version                 # 必须 ≥ 3.11
pip show mcp pyyaml packaging     # 缺失则 pip install .
python3 /path/to/src/server.py    # 手动启动查报错（正常时等待 stdin，Ctrl+C 退出）
claude mcp list                   # 确认注册条目存在
```

### 9.2 `ingress2gateway` 二进制未找到

`check_prerequisites` 返回 warning 时，工具自动降级到内置 Python 转换器，**不影响基本功能**。  
按 §2.2 安装二进制可获得更广的标准转换覆盖；这不会改变 AWS LBC 对 CORS、snippet、Lua、Basic Auth、复杂 header/cookie rewrite 等能力的限制。

### 9.3 `kubectl apply` 失败

| 错误信息 | 原因 | 解决 |
|---------|------|------|
| `no matches for kind "HTTPRoute"` | Gateway API CRD 未安装 | 见 §6.2 安装 standard channel |
| `no matches for kind "TCPRoute"` | experimental channel 未安装 | 见 §6.2 安装 experimental channel |
| `no matches for kind "LoadBalancerConfiguration"` | LBC 版本过低 | 升级 LBC ≥ v2.14.0 |

**certificateRefs 被误用**：AWS LBC 不支持此字段，删除并改用 listener `hostname`：

```yaml
listeners:
  - name: https
    port: 443
    protocol: HTTPS
    hostname: app.example.com    # ACM 按此 hostname 自动发现证书
    tls:
      mode: Terminate
```

**L4/L7 混挂同一 Gateway**：TCPRoute 和 HTTPRoute 不能共享 Gateway，使用 `validate_output` 工具检查，或重新运行 `convert_to_gateway_api`（工具会自动拆分）。

### 9.4 ALB/NLB Target 全部 Unhealthy（503）

Gateway 和 Route 状态正常但返回 503，按顺序检查：

1. **健康检查路径**：ALB 默认路径为 `/`，若应用就绪端点不同，转换时传 `health_check_path`，或在 `TargetGroupConfiguration` 中修改 `defaultConfiguration.healthCheckConfig.healthCheckPath`
2. **Service 端口**：`kubectl get svc <svc> -n <ns>` 确认端口匹配 HTTPRoute `backendRefs`
3. **安全组**：确认 ALB 安全组允许对 Pod 发起健康检查流量

### 9.5 ReferenceGrant 缺失（跨 Namespace 路由 `RefNotPermitted`）

在 **Service 所在 namespace** 创建：

```yaml
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-from-frontend
  namespace: backend-ns
spec:
  from:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      namespace: frontend-ns
  to:
    - group: ""
      kind: Service
```

### 9.6 ACM 证书未匹配

```bash
aws acm list-certificates --region <region> \
  --query 'CertificateSummaryList[*].{Domain:DomainName,Status:Status}'
```

注意：ACM 证书必须与 EKS 集群在**同一 Region**；`*.example.com` 只覆盖一层子域名。

### 9.7 DNS 访问问题（L4/L7 拆分后双端点）

L4/L7 拆分后原 Ingress 流量分到 ALB（L7）和 NLB（L4）两个独立 DNS 端点：

```bash
kubectl describe gateway <alb-gateway> -n <ns>   # Status -> Addresses -> ALB DNS 名
kubectl describe gateway <nlb-gateway> -n <ns>   # Status -> Addresses -> NLB DNS 名
```

| 场景 | DNS 记录目标 |
|------|------------|
| HTTP/HTTPS 服务 | ALB DNS 名（CNAME 或 Route53 Alias）|
| TCP/UDP / TLS passthrough | NLB DNS 名 |

### 9.8 `validate_output` 报错速查

| 错误 | 解决 |
|------|------|
| L4/L7 listener 混挂同一 Gateway | 分开到 ALB / NLB Gateway |
| `certificateRefs` 存在 | 改为 listener hostname |
| Route parentRefs 指向不存在的 Gateway | 检查 Gateway name / namespace |
| 使用了 TCPRoute 等 experimental 资源 | 安装 experimental channel CRD |
| 跨 namespace 引用缺少 ReferenceGrant | 在目标 namespace 创建 ReferenceGrant |

---

## 10. 收集诊断信息

```bash
python3 -c "import importlib.metadata; print(importlib.metadata.version('ingress2gateway-aws-mcp'))"
python3 --version
ingress2gateway --version 2>/dev/null || echo "not installed"
kubectl get crd gateways.gateway.networking.k8s.io \
  -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/bundle-version}'
kubectl get deployment -n kube-system aws-load-balancer-controller \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
kubectl describe gateway -n <ns>
kubectl describe httproute -n <ns>
```
