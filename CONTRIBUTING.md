# 贡献指南

先读 [README](README.md)、[部署与 profile 契约](docs/DEPLOYMENT.md)、[UI 说明](ui/README.md)、[Docker 边界](docs/DOCKER.md)和[维护验收规范](docs/MAINTENANCE-SPEC.md)。文档以可观察实现为准；计划、测试目标、离线结果与真实 Cloudflare 验收要分开写。

## 范围与开发约定

- 提交小而可复核的变更，先建立离线复现/失败路径；保留他人在途修改，不接管不相关文件。
- CLI 与 UI 共用 Cloudflare/Tunnel 逻辑，不在前端另建 DNS/SaaS 实现。完整包含扩展名为空的 `cf-tunnel-accel`、`cfa_config.py`、`cfa_ingress.py`、`cfa_cdn.py` 和 `ui/`，接口变更需一起检查。
- Linux / Python 3.9+ 是兼容目标，运行时仅标准库；不声称所有操作系统可用。systemd/root 权限只在受控部署中验证，不作为普通开发环境前提。
- 关注认证/CSRF/origin、配置来源组、账户/缓存归属、YAML 拒绝路径、CDN 有效性/回滚、并发、部分失败与任务中断。静态 AST guard 不是进程隔离证明。
- 安装器检查需临时 HOME/XDG/前缀、stub、`--no-deps`；不得安装到现有用户/系统路径、调用包管理器、下载或启动真实服务。不要在开发测试运行真实 `add`、`del`、`switch`、`cdn best/set` 或 `schedule install`。

## 隔离测试环境

测试另需 pytest；在有适当依赖来源的开发环境预先准备。不要把安装测试依赖和项目的离线验证混为一谈。

```bash
python3 -m venv .venv
. .venv/bin/activate
# 仅依赖准备阶段，需要包来源；以下不属于离线测试命令
python -m pip install 'pytest>=8.3.5,<9'

TEST_STATE=$(mktemp -d)
export HOME="$TEST_STATE/home"
export XDG_CONFIG_HOME="$TEST_STATE/config"
export XDG_DATA_HOME="$TEST_STATE/data"
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"
# 显式 env 必须可读：创建空文件，不指向现有机器配置
: > "$TEST_STATE/empty.env"
unset CF_API_TOKEN CLOUDFLARE_API_TOKEN CF_TUNNEL_ACCEL_API_TOKEN
export CF_TUNNEL_ACCEL_ENV_FILE="$TEST_STATE/empty.env"
export CF_TUNNEL_ACCEL_TOKEN_FILE="$TEST_STATE/absent-token"
export CF_TUNNEL_ACCEL_CONFIG="$TEST_STATE/absent-tunnel.yml"
export CF_TUNNEL_ACCEL_CONFIG_DIR="$TEST_STATE/config/cf-tunnel-accel"
export CF_TUNNEL_ACCEL_UI_DIR="$TEST_STATE/ui"
export CF_TUNNEL_ACCEL_SCRIPT="$PWD/cf-tunnel-accel"
```

最好从不携带任何真实 `CF_*` 配置的干净测试进程开始。以上清理不是任意父环境的完整净化器；同时检查继承的 account/zone/cache/registry 等设置。HTTP、Cloudflare、DNS、cloudflared 和 systemd 调用必须在测试边界 mock/stub，不读取个人目录、凭据或实际服务状态。

## 本地检查（仓库根目录）

以下是应运行并记录结果的命令，**不是本文件已经测试通过的声明**：

```bash
python -m py_compile cf-tunnel-accel cfa_config.py cfa_ingress.py cfa_cdn.py ui/*.py tests/*.py ui/tests/*.py
bash -n install.sh
python -m pytest tests -q
python -m pytest ui/tests -q
python tests/test_cf_tunnel_accel.py
python -m json.tool docs/profiles.example.json >/dev/null
node --check ui/static/app.js
git diff --check
```

`py_compile` 必须显式包含无后缀 CLI 和辅助模块。两组 pytest 发现各目录测试；独立 unittest 入口只作额外兼容性检查，不替代 pytest/UI 测试。浏览器测试可能有额外运行依赖和启动边界，按实际测试文件要求单独记录，不把文件存在当成已运行。CI 的实际矩阵/步骤以 [.github/workflows/tests.yml](.github/workflows/tests.yml) 为准，不在文档固定用例数。

不要把语法检查、AST 调用图检查、mock HTTP 或浏览器界面测试称为真实 Cloudflare provisioning 证明。真实 zone/SaaS 启用、证书签发、权威/公共 DNS、各地业务 Host/SNI 和速度表现需另行验收，不能在普通开发检查中偷偷执行。

## Docker 与登录契约检查

- 变更容器能力时同时核对 `/api/state.capabilities` 与 `/api/environment.environment.capabilities`。`local_tunnel_management: false` 只禁用 add/update/delete/服务映射，不额外禁用 switch/repair/CDN/health；`service_discovery: false` 不得显示成宿主无服务。未管理不是 DOWN。
- 容器健康 `partial: true` / `scope: "container-network"` / `unchecked` 必须在 UI 与文档明确表达为有限范围，即使 `ok: true` 也不是完整健康。
- 登录测试覆盖 32–1024 可打印字符/首尾空白、CLI 文件路径优先、环境值与文件冲突、只读外部来源不打印/持久化/轮换，以及 UI 变量不从 Cloudflare env 文件加载。
- 托管随机密钥为 48 位十六进制、0600；首次成功绑定/启动才打印 `首次登录密钥: ...` 一次，重启不重打，关闭首次打印有效。测试不得向输出泄漏实际测试密钥。
- Compose 检查 named state volume `/data` 的 UID 1000 初始化、配置只读绑定、回环端口、空 profiles 透传、非 root/只读根/cap-drop；不要把旧 `./data` bind 写回文档。

可选的真实容器 E2E（需可用 Docker Engine/Compose 与构建依赖网络，**不是普通离线 pytest**）：先读 `tests/docker_e2e.py`，再从仓库根目录执行：

```bash
python tests/docker_e2e.py
```

该脚本使用隔离 Compose 项目及 fixture Cloudflare/后端服务，验证容器 HTTP 会话、密钥/状态持久化与能力边界，并清理测试资源。不连接真实 Cloudflare、不接管现有 connector，不证明公网业务成功。不要将普通开发 `config/`、宿主状态或真实凭据挂入测试；分享日志前确认不含首次登录密钥。`docker compose config --quiet` 可检查配置语法，避免输出包含自定义 UI 密钥的完整解析配置。

## 文档与 PR 检查单

- 列出改动范围、实际执行的命令、Python 版本、结果和未执行项；不虚构通过状态、云端验收或个人机器部署记录。
- 用户文档解释 Tunnel + SaaS 辅助 zone + DNS-only CNAME，不承诺直接改变 Tunnel 网络或保证提速。
- 示例只用 `example.net` / `example.org`、明显的 `<YOUR_…>` 占位符、通用服务名和 HOME/XDG；无个人域名、真实 ID/IP/PID、凭据、主机路径或真实 profile 清单。
- env/JSON 不展开 shell 变量；模板里的路径需说明替换为自己的绝对路径。更新链接和安装/迁移步骤，明确 `--user`、服务权限、旧 root 显式配置、三个 helper 的打包要求。
- 不提交 API/UI Token、Tunnel JSON、env、证书、缓存、任务记录、审计、备份或实际服务器截图。日志脱敏后再分享。
- 检查 `git status --short`、限定范围 diff、完整 diff 和链接；文档变更不顺便修改 source、产品规格或不相关文件。
- 发布前按[维护手册](docs/MAINTENANCE.md) 准备备份和回滚；报告剩余限制：无 Token UI/onboarding/跨云事务、纯块式 YAML 子集、有限网络探测和同进程 profile 边界。
