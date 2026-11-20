# cf-tunnel-accel

面向 Linux 的 Cloudflare Tunnel 路由管理工具：用 **Tunnel + Cloudflare for SaaS 辅助 zone + DNS-only 优选 CNAME** 连接业务域名与本机后端。提供 CLI 和同源 Web 管理界面；Python 运行时仅使用标准库。

这不是直接改变 Tunnel 网络的“加速魔法”。工具不修改 Cloudflare Anycast 路由、运营商线路或 Tunnel 传输协议；候选入口的表现取决于客户端地区、网络、时间和 Cloudflare 策略，**不保证提速或持续可用**。

## 架构与前提

```text
DNS：app.example.net -- DNS-only CNAME --> cdn.example.org
                                      -- DNS-only CNAME --> edge.example.net（候选目标）

请求：浏览器保留业务 Host/SNI app.example.net
      → 候选解析到的 Cloudflare 边缘
      → example.org 上的 SaaS custom hostname / TLS 配置
      → SaaS fallback origin 或该主机名的 custom origin
      → 代理开启的 origin.example.org → <Tunnel UUID>.cfargotunnel.com
      → cloudflared → ingress 匹配的本机后端
```

CNAME 链解决入口地址选择，不会把请求 Host/SNI 换成候选域名。**候选域名自身能打开，不代表业务域名经过它能打开。**

开始前准备：

- Linux、Bash、Python **3.9+**。`cloudflared` 用于 Tunnel；`curl`、`dig` 用于相应网络验收和测速；`ss`、`ping` 提供可选诊断。并非所有操作系统都受支持。
- 自己控制的 Cloudflare active zone 作为 SaaS 辅助 zone，先在 Cloudflare 启用 **Cloudflare for SaaS**，确认自定义主机名资格、证书签发和配额满足需求。安装器不会开通这些能力。
- 已创建的 Tunnel、可读的本地配置及匹配凭据，和从 `cloudflared` 可达的后端。创建/登录 Tunnel 由管理员在 Cloudflare/cloudflared 中单独完成。
- API Token 按需限定资源：辅助 zone 需要 `Zone:Read`、`DNS:Edit`、`SSL and Certificates:Edit`；自动管理业务 DNS 的 zone 需要 `Zone:Read`、`DNS:Edit`。只授予需要管理的 zone，不使用全局 API Key。通常不需账户级 Tunnel 编辑权限：此工具使用已有 Tunnel 和本地凭据，不创建云端 Tunnel。
- 明确源站路由：辅助 origin DNS、SaaS fallback/custom origin、Tunnel UUID、ingress hostname 和后端必须对应。多个 Tunnel 共用辅助 zone 时，先看[源站归属](docs/DEPLOYMENT.md#源站归属与共享影响面)。
- 修改 systemd 单元、重启隧道或安装定时任务，需要 root 或等效的明确授权。安装为普通用户不自动获得这些权限。

示例中的 `example.net` / `example.org` 和 `<YOUR_…>` 均是占位符，不是可用基础设施。

## 安装：优先普通用户

取得完整源码包后，在包根目录执行；不要只复制扩展名为空的 CLI 文件。

```bash
git clone https://github.com/zzhjj/cf-tunnel-accel.git
cd cf-tunnel-accel
./install.sh --user
export PATH="$HOME/.local/bin:$PATH"
cf-tunnel-accel --version
```

普通用户安装不需要 root，只写入 XDG 目录。默认会尝试准备缺少的可选工具；已有 `cloudflared` 时可加 `--no-deps`，它只跳过依赖安装/下载，不跳过 Python 3.9+ 检查。没有 cloudflared 仍可安装并查看本地环境，但不能据此认为 Tunnel 功能已就绪。安装前可加 `--dry-run` 查看计划；安装器不使用系统包管理器。

| 选项 | 实际作用 |
|---|---|
| `--user` | 包放 `${XDG_DATA_HOME:-$HOME/.local/share}/cf-tunnel-accel`，启动器放 `$HOME/.local/bin` |
| 无 `--user` | 要求 root；包放 `/usr/local/lib/cf-tunnel-accel`，启动器放 `/usr/local/bin` |
| `--source-dir DIR` | 从指定完整包安装；默认安装器所在目录；与目标包目录不得重叠 |
| `--no-deps` | 跳过依赖安装/下载，不跳过 Python 检查 |
| `--dry-run` | 预检并打印计划，不写文件、不下载、不装包、不改服务；不是云端或连通性验收 |
| `--config-dir DIR` | 设置应用配置目录；默认 `${XDG_CONFIG_HOME:-$HOME/.config}/cf-tunnel-accel`，root 也按执行时的 HOME/XDG 计算 |
| `--bin-dir DIR` / `--link-dir DIR` | 分别覆盖包目录/启动器目录 |
| `--service` | 可选安装明确指定的 Tunnel systemd unit，需要 `--config` 和 `--service-name` |
| `--ui-service` | 可选安装回环监听的 UI unit；可用 `--ui-service-name` 改名 |
| `--no-service` | 不安装 Tunnel unit（默认）；不代表关闭单独请求的 UI unit |
| `-y` / `--yes` | 兼容旧命令；当前安装本身是非交互的，不代表自动开户 |

完整包必须包含 `cf-tunnel-accel`、`cfa_config.py`、`cfa_ingress.py`、`cfa_cdn.py` 和完整 `ui/`；`LICENSE` 会随包安装。辅助模块必须与 CLI 同目录；它们分别承担配置、受限 ingress 编辑和 CDN 策略，需一起更新、备份与恢复。

root 安装示例（由管理员在确认路径、权限和现有配置后执行）：

```bash
sudo bash ./install.sh --no-deps --source-dir "$PWD"
# 可选：为已有且验证通过的 Tunnel 配置安装 unit
sudo bash ./install.sh --no-deps --source-dir "$PWD" \
  --config /etc/cloudflared/example.yml \
  --service --service-name cloudflared-example.service --ui-service
```

安装器**不自动发现或挑选**其它 Tunnel 配置/JSON 凭据；创建新 Tunnel 配置还要显式提供 `--tunnel-id`、`--credentials-file`、`--hostname`、`--backend http://127.0.0.1:8080`。这不创建云端 Tunnel。服务选项要求 root 系统安装，不能与 `--user` 混用；无运行中的 systemd 时跳过 unit，退回前台 CLI/UI。安装的 unit **不会自动 enable/start**；管理员核对后自行启动。`schedule install` 是独立的显式启用操作，不同于安装器。

安装采用本地暂存与备份，保留相邻 `.bak.*`；这不是 Cloudflare 事务。依赖安装、外部中断或云端操作不属于完全可撤销的范围。不要使用旧文档的镜像前缀、自动凭据识别或自动启动假设。

## 首次配置

默认 env 是 `${XDG_CONFIG_HOME:-$HOME/.config}/cf-tunnel-accel/env`。普通配置优先级：**进程环境 > 选定 env 文件 > HOME/XDG 派生默认值**。用 `CF_TUNNEL_ACCEL_ENV_FILE` 选择其它文件前，先确认文件存在且可读；当前普通 CLI 对缺失 env 可继续采用环境/默认配置，不应把未报错当成选中正确配置。多 profile 注册表引用的 env 缺失则会拒绝启动。

### 最短路径（交互，只填辅助域）

最少只需要三样东西：

1. 已存在的 `~/.cloudflared/config.yml`（顶层含 `tunnel` UUID）；
2. 一个 Cloudflare API Token；
3. 一个启用 Cloudflare for SaaS 的辅助域名。

```bash
APP_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/cf-tunnel-accel"
install -d -m 0700 "$APP_CONFIG"
install -m 0600 /secure/path/cloudflare-token "$APP_CONFIG/token"

cf-tunnel-accel init
# Cloudflare for SaaS 辅助域名: example.org
# 管理的 systemd 服务名 [cloudflared.service]: 直接回车
cf-tunnel-accel env
cf-tunnel-accel ui
```

`init` 会使用默认 Tunnel 配置和 Token 路径，从配置顶层自动读取 Tunnel UUID，并给服务名提供 `cloudflared.service` 默认值。**账户 ID、Zone ID、优选目标和探测域名都不是首次配置的必填项**：账户 ID 省略时按 Token 可见范围解析，Zone ID 首次联网时按辅助域名解析，优选可在管理台之后再设置。

如果配置或服务不在默认位置，只覆盖对应一项：

```bash
cf-tunnel-accel init --config /path/to/config.yml --service cloudflared-example.service
```

非交互安装适合自动化；仍需明确辅助域和服务，但不需要手工提供 UUID、账户 ID 或 Zone ID：

```bash
cf-tunnel-accel init --non-interactive --aux-zone example.org --service cloudflared.service
```

`init` **只生成本地 0600 env**，不创建云资源、不登录、不安装或重启服务；已有完整配置默认拒绝覆盖，检查后用 `--force` 或 `--output`。Token 缺失时它会给出实际应写入的路径。也识别标准环境变量 `CLOUDFLARE_API_TOKEN`，但生产环境更推荐 0600 Token 文件，避免 shell 历史和环境继承。

env 是 `KEY=VALUE` 数据，不是 shell 脚本：不执行、不展开 `$HOME` / `$VAR`。由命令行传入的 `$HOME` 在 shell 中先展开；手写 env 时必须填真实绝对路径。完整模板见 [env.example](docs/env.example)，凭据**来源组优先级**及多账号配置见[部署说明](docs/DEPLOYMENT.md#配置优先级与凭据来源组)。

**旧 root 部署迁移：先配置，再升级。** 如果旧安装依赖代码里硬编码的账户、辅助 zone 或 Tunnel，先在本机受保护的 env 中显式记录自己的 `ACCOUNT_ID`、`AUX_ZONE`、`AUX_ZONE_ID`、`TUNNEL_ID`、`CONFIG`、`SERVICE`、Token 来源及 origin/优选入口设置，核对现有云端归属，再安装新包。不要期待新的 HOME 默认值重现旧机器；不把实际值贴到公共文档、日志或 issue。详见[升级与回滚](docs/MAINTENANCE.md)。

## 常用操作

以下 `domains`、`list`、`status`、`health` 会访问网络/Cloudflare；写命令还会变更选定资源。执行前先用 `env` 在本地核对目标。

```bash
cf-tunnel-accel domains --offline       # 只读本地缓存，不证明云端仍有效
cf-tunnel-accel domains                 # 读取授权 zone 清单
cf-tunnel-accel list
cf-tunnel-accel add app.example.net http://127.0.0.1:8080
cf-tunnel-accel health app.example.net --https
cf-tunnel-accel switch app.example.net
cf-tunnel-accel del app.example.net
cf-tunnel-accel ui                      # 前台启动控制台，默认回环
```

HTTPS 后端**默认验证证书**，也可显式 `--verify`；仅自签等明确需要时选择 `--no-verify`（UI 的 noTLSVerify 复选框）。证书失败不会自动退成不校验证书或“TCP 可达=健康”。已有 ingress 的其他 originRequest 参数保持不变；探测只是当前 HTTP/TLS 连通性，不替代业务功能验收。

外部域名不在当前 Token 的业务 zone 中时，只在**交互式 CLI** 执行：

```bash
cf-tunnel-accel add app.example.net http://127.0.0.1:8080 --external
```

按提示在权威 DNS 平台手动添加 ownership/DCV/ACME 和 DNS-only CNAME，等待生效再回终端确认。控制台不提交需 stdin 确认的外部 DNS 任务；外部 DNS 的切换、清理也由管理员手工完成。中途失败可能已有 Tunnel/SaaS 变更，先核对再重试。

## CDN 测速不等于业务验证

CLI 与 UI 已共用 `cfa_cdn.py` 的 HTTPS 判定。`cdn test` 不写 DNS；`cdn best` 仅在已配置业务探测域名且候选满足业务证据门槛时切换。手动 `cdn set` 是显式 DNS 操作，不代表业务已验收。

```bash
cf-tunnel-accel cdn show
cf-tunnel-accel cdn test                 # 网络探测，不改 DNS
cf-tunnel-accel cdn best                 # 按业务 HTTPS 样本及改善阈值自动优选
cf-tunnel-accel cdn best --force         # 只跳过改善阈值，不绕过有效性
cf-tunnel-accel cdn set edge.example.net # 手动更改入口，API 确认失败尝试恢复旧记录
```

- 未配置 `CF_TUNNEL_ACCEL_PROBE_HOSTNAME` 时，候选自身的 DNS/TCP/HTTPS 只作为 **network-only** 测量，不证明业务 Host/SNI 可用，不能据此自动切最优。
- 自动优选要求真实业务探测域名、证书校验成功、有效 HTTPS 计时，**至少 2 个健康样本且成功率 ≥80%**。当前健康 HTTP 范围为 2xx–4xx，5xx、超时、证书失败不是健康样本；4xx 可说明网络可达，但不证明业务功能或权限正确。
- `--force` 只绕过防抖改善百分比（默认 15%），不允许用 TCP/ping 代替业务 HTTPS，也不允许把失败候选强行变成有效候选。
- 手动切换保存旧 DNS 记录并通过 API 确认；确认失败时尝试恢复。自动优选还会验收业务 HTTPS，失败时恢复旧目标并报告恢复后的检查结果。单次 CNAME 等待上限 30 秒、单次 HTTPS 检查上限 25 秒；API 请求本身另有有界超时/重试。恢复也可能失败，不承诺跨服务原子性或全球 DNS 传播完成。
- 一个 probe hostname 和当前机器出口只覆盖有限样本；其它业务域名、地区、运营商仍需独立验收。公共候选随时可能变化。

定时优选是可选的 root/systemd 操作：`schedule install 6` 会安装并启用 timer。先确认定时单元携带**同一份显式 env**，与手工 `cdn best` 的配置/凭据一致；多 profile 不要直接复用默认单元路径。见[部署说明](docs/DEPLOYMENT.md#定时任务)。

## Web 管理界面

控制台采用简洁的**路由优先**布局：路由、本机服务、优选、诊断、任务审计、环境，共六页。错误、过期/未知和部分数据会分别显示，不把读失败显示成健康的空列表；任务执行结束不等于检查通过。

后台任务记录可保留最近结果；重启后未确认任务标记为 `interrupted`，**不会自动重放**。失败/中断不代表 DNS、SaaS 或本地配置未改变。UI 没有 Token 管理、Cloudflare 开户/onboarding 向导或跨云资源事务。

默认监听 `127.0.0.1:8642`。远程优先 SSH 转发，或在受信 TLS 代理之后部署；非回环需显式 `--allow-remote-http`，该参数本身不提供 TLS。访问、任务与审计为管理员级控制面，不是多租户权限系统。

## 文档与验证边界

- [部署、配置优先级和多 profile](docs/DEPLOYMENT.md)
- [UI 六页、访问与服务部署](ui/README.md)
- [维护、旧 root 迁移、备份和回滚](docs/MAINTENANCE.md)
- [维护范围与验收要求](docs/MAINTENANCE-SPEC.md)
- [贡献与离线检查](CONTRIBUTING.md)
- [env 模板](docs/env.example) · [profiles JSON 模板](docs/profiles.example.json)

已知边界：ingress 编辑只支持**纯块式 YAML 子集**，遇到不支持结构应停止，不是通用 YAML 编辑器；多 profile 使用同进程模块，不具备完整进程隔离，AST guard 也不是隔离证明；跨 DNS/SaaS/Tunnel 没有统一事务。离线测试和 mock/浏览器测试不证明真实 Cloudflare 开通、证书签发或公网提速。本 README 不发布特定机器的部署记录，也不代表本次已执行任何云端验收。

## 许可证

MIT，见 [LICENSE](LICENSE)。
