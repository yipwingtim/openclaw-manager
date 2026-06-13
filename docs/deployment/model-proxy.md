# Model Proxy Deployment

本文档说明如何启用轻量模型代理，避免真实上游模型服务 URL 和 API Key 写入用户实例配置。

## 目标

启用后，用户实例中的模型 Provider 只保存：

- `MODEL_PROXY_PUBLIC_BASE_URL`
- 实例级 model proxy token

真实上游地址和 API Key 只保存在管理端环境变量中：

- `MODEL_PROXY_UPSTREAM_BASE_URL`
- `MODEL_PROXY_UPSTREAM_API_KEY`

## 请求链路

```text
用户实例
  -> openclaw-model-proxy:8081/v1
  -> 上游模型服务
```

`model-proxy` 会校验实例级 token，通过后将请求转发给上游模型服务，并替换为真实上游 API Key。

## 配置

在 `config/openclaw-manager.env` 中配置：

```env
MODEL_PROXY_PUBLIC_BASE_URL=http://openclaw-model-proxy:8081/v1
MODEL_PROXY_TOKEN_DIR=/data/docker/openclaw-public/model-proxy-tokens
MODEL_PROXY_UPSTREAM_BASE_URL=http://127.0.0.1:18080/v1
MODEL_PROXY_UPSTREAM_API_KEY=replace-with-your-upstream-api-key
```

启用内置 `model-proxy` 时，用户实例中的模型 Provider 应使用：

```text
baseUrl = MODEL_PROXY_PUBLIC_BASE_URL
apiKey  = /data/docker/openclaw-public/model-proxy-tokens/<user_id>.token 中的实例级 token
```

`scripts/set_model_provider.sh` 和 `scripts/batch_set_model_provider.sh` 会自动写入上述地址和 token，不需要手工编辑用户实例的 `openclaw.json`。

在 `config/model-providers.env` 中保留模型展示信息：

```env
MODEL_PROVIDER_ID=gpustack
MODEL_ID=gpustack/minimax-m2.1
MODEL_ALIAS=MiniMax M2.1
MODEL_BASE_URL=http://openclaw-model-proxy:8081/v1
```

`MODEL_BASE_URL` 保留用于兼容旧脚本输入；优先使用 `MODEL_PROXY_PUBLIC_BASE_URL`。

## 可替换网关

内置 `model-proxy` 是轻量代理组件，不是强绑定必选组件。未来如果建设独立 API 网关，可将：

```env
MODEL_PROXY_PUBLIC_BASE_URL=https://gateway.example.com/v1
```

设置为正式 API 网关地址，并让脚本继续为实例写入该地址和实例级 token。

此时可以不启动内置 `openclaw-model-proxy` 容器，但外部 API 网关需要自行实现：

- 实例 token 鉴权
- 上游模型服务转发
- 真实上游 URL/API Key 隐藏
- 令牌吊销、限流、审计等后续能力

## 启动服务

```bash
cd /data/docker/openclaw-manager/services
docker compose up -d --build model-proxy
```

如果自定义了 `MODEL_PROXY_TOKEN_DIR`，需要确保 Docker Compose 渲染 volume 时也能读取该变量。可在执行前导入环境变量：

```bash
set -a
. /data/docker/openclaw-manager/config/openclaw-manager.env
set +a
```

## 更新实例模型配置

单个实例：

```bash
./scripts/set_model_provider.sh <user_id>
```

批量实例：

```bash
./scripts/batch_set_model_provider.sh <input.csv> <output.csv>
```

脚本会为每个实例生成或复用：

```text
/data/docker/openclaw-public/model-proxy-tokens/<user_id>.token
```

并写入该实例允许使用的模型白名单：

```text
/data/docker/openclaw-public/model-proxy-tokens/<user_id>.models
```

然后把代理地址和实例级 token 写入该用户实例的模型 Provider 配置。

`model-proxy` 会根据实例 token 找到对应用户，并执行模型白名单校验：

- `GET /v1/models` 只返回 `<user_id>.models` 中允许的模型。
- `POST /v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/responses` 只允许请求白名单中的 `model`。
- 没有白名单文件或请求非白名单模型时返回 `403`。

默认情况下，`set_model_provider.sh` 会把当前 `MODEL_ID` 的短模型 ID 写入 `<user_id>.models`。例如：

```env
MODEL_ID=gpustack/qwen3.6-27b-fp8
```

对应白名单内容为：

```text
qwen3.6-27b-fp8
```

如果需要给某个同事实例开放多个模型，可手工编辑该用户的 `.models` 文件，每行一个模型 ID。

## 安全注意事项

- 不要再把真实上游 API Key 写入用户实例的 `openclaw.json`。
- 用户实例应只能访问 `model-proxy`，不应直接访问上游模型服务。
- 上游模型服务应尽量只允许 `model-proxy` 所在网络或主机访问。
- 如果上游模型服务本身没有鉴权，必须通过网络层阻止用户实例直连上游；否则 model-proxy 只能隐藏配置，不能阻止用户绕过代理直接调用上游。
- 如果上游 API Key 有多个模型权限，应使用每实例 `.models` 白名单限制实际可见和可调用的模型。
- 当前版本是轻量透明代理，不包含配额、限流和审计页面。

## 验证

1. 查看实例配置，确认 `baseUrl` 是 `openclaw-model-proxy`，`apiKey` 是 `ocm_` 开头的实例 token。
2. 从用户实例访问模型，确认模型调用正常。
3. 确认真实上游 API Key 不存在于用户实例配置文件中。
4. 使用实例 token 请求 `/v1/models`，确认只返回该实例 `.models` 文件允许的模型。
