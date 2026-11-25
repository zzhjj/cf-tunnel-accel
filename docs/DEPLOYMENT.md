# 部署、配置与多 profile

本文件说明公共部署契约，不记录任何运行中服务器。快速入门见 [README](../README.md)，UI 访问见 [UI README](../ui/README.md)，升级见[维护手册](MAINTENANCE.md)。

## 平台与部署范围

- 目标平台是 **Linux / Python 3.9+ / Bash**；Python 运行时使用标准库。不能由此推断 Windows、macOS 或所有容器均受支持。
- Tunnel 操作需要 cloudflared、已有 Tunnel 配置/凭据及可达后端；HTTPS/DNS 检查依赖 curl/dig，相应诊断可使用 ss/ping。
- root 或等效服务管理权限仅对写入受保护路径、systemd 服务/定时器操作必需。普通用户安装不授予重启 root 服务的权限。
- 无 systemd 可安装 CLI/UI 并前台运行；Tunnel 可由管理员另行前台运行。当前路由修改流程可能要求重启已配置服务，不承诺前台部署拥有全部管理功能。
- 先人工完成 Cloudflare zone、SaaS 启用、Tunnel 创建、Token 和源站规划。`init` 不是账户 onboarding，安装器不调用 Cloudflare API。

## 安装布局与服务

优先从完整包执行 `./install.sh --user`，先加 `--dry-run` 查看计划；已有 cloudflared 可加 `--no-deps` 跳过依赖下载。`--source-dir /absolute/package` 可选择另一份源码。来源目录与包目标不得重叠。

| 内容 | 用户安装 | root 系统安装 |
|---|---|---|
| 包（CLI + 三个辅助模块 + UI） | `${XDG_DATA_HOME:-$HOME/.local/share}/cf-tunnel-accel` | `/usr/local/lib/cf-tunnel-accel` |
| 启动器 | `$HOME/.local/bin` | `/usr/local/bin` |
| 应用配置 | `${XDG_CONFIG_HOME:-$HOME/.config}/cf-tunnel-accel` | 同样按执行用户 HOME/XDG 计算，或显式覆盖 |
| Tunnel 配置 | 管理员明确选择的路径 | 管理员明确选择的路径 |

包内必须有 `cfa_config.py`、`cfa_ingress.py`、`cfa_cdn.py`，只复制 CLI 会造成导入失败。用 `--bin-dir`、`--link-dir`、`--config-dir` 可覆盖布局；启动器会保存安装时选择的 env/模块路径。

安装器的 `--service` 与 `--ui-service` 均为可选，要求 root 系统安装和运行中的 systemd。无 systemd 时跳过 unit，并给出前台运行提示。生成单元后不会自动启动或启用；这也不证明 Tunnel 已连通。

已有 Tunnel：提供 `--config /etc/cloudflared/example.yml --service --service-name cloudflared-example.service`。新建本地配置还要 `--tunnel-id <YOUR_TUNNEL_UUID> --credentials-file /secure/path/tunnel.json --hostname origin.example.org --backend http://127.0.0.1:8080`；配置和凭据不会自动从机器上搜寻，云端 Tunnel 须先创建。`--no-deps --service` 仍需已安装 cloudflared 执行本地 ingress 校验。

UI 使用 `--ui-service`，默认单元 `cf-tunnel-ui.service`、监听 `127.0.0.1:8642`，支持 `--ui-service-name`。管理员核对运行用户、env、监听及权限后，再显式启动需要的单元。手动模板是另一种部署方式，不要混装不同版本。

默认依赖模式可能使用系统包管理器或下载官方 cloudflared release；`--no-deps` 关闭这些动作，Python 必须预先存在。`--dry-run` 仅做本地预检和计划输出，没有文件/下载/服务变更，不是安装完成或云端可用的证据。

## 配置优先级与凭据来源组

默认配置目录来自 `CF_TUNNEL_ACCEL_CONFIG_DIR`，未设置时为 `${XDG_CONFIG_HOME:-$HOME/.config}/cf-tunnel-accel`；默认 env 为该目录的 `env`。`CF_TUNNEL_ACCEL_ENV_FILE` 可显式覆盖，部署前务必确认该文件存在且可读。未选择注册 profile 的普通 CLI 对缺失 env 可继续使用环境/默认值，不把“能启动”当成该文件已加载；CLI 选中的注册 profile env 缺失会报错，UI 注册表引用的任一 env 缺失也会导致启动失败。非法格式/读取错误需处理后再操作，不静默回落。

2.2.1 先处理命令前的 CLI 全局 profile flags / 本机选择 marker，再解析设置和凭据，规则见[CLI 选择与本机 marker](#cli-选择与本机-marker)。未选择注册 profile 时，进程环境优先于 env 文件，再使用通用默认值。env 使用 `KEY=VALUE`、整行注释和可选包裹引号；不执行 shell、不展开变量、不支持把它当作 shell 脚本 source。相同键应只写一次。手写路径使用绝对路径。

凭据按**来源组**而非逐个别名合并：

1. 进程环境只要设置了任一非空 `CF_TUNNEL_ACCEL_API_TOKEN`、`CF_API_TOKEN`、`CLOUDFLARE_API_TOKEN`、`CF_TUNNEL_ACCEL_TOKEN_FILE`，整个凭据组来自进程环境；env 文件中其它 Token 别名不补入。
2. 否则使用选定 env 的凭据组，再退到配置目录下 `token` 文件。
3. 一个组内：`CF_TUNNEL_ACCEL_API_TOKEN` → `CF_API_TOKEN` → `CLOUDFLARE_API_TOKEN` → `CF_TUNNEL_ACCEL_TOKEN_FILE` 指定的文件。推荐只指定一个私有 Token 文件。

例如，进程仅设置 Token 文件时，env 中的内联 Token 不会盖过它；文件缺失也不会借用低优先级内联 Token。安装启动器只固定所选配置目录/env-file/CLI 路径，不再强制导出默认 Token-file。注册表 profile 的显式凭据组优先于继承凭据。用 `env` 核对来源，不显示 Token 内容；账户/zone/path 等元信息分享前仍需脱敏。

交互式 `cf-tunnel-accel init` 是最小配置路径：先准备已有 Tunnel 配置/凭据及 API Token，默认读取 `~/.cloudflared/config.yml` 并从中取得 Tunnel UUID，服务名默认 `cloudflared.service`，因此只需填写 SaaS 辅助 zone `AUX_ZONE` 并接受默认服务名。`--non-interactive --aux-zone example.org --service cloudflared.service` 适合自动化；`--config` / `--service` 只覆盖非默认路径。`AUX_ZONE=example.org` 自动派生 `origin.example.org` 和 `cdn.example.org`，新用户不必分别输入 origin/入口，也不需手填账户或 Zone ID。

**业务 zone ≠ SaaS 辅助 zone**：例如 `example.net` 管理 `app.example.net` 的业务 DNS，`example.org` 提供 SaaS custom hostname、origin 和优选入口。`init --aux-zone` 选择后者，不是待新增的业务 hostname；业务 DNS 自动写入还要求 Token 对业务 zone 有相应权限。

`ACCOUNT_ID`、`AUX_ZONE_ID`、`PREFERRED_TARGET`、`PROBE_HOSTNAME` 均非 `init` 必填：多账户或希望写前严格限定账户时再显式设置。优选目标没有个人默认值，但首次新增要求已有有效入口 CNAME 或明确的目标，见[新增预检与首次入口](#新增预检与首次入口)。示例见 [env.example](env.example)。

### 关键变量

以下省略共同前缀 `CF_TUNNEL_ACCEL_`。

| 变量 | 含义 / 默认边界 |
|---|---|
| `AUX_ZONE` | SaaS 辅助 zone，首次配置必填；`ACCOUNT_ID`、`AUX_ZONE_ID` 可省略后解析，多账户或写前严格限定时显式设置 |
| `TUNNEL_ID`、`CONFIG`、`SERVICE` | `CONFIG` 默认 `~/.cloudflared/config.yml`；UUID 从该配置读取；`SERVICE` 默认 `cloudflared.service`。非默认部署显式覆盖 |
| `TUNNEL_NAME` | 通用显示名默认 `default`；默认配置路径按 HOME 派生，不替代显式选择 |
| `TOKEN_FILE` | 私有 Cloudflare API Token；不是 UI 登录凭据或 Tunnel JSON |
| `FALLBACK_ORIGIN`、`PREFERRED_ENTRY` | 默认从辅助域派生 `origin.<zone>`、`cdn.<zone>` |
| `PREFERRED_TARGET`、`CDN_CANDIDATES` | 管理员选择的目标 / 逗号分隔候选，不保证公共候选长期有效 |
| `PROBE_HOSTNAME` | 已激活、可经同一辅助 zone 服务的真实业务域名；自动优选必需 |
| `CUSTOM_ORIGIN` | 本 Tunnel 的独立 SaaS `custom_origin_server`，共享辅助 zone 时显式规划 |
| `FALLBACK_SERVICE` | ingress 兜底，默认 `http_status:404`，不是业务后端默认端口 |
| `ZONE_CACHE`、`ZONE_CACHE_TTL` | zone 缓存及 TTL（默认 21600 秒）；显式共享路径可能破坏隔离，应按账户/profile 分开 |
| `LOCK` | 写锁路径；与其它会修改相同资源的实例协调，不把不同锁当成云端隔离 |
| `CLOUDFLARED` | 可执行文件路径，默认在 PATH 中查找 |
| `PROFILE`、`PROFILES` | CLI profile ID / registry 路径选择 marker；无显式路径时 registry 为 `config_dir/profiles.json`，仅有路径不自动选 ID |

## 源站归属与共享影响面

业务域名使用 DNS-only CNAME 指向优选入口，优选入口再以 DNS-only CNAME 指向候选目标；SaaS 按**业务 Host/SNI**识别 custom hostname。辅助 origin 需为代理开启、指向对应 Tunnel 的 DNS 记录；本地 ingress 必须包含实际业务 hostname 和后端。

辅助 zone 的 SaaS fallback origin 是共享设置，不属于某个 UI profile 的私有配置。多个 Tunnel 共用辅助 zone 时：

- 为每个 Tunnel 规划独立 origin，如 `origin-alpha.example.org`、`origin-beta.example.org`，设为各自 `CUSTOM_ORIGIN`，并确认当前 Cloudflare 服务支持此自定义 origin 用法。
- 不随意重指共享 origin 或启用 `ALLOW_ORIGIN_TAKEOVER=1`；接管可能影响其它业务。不同名称的 profile 不等于不同云资源。
- `PREFERRED_ENTRY` 相同的所有域名共享入口切换影响面；即使 profile 属于同一账户，若选择不同辅助 zone/入口，也不应描述成“全账户必定共用”。
- 不在不同 Tunnel 间重复声明同一业务 hostname，除非明确设计并验证了这种部署。

## 多 profile 注册表

CLI 与 UI 使用同一份管理员控制的本机 JSON，但**选择方式不同**：CLI 选择一个明确 ID，UI 加载注册表并保留界面选择器。先按模板准备各 profile 的私有 env 与注册表；仅在 UI 注册，不会让独立终端的裸 CLI 自动载入。

### CLI 选择与本机 marker

2.2.1 的全局前缀 `--profile ID` / `--profiles PATH` 也支持 `--profile=ID` / `--profiles=PATH`，必须位于命令前，在首次设置/凭据读取前处理：

```bash
cf-tunnel-accel --profile alpha env
cf-tunnel-accel --profile alpha add app.example.net http://127.0.0.1:8080
cf-tunnel-accel --profiles /etc/cf-tunnel-accel/profiles.json --profile beta env
cf-tunnel-accel --profile=beta --profiles=/etc/cf-tunnel-accel/profiles.json add api.example.net http://127.0.0.1:9090
```

ID 与 registry 路径分别按 **命令行 flag > 进程环境 > 本机 env marker** 选择。无 flags 时读取 `CF_TUNNEL_ACCEL_PROFILE`、`CF_TUNNEL_ACCEL_PROFILES`；本机 marker 文件默认是配置目录中的 `env`，若进程指定 `CF_TUNNEL_ACCEL_ENV_FILE`，则读取该文件。registry 路径未指定时使用 `config_dir/profiles.json`，不会根据选中 profile 的 env 路径改默认目录。

已有 `alpha` / `beta` 注册表时，推荐在默认本机 env（不是公共模板）中加入：

```ini
CF_TUNNEL_ACCEL_PROFILE=alpha
# 可选：registry 不在默认配置目录时才需要
CF_TUNNEL_ACCEL_PROFILES=/etc/cf-tunnel-accel/profiles.json
```

这样裸 `cf-tunnel-accel env` / `add` 使用 `alpha`；`--profile beta` 仍优先。marker 只参与选择，不把默认 env 的 Token/业务设置带入所选 profile。实际 env、marker、registry 和凭据保存在本机，权限受限，**不入 Git**。

仅有 `--profiles PATH`、路径环境变量或路径 marker 而无 profile ID/ID marker 时会报错，绝不自动选第一个。既无 ID 也无路径选择时，CLI 不加载 registry，即使它存在或 UI 正在使用它，也仍解析默认 env。未知 ID、重复 flags、缺少参数、坏 registry 或所选 env 错误均非零退出，不静默退回默认/其它 profile 的凭据；CLI 只加载所选 profile 的 env，不读取未选 profile 的 env/凭据。

### UI 加载与选择器

UI 参数仍放在 `ui` 后，独立于 CLI 全局前缀：

```bash
cf-tunnel-accel ui --profiles /etc/cf-tunnel-accel/profiles.json
# 安装的 UI 启动器等价入口
cf-tunnel-ui --profiles /etc/cf-tunnel-accel/profiles.json
# 仓库内等价入口
python3 ui/server.py --module "$PWD/cf-tunnel-accel" \
  --profiles /etc/cf-tunnel-accel/profiles.json
```

也可设 `CF_TUNNEL_ACCEL_UI_PROFILES`。未显式指定时，UI 配置目录内存在 `profiles.json` 会自动加载；都没有则运行一个 `default` profile。显式注册表缺失、非法或目标冲突时启动失败，不退回其它账户。

### 注册表格式与 profile env

格式见 [profiles.example.json](profiles.example.json)：顶层仅 `profiles` 非空数组，每项仅 `id`、`name`、`env_file`。ID 唯一，以字母/数字开头，随后只用字母、数字、`_`、`-`；名称非空；`env_file` 必须是绝对路径。JSON 不展开 `$HOME`、`~` 或 shell 变量。样例 `/etc` 路径可替换为自己的绝对 HOME 路径。

每个 profile env 显式保存自己的配置，示例（账户/Zone ID 可省略）：

```ini
CF_TUNNEL_ACCEL_TUNNEL_ID=<YOUR_TUNNEL_UUID>
CF_TUNNEL_ACCEL_CONFIG=/etc/cloudflared/alpha.yml
CF_TUNNEL_ACCEL_SERVICE=cloudflared-alpha.service
CF_TUNNEL_ACCEL_ACCOUNT_ID=<YOUR_ACCOUNT_ID>
CF_TUNNEL_ACCEL_AUX_ZONE=example.org
CF_TUNNEL_ACCEL_AUX_ZONE_ID=<YOUR_AUX_ZONE_ID>
CF_TUNNEL_ACCEL_TOKEN_FILE=/etc/cf-tunnel-accel/alpha/token
CF_TUNNEL_ACCEL_ZONE_CACHE=/etc/cf-tunnel-accel/alpha/zones.json
```

前三项是 CLI/UI 注册表 loader 的必要项，须在各 env 显式填写，不能靠继承补齐；config 必须绝对路径。UI 加载全部 profile 时还检查 config 与 service 各自不同（`name` / `name.service` 当作同一服务）；CLI 只加载所选 env，不校验其它 profile 的本地目标。部署时仍为每个 profile 规划不同 config/service。`AUX_ZONE`、Token 和本地 Tunnel 配置是实际新增的前提；账户/Zone ID 可省略后解析，缓存项用于明确归属。`beta` 使用自己的 `/etc/cloudflared/beta.yml`、`cloudflared-beta.service`、`/etc/cf-tunnel-accel/beta/` 路径和 Tunnel UUID，不复制 `alpha` 的实际值。多账号时**每个 profile 都设置自己的凭据来源**，否则无凭据设置的 profile 可能继承启动进程的凭据。

注册表模式中，profile env 的 `CF_*` 设置覆盖继承配置；显式非空 profile 凭据组先清除继承的三个 Token 别名及 `TOKEN_FILE`，再应用本 profile 的来源，避免父进程内联 Token 盖过它的 Token 文件。`env_file` 由注册表固定，文件内的 `CF_TUNNEL_ACCEL_ENV_FILE` 不转指其它 env。UI 的 profile 切换不改变独立终端中的 CLI 配置；CLI 用 `--profile alpha env` 或本机 marker 选择。手设 `CF_TUNNEL_ACCEL_ENV_FILE` 仍是普通单 env 入口，但不等同于按注册表加载，也不应用注册 profile 的覆盖规则。

权限由管理员落实：目录 0700，registry、env、Token 和 Tunnel 凭据 0600，运行用户可读，父目录不允许非受信用户写入。loader 不提供所有权沙箱。

### 隔离边界

- UI 只接受注册表 profile ID，不接受浏览器提交任意文件路径；未知/重复 profile 查询参数返回 400，不悄悄选择默认目标。
- 每 profile 有独立 CLI 模块配置与界面数据键，CDN 历史按 profile 区分；默认 zone 缓存使用账户、辅助域、配置/凭据作用域标识校验。仍推荐显式不同 `ZONE_CACHE` 路径，防止覆盖与频繁失效；缓存不证明实时授权。
- 会话、任务列表、审计、解释器和写任务 worker 是共享的；不是每 profile 不同管理员权限。写任务采用全局单写准入，冲突请求可返回 409。
- 文件锁和自动优选 unit 路径可能共享；不同 profile 的模块不能隔离相同 Cloudflare DNS/SaaS 资源。
- CLI 配置在 profile 导入时固化于独立模块；运行期 helper 读取该 profile 快照而非共享活动 `os.environ`。文件配置和 UI 视图键各自独立；同一进程内 profile 切换不用在线程间改环境。

## 新增预检与首次入口

`main` 的 `add` / 无参数入口在写锁、Token 联网核验和第一次输入之前先做本地预检；直接调用 `interactive_add` 也在选择 hostname 和后端输入之前检查。缺少或无效的 `AUX_ZONE`、`PREFERRED_ENTRY`、`FALLBACK_ORIGIN`、Tunnel UUID、本地 config，或 config 与 UUID 不一致时立即停止。错误列出实际 env 路径、当前 profile 和恢复方向：新用户先 `cf-tunnel-accel init`；已有注册表先 `cf-tunnel-accel --profile alpha env` 核对，registry 在其它位置时再加命令前的 `--profiles PATH`。本地检查不等于云端权限、SaaS 或后端已经可用，之后仍需联网核验。

新增流程还会在任何 DNS 写入之前预检 preferred target，先于 origin DNS、SaaS 与 ingress 更改：

- Cloudflare API 读到已有有效优选入口 CNAME 时沿用其目标，不要求另设 `PREFERRED_TARGET`。
- 确认没有入口 CNAME 时才使用明确的 `CF_TUNNEL_ACCEL_PREFERRED_TARGET`；没有目标就停止，提示先配置目标或执行 `cdn set <目标域名>`。
- 读取错误直接向上报告，不 fallback 到默认目标、不覆盖原入口；非法目标或入口循环也停止。该预检失败时尚未发生新增流程的 DNS/SaaS/ingress 写入，不承诺后续跨服务操作原子化。

新部署可在所选私有 env 中配置目标，或显式创建入口后再新增：

```bash
cf-tunnel-accel --profile alpha cdn set edge.example.net
cf-tunnel-accel --profile alpha add app.example.net http://127.0.0.1:8080
```

`edge.example.net` 是通用占位目标，需替换并验证；`cdn set` 本身是 DNS 写操作，不是预检或业务可用性证明。只填 `AUX_ZONE` 完成 `init` 不等于新优选入口已经存在。

## 定时任务

`schedule install [1–168 小时]` 是显式写操作：安装并启用 systemd timer，默认 6 小时。先手工验证同一配置下 `cdn best` 的行为，再决定启用；服务必须获得固定的绝对 env 路径、Token 来源与同样的账户/辅助 zone/业务 probe，不能只依赖交互 shell 的 export。

定时 service 固定本次选择的绝对 env-file、配置目录、解释器与 CLI 路径；timer 引用实际 `CDN_SERVICE_UNIT` basename，不复制 Token 内容。未持久化配置或未设置业务 probe 时拒绝安装。默认单元适用于单部署；多 profile 分别设置 `CF_TUNNEL_ACCEL_CDN_SERVICE_UNIT` / `CF_TUNNEL_ACCEL_CDN_TIMER_UNIT`，避免覆盖同名计划。无 root/systemd 时使用前台人工 CLI。

## 验收与限制

先本地核对 `--version`、`env`、配置路径和权限；再由运维单独安排 Cloudflare 读取、源站连通性和受控写验证。线上验收要确认 SaaS/证书 active、权威 DNS、公共 DNS、业务 Host/SNI 和应用行为，不只看 API 成功或进程 active。

CLI、UI 与定时优选已使用同一份 `cfa_cdn.py` 策略：至少两个业务 HTTPS 样本、≥80% 成功率、固定 Host/SNI/IP 证据，`force` 仅跳过延迟防抖。手动切换做 DNS 原记录快照和 API 确认失败恢复；自动优选另做业务验收，失败恢复旧目标。恢复有界且可能失败，不等同于 Cloudflare 全球缓存同步。跨 DNS/SaaS/Tunnel 仍无统一事务，失败可留下部分变更；ingress 是纯块式 YAML 子集，复杂结构明确报错；UI 不管理 Token、不自动开通 Cloudflare，外部手工 DNS 仅 CLI；离线测试和本地 contract server 验证不是真实 Cloudflare provisioning 或速度承诺。
