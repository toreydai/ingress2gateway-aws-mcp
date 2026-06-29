# Nginx Ingress 注解兼容性速查表

**适用版本**: AWS LBC >= v2.14.0 + Gateway API v1.5.0  
**日期**: 2026-06-29

本表按当前项目的实际转换管道编写：`ingress2gateway` v1.1.0 可选，内置 Python 转换器兜底，随后执行 AWS 后处理层。若外部 `ingress2gateway` 生成了额外的标准 Gateway API 资源，仍需以 AWS LBC 的 Gateway API 支持范围和 `validate_output` 结果为准。

## 图例

| 符号 | 含义 |
|------|------|
| ✅ 自动 | 当前转换管道会生成对应 Gateway API 资源 |
| ⚡ AWS 后处理 | 当前 AWS 后处理层会生成或修正 AWS LBC CRD / Gateway 字段 |
| ⚠️ 半自动 | 会生成骨架或部分规则，但必须人工补齐或确认语义 |
| ⚠️ 警告 | 不会可靠自动转换；迁移报告会提示人工改造 |
| ❌ 不兼容 | ALB/NLB/Gateway API 没有等价能力；工具生成 ERROR |

## 1. 路由与重写

| Nginx 注解 / 能力 | Gateway API / AWS 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `nginx.ingress.kubernetes.io/rewrite-target` | `HTTPRoute` `URLRewrite` | ✅ 自动 | 仅支持不含 `$1`/`$2` 捕获组的简单前缀替换 |
| 重写含捕获组 `$1`, `$2` | 无直接等价 | ⚠️ 警告 | Gateway API `URLRewrite` 不能复用 regex capture；需改路由设计、应用层处理或 Lambda/CloudFront |
| `nginx.ingress.kubernetes.io/app-root` | `HTTPRoute` `RequestRedirect` | ✅ 自动 | 生成 `/` 到 app-root 的 302 redirect |
| `nginx.ingress.kubernetes.io/use-regex` | 无可靠自动映射 | ⚠️ 警告 | 当前 fallback 转换器会清理 regex path，不生成 `PathRegularExpression` |
| `nginx.ingress.kubernetes.io/permanent-redirect` | `HTTPRoute` `RequestRedirect` | ✅ 自动 | 保留 scheme / hostname / path，默认 301 |
| `nginx.ingress.kubernetes.io/temporal-redirect` | `HTTPRoute` `RequestRedirect` | ✅ 自动 | 保留 scheme / hostname / path，默认 302 |
| `nginx.ingress.kubernetes.io/permanent-redirect-code` | `RequestRedirect.statusCode` | ✅ 自动 | 与 `permanent-redirect` 搭配使用 |

## 2. TLS / HTTPS

| Nginx 注解 / 能力 | Gateway API / AWS 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `tls.secretName`（Ingress spec） | listener `hostname` + ACM 证书发现 | ⚡ AWS 后处理 | AWS LBC 不使用 Gateway listener `certificateRefs`；工具会移除并保留 hostname |
| `nginx.ingress.kubernetes.io/ssl-redirect` | 独立 `HTTPRoute` + `RequestRedirect` | ✅ 自动 | 绑定到 `http` listener；当前生成 HTTPS 302 |
| `nginx.ingress.kubernetes.io/force-ssl-redirect` | 同上 | ✅ 自动 | 需要 Ingress TLS host 才能绑定 HTTPS listener |
| `nginx.ingress.kubernetes.io/ssl-passthrough` | `TLSRoute` passthrough + NLB Gateway | ✅ 自动 | 需要 Gateway API experimental channel |
| `nginx.ingress.kubernetes.io/backend-protocol: HTTPS` | `HTTPRoute` + `BackendTLSPolicy` | ✅ 自动 | 使用 `wellKnownCACertificates: System`；私有 CA 需手动改为 `caCertificateRefs` |
| `nginx.ingress.kubernetes.io/backend-protocol: GRPCS` | `GRPCRoute` + `BackendTLSPolicy` | ✅ 自动 | 需要 Gateway API experimental channel |
| `nginx.ingress.kubernetes.io/proxy-ssl-verify-depth` | 无等价字段 | ⚠️ 警告 | `BackendTLSPolicy` 不支持证书链深度控制 |
| `nginx.ingress.kubernetes.io/proxy-ssl-protocols` | 无等价字段 | ⚠️ 警告 | 后端 TLS protocol versions 不由该策略配置 |
| `nginx.ingress.kubernetes.io/secure-verify-ca-secret` | `BackendTLSPolicy.validation.caCertificateRefs` | ⚠️ 警告 | 当前不自动读取 Secret；需手动建 CA 引用 |

## 3. 后端协议

| Nginx 注解 | Gateway API 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `backend-protocol: HTTP` | `HTTPRoute` | ✅ 自动 | 默认行为 |
| `backend-protocol: HTTPS` | `HTTPRoute` + `BackendTLSPolicy` | ✅ 自动 | 见 TLS 章节 |
| `backend-protocol: GRPC` | `GRPCRoute` | ✅ 自动 | 需要 LBC >= v2.14.0 |
| `backend-protocol: GRPCS` | `GRPCRoute` + `BackendTLSPolicy` | ✅ 自动 | 需要 experimental channel |
| `nginx.ingress.kubernetes.io/grpc-backend` | 使用 `backend-protocol: GRPC` 替代 | ⚠️ 警告 | 旧注解不自动转换 |

## 4. 请求头 / 响应头 / CORS

| Nginx 注解 | Gateway API / AWS 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `nginx.ingress.kubernetes.io/proxy-set-headers` | 可手写 `RequestHeaderModifier` | ⚠️ 警告 | 当前不读取 ConfigMap；请显式建 HTTPRoute filter 或放到应用层 |
| `nginx.ingress.kubernetes.io/upstream-vhost` | `HTTPRoute` `RequestHeaderModifier` | ✅ 自动 | 设置 upstream Host header；上线前确认后端虚拟主机行为 |
| `nginx.ingress.kubernetes.io/hide-headers` | 无 AWS LBC Gateway API 等价 | ⚠️ 警告 | 响应头删除建议放应用、CloudFront 或代理层 |
| `nginx.ingress.kubernetes.io/cors-*` | 无 AWS LBC Gateway API 等价 | ⚠️ 警告 | AWS LBC Gateway API 不支持 `ResponseHeaderModifier`；用应用层、CloudFront 或 WAF 方案 |
| `nginx.ingress.kubernetes.io/proxy-redirect-from/to` | 无等价 | ⚠️ 警告 | 响应 Location rewrite 需应用层或边缘层处理 |
| `nginx.ingress.kubernetes.io/proxy-cookie-domain/path` | 无等价 | ⚠️ 警告 | Cookie rewrite 需应用层或边缘层处理 |

## 5. 金丝雀 / 流量分配

| Nginx 注解 | Gateway API 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `nginx.ingress.kubernetes.io/canary` + `canary-weight` | `HTTPRoute.backendRefs[].weight` | ✅ 自动 | 按 host + path 关联主路由，避免污染其他路径 |
| `nginx.ingress.kubernetes.io/canary-by-header` | `HTTPRoute.matches[].headers` | ✅ 自动 | 使用 exact match |
| `nginx.ingress.kubernetes.io/canary-by-header-value` | 同上 | ✅ 自动 | 默认值为 `always` |
| `nginx.ingress.kubernetes.io/canary-by-header-pattern` | 无 regex header match 等价 | ⚠️ 警告 | 改为 exact/prefix 设计 |
| `nginx.ingress.kubernetes.io/canary-by-cookie` | 无自动转换 | ⚠️ 警告 | 建议改为 header-based canary 或应用层分流 |

## 6. 认证 / 授权

| Nginx 注解 | Gateway API / AWS 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `nginx.ingress.kubernetes.io/auth-url` | `ListenerRuleConfiguration` OIDC action | ⚠️ 半自动 | 工具生成并挂载 ExtensionRef，但 issuer/client Secret/endpoints 必须人工补齐 |
| `nginx.ingress.kubernetes.io/auth-signin` | OIDC authorization endpoint hint | ⚠️ 半自动 | 仅在同时存在 `auth-url` 时参与骨架生成 |
| `nginx.ingress.kubernetes.io/auth-method` | 无完整等价 | ⚠️ 半自动 | 只作为报告分类；OIDC 细节需人工配置 |
| `nginx.ingress.kubernetes.io/auth-response-headers` | 无完整等价 | ⚠️ 半自动 | 需要结合认证服务和应用确认 |
| `nginx.ingress.kubernetes.io/auth-tls-secret` | 可用 LBC 认证/证书能力重建 | ⚠️ 半自动 | 当前不自动读取证书 Secret |
| `nginx.ingress.kubernetes.io/auth-type: basic` | 无 ALB 原生 Basic Auth | ⚠️ 警告 | 改 OIDC/Cognito、应用认证或 Lambda/API Gateway |
| `nginx.ingress.kubernetes.io/auth-secret` / `auth-realm` | 无等价 | ⚠️ 警告 | Basic Auth 配套项不转换 |

## 7. IP 访问控制

| Nginx 注解 | Gateway API / AWS 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `nginx.ingress.kubernetes.io/whitelist-source-range` | `ListenerRuleConfiguration` source-ip condition | ⚠️ 半自动 | 工具生成并挂载；上线前确认未匹配请求的默认行为符合预期 |
| `nginx.ingress.kubernetes.io/denylist-source-range` | `ListenerRuleConfiguration` source-ip condition + fixed 403 | ⚠️ 半自动 | 工具生成并挂载；需确认与其他 rule 的优先级和匹配范围 |

## 8. 限流

| Nginx 注解 | Gateway API / AWS 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `nginx.ingress.kubernetes.io/limit-rps` | AWS WAF Rate-based Rule | ⚠️ 警告 | ALB 本身无等价限流 |
| `nginx.ingress.kubernetes.io/limit-rpm` | AWS WAF Rate-based Rule | ⚠️ 警告 | 同上 |
| `nginx.ingress.kubernetes.io/limit-connections` | AWS WAF / 应用层 | ⚠️ 警告 | 无连接级等价 |
| `nginx.ingress.kubernetes.io/limit-burst-multiplier` | AWS WAF / 应用层 | ⚠️ 警告 | 无直接等价 |

## 9. 超时 / 请求大小 / 重试

| Nginx 注解 | Gateway API / AWS 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `nginx.ingress.kubernetes.io/proxy-read-timeout` | `LoadBalancerConfiguration.loadBalancerAttributes` | ⚡ AWS 后处理 | 映射为 `idle_timeout.timeout_seconds` |
| `nginx.ingress.kubernetes.io/proxy-send-timeout` | 同上 | ⚡ AWS 后处理 | 仅在未设置 read timeout 时映射为 idle timeout |
| `nginx.ingress.kubernetes.io/proxy-connect-timeout` | 无严格等价 | ⚠️ 警告 | connect timeout 与 ALB idle timeout 语义不同 |
| `nginx.ingress.kubernetes.io/proxy-body-size` | 无 per-Ingress 等价 | ⚠️ 警告 | ALB 请求大小限制不可按 Ingress 配置 |
| `nginx.ingress.kubernetes.io/proxy-next-upstream` | 无 Nginx retry 语义等价 | ⚠️ 警告 | 需按 ALB 健康检查、客户端重试或应用重试重新设计 |

## 10. 会话保持

| Nginx 注解 | Gateway API / AWS 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `nginx.ingress.kubernetes.io/session-cookie-name` | `TargetGroupConfiguration` stickiness | ⚠️ 半自动 | 会启用 ALB `lb_cookie` stickiness，但不保留 Nginx cookie 名 |
| `nginx.ingress.kubernetes.io/session-cookie-expires` | `stickiness.lb_cookie.duration_seconds` | ⚠️ 半自动 | 仅在设置 `session-cookie-name` 时生效 |
| `nginx.ingress.kubernetes.io/session-cookie-max-age` | `stickiness.lb_cookie.duration_seconds` | ⚠️ 半自动 | 优先于 `session-cookie-expires` |
| `nginx.ingress.kubernetes.io/session-cookie-path` | 无等价 | ⚠️ 警告 | ALB `lb_cookie` 不支持该 cookie path 语义 |
| `session-cookie-samesite` / `conditional-samesite-none` | 无等价 | ⚠️ 警告 | 需应用层处理 |

## 11. 流量镜像

| Nginx 注解 | Gateway API 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `nginx.ingress.kubernetes.io/mirror` | `HTTPRoute` `RequestMirror` | ✅ 自动 | 当前按 service 名生成 mirror backend |
| `nginx.ingress.kubernetes.io/mirror-target` | `HTTPRoute` `RequestMirror.backendRef` | ✅ 自动 | 支持 `svc:port` 或 `svc.namespace:port` 形式 |

## 12. 负载均衡器配置

| 来源 | Gateway API / AWS 对应 | 状态 | 备注 |
|------|-----------------|------|------|
| `ingressClassName: nginx` / `kubernetes.io/ingress.class: nginx` | GatewayClass `aws-alb` / `aws-nlb` | ⚡ AWS 后处理 | 工具按 listener protocol 注入 GatewayClass |
| `convert_to_gateway_api(..., scheme=...)` | `LoadBalancerConfiguration.spec.scheme` | ⚡ AWS 后处理 | 当前通过工具参数设置 `internet-facing` / `internal` |
| `alb.ingress.kubernetes.io/scheme` | `LoadBalancerConfiguration.spec.scheme` | ⚠️ 警告 | 当前不从原 Ingress 注解读取；请用工具参数或手工改 YAML |
| `alb.ingress.kubernetes.io/subnets` | `LoadBalancerConfiguration.spec.subnets` | ⚠️ 警告 | 生成器支持字段，但转换管道当前不自动从注解填充 |
| `alb.ingress.kubernetes.io/security-groups` | `LoadBalancerConfiguration.spec.securityGroups` | ⚠️ 警告 | 当前不自动从注解填充 |
| `alb.ingress.kubernetes.io/tags` | `LoadBalancerConfiguration.spec.tags` | ⚠️ 警告 | 当前不自动从注解填充 |

## 13. 健康检查

> 健康检查是迁移后最易出问题的环节。若不显式配置，ALB 默认健康路径为 `/`，后端若不提供 `/` 可能导致 target unhealthy。

| 来源 | `TargetGroupConfiguration` 字段 | 状态 | 备注 |
|------|-------------------------------|------|------|
| 每个 Service backend | `targetReference.name` | ⚡ AWS 后处理 | 工具为 HTTPRoute/GRPCRoute 后端生成 TGC |
| 默认健康路径 | `defaultConfiguration.healthCheckConfig.healthCheckPath` | ⚠️ 半自动 | 默认 `/`；可通过 `health_check_path` 工具参数覆盖 |
| `backend-protocol` | `healthCheckProtocol` | ⚡ AWS 后处理 | HTTP/HTTPS/gRPC 自动映射到 LBC 可接受协议 |
| 健康检查间隔/阈值 | `healthCheckInterval` / threshold 字段 | ⚠️ 半自动 | 可通过 `health_check_interval`、`healthy_threshold`、`unhealthy_threshold` 工具参数覆盖 |

## 14. 默认后端 / 自定义错误页

| Nginx 能力 | Gateway API / AWS 对应 | 状态 | 备注 |
|-----------|-----------------|------|------|
| `spec.defaultBackend` | catch-all `HTTPRoute` backend | ✅ 自动 | 生成无 match 的兜底 route，指向原 default backend Service |
| `nginx.ingress.kubernetes.io/default-backend` | 无自动转换 | ⚠️ 警告 | 注解形式当前不读取；请手工建兜底 route 或 fixed-response |
| `nginx.ingress.kubernetes.io/custom-http-errors` | 可手写 fixed-response，但不能代理错误页服务 | ⚠️ 警告 | ALB fixed-response 只能返回静态内容；复杂错误页建议应用层、CloudFront 或 Lambda |

## 15. Nginx 原生扩展（不兼容）

| Nginx 注解 | 状态 | 原因 |
|-----------|------|------|
| `nginx.ingress.kubernetes.io/configuration-snippet` | ❌ 不兼容 | Nginx 原生配置块，ALB/NLB 无对应能力 |
| `nginx.ingress.kubernetes.io/server-snippet` | ❌ 不兼容 | 同上 |
| `nginx.ingress.kubernetes.io/main-snippet` | ❌ 不兼容 | 同上 |
| `nginx.ingress.kubernetes.io/stream-snippet` | ❌ 不兼容 | Nginx stream 配置块无等价 |
| `nginx.ingress.kubernetes.io/lua-resty-*` | ❌ 不兼容 | Lua 扩展无法迁移到 ALB/NLB |
| `nginx.ingress.kubernetes.io/fastcgi-params-configmap` | ❌ 不兼容 | ALB 不支持 FastCGI |

## 16. L4 路由（来自 ConfigMap）

> TCP/UDP 路由不在 Ingress 内，而在 `tcp-services` / `udp-services` ConfigMap。

| 来源 | Gateway API 对应 | 承载 LB | 状态 |
|------|-----------------|--------|------|
| `tcp-services` ConfigMap | `TCPRoute` + NLB Gateway | NLB | ✅ 自动 |
| `udp-services` ConfigMap | `UDPRoute` + NLB Gateway | NLB | ✅ 自动 |
| `ssl-passthrough` Ingress | `TLSRoute` passthrough + NLB Gateway | NLB | ✅ 自动 |

## 注意事项

1. **L4/L7 分离**：TCPRoute/UDPRoute/TLSRoute 与 HTTPRoute/GRPCRoute 不能混挂同一 Gateway。迁移后原 Ingress 的流量可能落到 ALB + NLB 两个不同 DNS 端点。

2. **experimental channel**：TCPRoute、UDPRoute、TLSRoute 和 BackendTLSPolicy 需要 Gateway API experimental channel CRD。

3. **Gateway 合并**：工具默认按 `(GatewayClass, scheme, namespace)` 合并 Gateway，避免 1:1 映射导致 ALB/NLB 数量和成本失控。

4. **报告优先级**：`ERROR` 表示不能按原语义迁移；`WARNING` 表示必须人工设计替代方案；`INFO` 通常表示已生成骨架但仍需补业务参数。

## 不支持能力的替代方案

| 不支持 / 不兼容能力 | 推荐替代方案 | 说明 |
|-------------------|-------------|------|
| CORS 响应头（`cors-*`） | 应用层；CloudFront Response Headers Policy | AWS LBC Gateway API 不支持响应头修改；不要在 ALB/Gateway 层伪装支持 |
| 隐藏响应头（`hide-headers`） | 应用层；CloudFront；独立 Envoy/Nginx 代理 | 需要响应处理能力，ALB listener rule 不提供该语义 |
| Cookie rewrite（`proxy-cookie-domain/path`） | 应用层；CloudFront Functions；Lambda@Edge | ALB 不改写 `Set-Cookie` |
| 响应 `Location` rewrite（`proxy-redirect-from/to`） | 应用层；CloudFront Functions；Lambda@Edge | ALB/Gateway API 不支持响应头内容 rewrite |
| HTTP Basic Auth | OIDC/Cognito；应用认证；API Gateway/Lambda authorizer | ALB 原生支持 OIDC/Cognito 类认证，不支持 Nginx Basic Auth secret/realm 语义 |
| 外部复杂 auth | OIDC/Cognito 标准化；应用层鉴权；API Gateway | `auth-url` 可生成 OIDC 骨架，但 Nginx subrequest 风格外部 auth 不能原样迁移 |
| 限流（`limit-rps` / `limit-rpm` / 连接数） | AWS WAF Rate-based Rule；应用层限流 | ALB 本身不提供 Nginx per-location 限流 |
| `proxy-body-size` | 应用层请求大小限制；AWS WAF body inspection | ALB 没有 per-Ingress body-size 配置 |
| regex capture rewrite（`$1`, `$2`） | 改路由设计；应用层路由；CloudFront Function | Gateway API `URLRewrite` 不能引用正则捕获组 |
| `use-regex` 复杂路径 | 拆成明确 path rule；应用层路由 | 避免把 Nginx regex location 语义直接搬到 ALB |
| `configuration-snippet` / `server-snippet` / `main-snippet` | 拆分为 Gateway API、AWS WAF、应用配置或独立代理 | snippet 是任意 Nginx 配置块，没有通用自动转换路径 |
| Lua WAF / Lua rate limit | AWS WAF；应用层；独立代理 | ALB/NLB 不运行 Lua 扩展 |
| FastCGI | 改造成 HTTP 服务后接 ALB；或保留 Nginx/Envoy 代理 | ALB 不支持 FastCGI 协议 |
| `stream-snippet` 复杂 L4 逻辑 | 简单端口用 NLB + TCPRoute/UDPRoute；复杂逻辑保留代理层 | TCPRoute/UDPRoute 只覆盖标准 L4 转发，不覆盖任意 stream 配置 |
| 后端 TLS 深度/协议版本控制 | 应用/后端 TLS 配置；私有 CA 用 `BackendTLSPolicy.caCertificateRefs` | `BackendTLSPolicy` 能表达 CA/hostname 校验，但不表达 Nginx 的 verify depth/protocol knobs |
| 自定义错误页代理服务 | 应用层错误页；CloudFront custom error response；Lambda | ALB fixed-response 只能返回静态内容，不能像 Nginx 一样转发到错误页后端 |
