# Docker 控制台

Docker 运行 Web 控制台，支持已有配置的云端 DNS/SaaS、优选入口操作及容器网络可达的后端检查，**不是完整路由/宿主服务管理部署**。已有 Tunnel 和外部 `cloudflared` connector 仍由管理员单独部署、配置及维护；镜像不含 connector，不接管宿主 systemd、Docker socket 或宿主 PID。

## 最短启动（空配置）

在仓库根目录执行，需 Docker Engine 与 Compose 插件：

```sh
mkdir -p config
touch config/env
docker compose up -d --build
docker compose logs cf-tunnel-accel
curl -fsS http://127.0.0.1:8642/api/health
```

浏览器访问 `http://127.0.0.1:8642/`。首次成功绑定并启动时，日志以 `首次登录密钥: ...` 打印随机生成的 48 位十六进制 UI 密钥一次；此处省略号不是实际密钥。重启复用，不再打印。日志含管理凭据，应限制访问，不要贴入 issue。空配置能登录并查看环境缺失项，**不表示云端操作已就绪**。匿名 `/api/health` 只是 HTTP liveness，不证明 Tunnel、后端或 Cloudflare 健康。

Compose 默认：

- 端口仅发布到 `127.0.0.1:${CFA_DOCKER_PORT:-8642}`，容器监听 8642。改宿主端口可用 `CFA_DOCKER_PORT=8643 docker compose up -d`。
- 配置来自 `${CFA_DOCKER_CONFIG_DIR:-./config}`，只读绑定到 `/config`。指定其它目录时，先创建目录与 `env`；配置中的路径必须是**容器内绝对路径**，不展开 shell 变量。
- 状态使用 Compose named volume `state` 挂载到 `/data`，**不是宿主 `./data` 绑定目录**。新卷自动具备 UID/GID `1000:1000` 所需权限，无需手工 chown；已有卷须保留正确权限。`/data/ui-token`、任务、审计与缓存可跨容器重建保留。`docker compose down` 保留卷，`down -v` 会删除状态及托管密钥，执行前备份。
- 应用以非 root UID/GID `1000:1000` 运行，根文件系统只读，`/tmp` 为 tmpfs，`cap_drop: ALL`，启用 `no-new-privileges`；默认 bridge 网络，无宿主运行时挂载。

## 挂载 Cloudflare API Token 与已有 Tunnel 配置

先在受保护的本机终端将管理员准备的 API Token 文件复制到 `config/cloudflare-token`，将已有 Tunnel YAML 复制到 `config/tunnel.yml`。目录须允许 UID 1000 遍历，文件须允许 UID 1000 读取；不要为方便读取而对所有用户开放凭据。只挂载控制台需要的文件，connector 的 Tunnel 凭据 JSON 仍由 connector 单独保管，控制台云端操作不需要把它放入镜像。

以下为 `config/env` 的配置结构；示例域名与 UUID 均须替换为自己的已有资源，不包含实际凭据：

```ini
CF_TUNNEL_ACCEL_TOKEN_FILE=/config/cloudflare-token
CF_TUNNEL_ACCEL_CONFIG=/config/tunnel.yml
CF_TUNNEL_ACCEL_TUNNEL_ID=11111111-1111-4111-8111-111111111111
CF_TUNNEL_ACCEL_AUX_ZONE=example.net
CF_TUNNEL_ACCEL_PREFERRED_ENTRY=cdn.example.net
CF_TUNNEL_ACCEL_FALLBACK_ORIGIN=origin.example.net
CF_TUNNEL_ACCEL_CDN_CANDIDATES=edge.example.net,edge-two.example.net
CF_TUNNEL_ACCEL_PROBE_HOSTNAME=app.example.net
```

API Token 的 zone 资源范围/权限、SaaS 启用和 origin 归属见[部署说明](DEPLOYMENT.md)。`CF_TUNNEL_ACCEL_TOKEN_FILE` 是 **Cloudflare API Token** 路径，不是 UI 登录密钥来源。不要在 `config/env` 中填写 UI 登录密钥；UI 密钥只从 UI 进程环境或显式外部文件读取。

后端 URL 由容器视角解释：同一 Docker 网络上的后端可使用服务名，如 `http://backend:8080`；须由管理员将两个服务接入同一网络。`localhost` / `127.0.0.1` 指控制台容器自身，既不是宿主也不是另一个 connector。只读 ingress 的后端地址还须适合外部 connector 所在网络；控制台探测可达不等于 connector 可达，反之亦然。容器不自动改写路由或配置宿主网关。

修改绑定文件后可运行 `docker compose restart cf-tunnel-accel` 重新读取；改变 Compose 环境变量、绑定目录或端口需 `docker compose up -d` 重建。不要把更新只读快照误认为已同步外部 connector。

### 多 profile

把注册表和各 profile env 放到 `config/`，其中引用 `/config/...` 的容器路径，按[注册表格式](DEPLOYMENT.md#多-profile-注册表)配置，然后启动：

```sh
CF_TUNNEL_ACCEL_UI_PROFILES=/config/profiles.json docker compose up -d
```

未设置或空字符串的 profiles 透传值按未提供处理，不会将空值当文件路径。未显式指定时仍可自动读取 UI 状态目录中的 `profiles.json`；不需要的旧注册表应先核对。各 profile 缓存/锁使用独立的 `/data/...` 路径；profile 不隔离共享的云端 DNS。

## 自定义 UI 密钥

完整规则见 [UI 登录密钥](../ui/README.md#登录密钥)。三种来源：

1. 默认托管 `/data/ui-token`，缺失时生成 48 位十六进制随机密钥，原子保存为 0600。
2. UI 进程环境 `CF_TUNNEL_ACCEL_UI_TOKEN`：32–1024 个单行可打印字符，首尾无空白；从受保护环境注入，不把值写入文档、命令历史或 Compose 文件。
3. 外部文件：将已准备的 UI 密钥文件放在 `config/ui-login-key`，确保 UID 1000 可读，然后传入路径：

```sh
CF_TUNNEL_ACCEL_UI_TOKEN_FILE=/config/ui-login-key docker compose up -d
```

Compose 已透传上述两个 UI 变量；空值按未提供处理。自定义环境值或文件不会打印或复制到 `/data/ui-token`；外部文件只读加载，错误不回退托管密钥。环境值与文件同时提供时报错；原生启动的 `--token-file PATH` 优先于环境文件路径，但不会消除与环境密钥的冲突。`--rotate-token` 仅支持托管来源。

关闭首次打印（仅影响日志，不影响托管文件生成）：

```sh
CF_TUNNEL_ACCEL_UI_PRINT_INITIAL_TOKEN=0 docker compose up -d --build
```

原生启动也支持 `--no-print-initial-token`。需要明确读取当前密钥时，可在受保护的本地终端运行（会输出凭据，不要重定向到共享日志）：

```sh
docker compose exec cf-tunnel-accel python /app/ui/server.py --print-token
```

该命令使用容器当前的来源配置；已经关闭首次打印的托管密钥在重启时也不会补打。

## 能力与健康范围

`/api/state.capabilities` 和 `/api/environment.environment.capabilities` 报告：

```json
{"runtime":"container","local_tunnel_management":false,"service_discovery":false,"health_scope":"container-network"}
```

- `add`、`update`、`delete` 和 UI 服务映射禁用；API 在任务入队前拒绝宿主 ingress/服务管理。请在实际管理 connector 的宿主 CLI 执行，之后更新只读配置快照。
- `switch` / `repair`、`cdn-set` / `cdn-best` / `cdn-test` 与 `health` 按各自正常配置、凭据、就绪及忙碌条件运行，不因宿主服务未管理而一律禁用。
- Tunnel 服务显示“未管理（容器）”，不是 DOWN；不发现宿主监听端口。环境页展示运行能力。
- 健康任务返回 `partial: true`、`scope: "container-network"` 和 `unchecked`（如 `host-systemd`、`host-connector-config`）。即使 `ok: true`，也只表示本次有限范围检查通过，不是完整健康；宿主 systemd 与本机 connector 配置校验被跳过。云端读写与后端探测也不替代真实端到端业务验收。

## 构建与验证边界

基础镜像默认 `python:3.12-slim`，可用 `PYTHON_IMAGE` 覆盖；包含 `curl`、`ca-certificates`、`dnsutils`、`iproute2`。构建上下文使用 `.dockerignore` allowlist，不把 Git、测试、文档、本地配置或凭据打包进镜像。

```sh
docker compose config --quiet
docker compose build
```

不要分享可能包含环境密钥的完整 `docker compose config` 输出。隔离容器 E2E 及检查要求见[贡献指南](../CONTRIBUTING.md)；测试通过不代表真实 Cloudflare 资源开通或公网路由验收。
