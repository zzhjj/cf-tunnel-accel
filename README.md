# cf-tunnel-accel

Cloudflare Tunnel + **双域名（辅助域 SaaS）优选加速**一键脚本。业务域名保持关黄云，走「辅助域 SaaS 回退源 → 公共优选 CDN」，本地服务通过 cloudflared tunnel 暴露，脚本自动完成 DNS / SaaS 自定义主机名 / 证书验证 / 优选入口切换。

```
业务域名 --(关黄云 CNAME)--> cdn.<辅助域>            # 优选入口
        --(关黄云 CNAME)--> 公共优选 CDN（可切换）
        --> 辅助域 SaaS 识别 Host
        --> origin.<辅助域> (开黄云, 回退源)
        --> cloudflared tunnel (home)
        --> 本机 ip:port
```

## 功能

| # | 能力 | 命令 |
|---|---|---|
| 0 | **自动识别账号里有哪些域名可用**（zone 状态/接入方式/已加速主机名/配额/额外辅助域） | `domains`、`domains --offline` |
| 1 | 新增域名并自动加速（Tunnel + SaaS + DCV + DNS + 优选 CNAME），**未加速的域名和已用子域名直接列出来选** | `add`、`add <zone>`、`add <域名> <ip:port>` |
| 2 | 列出当前隧道主机名 / SaaS 自定义主机名 / 关键 DNS | `list` |
| 3 | 把某个域名切回优选入口 | `switch <域名>` |
| 4 | 删除业务域名（隧道 + SaaS + DNS 清理） | `del <域名>` |
| 5 | HTTPS 实测并一键切换公共优选 CDN（带防抖阈值） | `cdn test / cdn best / cdn set` |
| 6 | 全链路健康诊断（Tunnel / 后端 / SaaS / DNS / 可选 HTTPS） | `health [域名] [--https]` |
| 7 | 可选 systemd 定时自动优选 | `schedule install [小时]` |
| 8 | 查看当前生效的基础设施配置（换服务器核对） | `env` |
| 9 | **Web 控制台 ARC**：浏览器里看本机服务、增删改域名、跑体检、看审计 | `ui` |

外部域名（不在本账号、解析要手动加）也能用：`add x.yourdomain.com 127.0.0.1:8080 --external`，脚本会打印需要手动添加的 ownership / DCV / ACME 记录。

## Web 控制台 ARC

命令行之外还有一个图形界面，只依赖标准库，和 CLI 共用同一套云端逻辑（不重写任何
Cloudflare 交互，避免两套实现互相打脸）。

```bash
cf-tunnel-accel ui                       # 已在跑就直接打印入口；否则前台拉起
cf-tunnel-accel ui --host 0.0.0.0        # 前台这边改绑定
cf-tunnel-accel ui --print-token         # 打印当前访问凭据
python3 ui/server.py --port 8642         # 也可以直接跑（仓库内）
```

```
http://127.0.0.1:8642/      本机
http://<内网IP>:8642/        局域网（systemd 单元默认 0.0.0.0）
凭据 ~/.config/cf-tunnel-accel/ui-token   # 0600，首次启动生成并打印一次
```

界面能力：

| 区块 | 能做什么 |
|---|---|
| LISTENERS | 扫 `ss -ltnp` 出本机全部监听端口，带进程名/命令行/HTTP 探活/已映射域名，可按「全部 / 已映射 / 未映射」筛，一键把某个端口映射成域名 |
| ROUTES | 隧道内所有域名卡片：后端、端口、zone、SaaS/SSL 状态；改后端、切优选入口、单域名体检、看详情、删除（要原样输入域名确认） |
| CDN | 优选入口换区：本机出口实测每个候选（ping/TCP/TLS/TTFB），按可用率与中位数排名；可「测速全部」「一键切最优」（阈值内不动 DNS）、按行切换、或手动输入任意 CDN 域名；日志实时流式输出 |
| DIAGNOSTICS | 跑 `health`，把 cloudflared / 后端 / SaaS / DNS 的输出实时逐行落进面板，PASS 绿、FAIL 红、WARN 琥珀 |
| JOURNAL | 只读审计：谁在什么时候提交了什么任务、成功还是报错 |
| 顶部拓扑 | 实时画「入口 → SaaS → 隧道 → 各后端」链路，域名点按状态着色，可拖动、悬停看域名、点击定位到卡片 |
| 命令面板 | `⌘K / Ctrl+K` 搜域名、服务、动作，键盘直达 |

安全设计（这几点在登录页明写，不是藏起来的）：

- 默认只监听回环地址；要对外用 `--host 0.0.0.0`，此时**凭据就是唯一门槛**，单元里也写清了这一点。
- 凭据是 48 位 hex，存于 0600 文件，恒定时间比较，页面永不回显。
- 会话只存在 HttpOnly Cookie 里，页面脚本读不到；写操作另需 `arc_csrf` 双提交头，
  并校验 `Origin` / `Sec-Fetch-Site`，跨站表单打不进来。
- 登录失败按 IP 指数退避（3 次后 30s 起翻倍，上限 15 分钟），全部落审计。
- 静态资源限定在 `ui/static/` 内，路径穿越一律 404；CSP 只放同源，禁内联脚本、禁被 iframe 嵌套。
- 变更类任务串行执行（单 worker），避免两次隧道重启互相踩；删除必须二次确认且拒绝系统入口。
- **切 CDN 会影响全部域名的入口**，所以：必须先实测、切换要二次确认、按钮文案直接写「影响面：全部 N 个域名」，
  测速全挂（HTTPS 0% 可用）的候选在确认弹窗里用红字劝退——但不禁用，留给你自己判断。
- 切走的旧目标会记进 `~/.config/cf-tunnel-accel/ui-cdn-history.json`（0600），
  所以切到内置候选列表以外的目标（例如 `www.shopify.com`）之后，界面里仍然能一键切回来。

安装成常驻服务：

```bash
sed "s#@UI_DIR@#$PWD/ui#" ui/cf-tunnel-ui.service > /etc/systemd/system/cf-tunnel-ui.service
systemctl daemon-reload && systemctl enable --now cf-tunnel-ui
```

## 依赖

- `python3`（仅标准库，无第三方包）
- `cloudflared`（默认 `/usr/local/bin/cloudflared`）
- `curl`、`dig`（验收与测速）
- `systemd`（隧道服务与定时优选）

## 安装（交互式，缺什么装什么）

```bash
git clone https://github.com/zzhjj/cf-tunnel-accel.git
cd cf-tunnel-accel
./install.sh                       # 交互式：每步都有默认值，回车即可
./install.sh -y                    # 无人值守，全用默认值
./install.sh -y --token <TOKEN>    # 顺带写入 API Token
./install.sh -y --service          # 顺带创建/修复 systemd 隧道服务
./install.sh --dry-run             # 只看会做什么，不落盘
```

install.sh 会依次做六步，**缺依赖自动装**（apt/dnf/yum/apk/pacman 都认）：

| 步骤 | 内容 |
|---|---|
| 1 | 安装脚本本体到 `/root/bin`，软链到 `/usr/local/bin`，旧版自动备份 |
| 2 | 检查并安装 `python3` / `curl` / `dig` / `systemctl` / **cloudflared** |
| 3 | 配置 Cloudflare API Token（权限 600，可交互粘贴或 `--token`） |
| 4 | 可选：生成 `~/.config/cf-tunnel-accel/env` 覆盖模板（多机部署用） |
| 5 | 可选：写 systemd 隧道服务并 `enable --now`，随后校验 ingress |
| 6 | 自检：脚本可用性 + `cf-tunnel-accel env` + 读取域名清单 |

cloudflared 安装方式（`CFA_CFD_METHOD=auto|repo|binary`）：

- `repo`：官方源（Debian/Ubuntu 走 `pkg.cloudflare.com` apt 源，RHEL 系走 yum repo）——国内网络友好
- `binary`：GitHub Releases 单文件二进制；下载慢时可加镜像前缀，例如
  `CFA_GH_MIRROR=https://ghproxy.net/ ./install.sh -y`

默认值会**自动对齐本机现状**：已存在的 `config-*.yml`、`cloudflared-*.service`、隧道凭据会被识别，通常一路回车即可。
需要另一套隧道时用 `--tunnel-name home2`（配置/服务名会跟着变），或 `--config/--hostname/--backend` 显式指定。

### install.sh 常用参数

| 参数 | 说明 |
|---|---|
| `-y, --yes` | 不询问，全用默认值（非交互终端自动如此） |
| `--token <TOKEN>` | 写入 API Token |
| `--no-deps` | 不自动安装依赖 |
| `--service` / `--no-service` | 强制 / 禁止创建 systemd 服务 |
| `--tunnel-name <n>` | 隧道名，默认 `home` |
| `--config <path>` | 隧道配置路径 |
| `--hostname <域名>` / `--backend <ip:port>` | 生成配置时写入的 hostname / 本地服务 |
| `--dry-run` | 只打印动作 |

## 首次配置

1. **API Token**（Cloudflare Dashboard → My Profile → API Tokens），权限至少：
   - `Zone:Read`（读域名清单）
   - `DNS:Edit`（写业务域名解析与验证记录）
   - `SSL and Certificates:Edit`（SaaS 自定义主机名 / 证书）
   写入方式任选：
   ```bash
   (umask 077; echo '<token>' > /root/.config/cf-tunnel-accel/token)
   # 或
   export CF_API_TOKEN=<token>
   ```
2. **cloudflared tunnel**（每台服务器一次）：
   ```bash
   cloudflared tunnel login
   cloudflared tunnel create home            # 生成 <TUNNEL_ID>.json 凭据
   cloudflared tunnel route dns home origin.<辅助域>
   ```
   上面两步（`login` / `create`）需要浏览器，必须在交互式终端里做。完成后再跑一次
   `./install.sh --service`，install.sh 会自动识别凭据、生成 `config-home.yml` 和 systemd 服务，
   并校验 ingress；也可以自己写配置，install.sh 只负责建/修复服务。
3. **核对配置**：`cf-tunnel-accel env`，再 `cf-tunnel-accel domains` 确认能读到域名清单。
4. 试试加速一个服务：`cf-tunnel-accel add` → 从列表里挑域名 → 填 `127.0.0.1:8080` → 自动验收。

## 多台服务器部署

同一份脚本可以在多台机器上用，**基础设施参数全部支持环境变量覆盖**，不用改代码。

优先级：**环境变量 > `CF_TUNNEL_ACCEL_ENV_FILE` 指定的配置文件（默认 `/root/.config/cf-tunnel-accel/env`） > 脚本内置默认值**。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `CF_TUNNEL_ACCEL_API_TOKEN` / `CF_API_TOKEN` | 空 | API Token |
| `CF_TUNNEL_ACCEL_TOKEN_FILE` | `/root/.config/cf-tunnel-accel/token` | Token 文件 |
| `CF_TUNNEL_ACCEL_TUNNEL_ID` | 内置 | 隧道 UUID |
| `CF_TUNNEL_ACCEL_TUNNEL_NAME` | `home` | 隧道名（菜单显示） |
| `CF_TUNNEL_ACCEL_CONFIG` | `/root/.cloudflared/config-home.yml` | 隧道配置文件 |
| `CF_TUNNEL_ACCEL_SERVICE` | `cloudflared-home.service` | systemd 服务名 |
| `CF_TUNNEL_ACCEL_CLOUDFLARED` | `/usr/local/bin/cloudflared` | 二进制路径 |
| `CF_TUNNEL_ACCEL_AUX_ZONE` / `_AUX_ZONE_ID` | 内置 | 辅助域（SaaS 提供方） |
| `CF_TUNNEL_ACCEL_FALLBACK_ORIGIN` / `_PREFERRED_ENTRY` | `origin./cdn.<辅助域>` | 回退源 / 优选入口 |
| `CF_TUNNEL_ACCEL_PREFERRED_TARGET` / `_CDN_CANDIDATES` | 内置 | 默认优选目标 / 候选（逗号分隔） |
| `CF_TUNNEL_ACCEL_KNOWN_ZONES` | 内置 | `a.com:<zone_id>,b.com:<zone_id>`，离线兜底 zone 表 |
| `CF_TUNNEL_ACCEL_ZONE_CACHE` / `_ZONE_CACHE_TTL` | `/root/.config/cf-tunnel-accel/zones.json` / `21600` | 域名清单缓存与过期秒数（0=每次刷新） |
| `CF_TUNNEL_ACCEL_ENV_FILE` | `/root/.config/cf-tunnel-accel/env` | 配置覆盖文件位置 |

**换服务器的两种常见做法**

- **新服务器用新隧道（推荐）**：`cloudflared tunnel create home2`，然后写 `/root/.config/cf-tunnel-accel/env`：
  ```
  CF_TUNNEL_ACCEL_TUNNEL_ID=<新隧道UUID>
  CF_TUNNEL_ACCEL_TUNNEL_NAME=home2
  CF_TUNNEL_ACCEL_CONFIG=/root/.cloudflared/config-home2.yml
  CF_TUNNEL_ACCEL_SERVICE=cloudflared-home2.service
  ```
  注意：**同一台服务器上不同隧道的 ingress hostname 不要重复**，同一个 hostname 只能指向一个后端。
- **只在新服务器上跑优选调度**：装好脚本 + Token，`cf-tunnel-accel cdn best` 或 `schedule install 6`。此时不需要隧道，`add/health` 会因缺少本地服务报错，属预期。

**在新服务器上取代码**（仓库是私有的，需要先有凭据，三选一）

```bash
# 1) 装了 gh 并登录（最省事）
gh auth login && gh repo clone zzhjj/cf-tunnel-accel

# 2) 用 PAT（只勾 repo 权限）
git clone https://<user>:<PAT>@github.com/zzhjj/cf-tunnel-accel.git

# 3) 该机器有 ssh key 时用部署密钥（repo → Settings → Deploy keys，只读即可）
git clone git@github.com:zzhjj/cf-tunnel-accel.git

# 国内机器需要代理时
git config --global http.proxy http://127.0.0.1:7890
```

## 目录与文件

| 路径 | 说明 |
|---|---|
| `/root/bin/cf-tunnel-accel` | 脚本本体（`/usr/local/bin/cf-tunnel-accel` 是软链） |
| `/root/.config/cf-tunnel-accel/token` | API Token（600，不入库） |
| `/root/.config/cf-tunnel-accel/env` | 可选配置覆盖文件 |
| `/root/.config/cf-tunnel-accel/zones.json` | 域名清单缓存（600，只有 zone 名称/ID/状态，无凭据） |
| `/root/.cloudflared/config-*.yml` | 隧道配置（含凭据文件名引用） |
| `/run/cf-tunnel-accel.lock` | 写操作互斥锁 |

## 测试

```bash
python3 -m pytest tests -q      # CLI 逻辑（离线，23 项）
python3 -m pytest ui/tests -q   # 控制台安全边界、CDN 换区、任务语义（离线，22 项）

python3 tests/test_cf_tunnel_accel.py        # 全离线，不碰真实账号
CF_TUNNEL_ACCEL_SCRIPT=/path/to/script python3 tests/test_cf_tunnel_accel.py
```

## 安全说明

- Token 只从环境变量 / 权限 600 的文件读取，**不写进脚本、不进仓库**（`.gitignore` 已排除 `token`、`env`、`zones.json`、`*.bak*`、凭据/证书文件）。
- `zones.json` 只缓存 zone 名称、ID、状态、套餐等非敏感元数据。
- 需要 root：会读写 `/root/.cloudflared`、`/etc/systemd/system` 并重启隧道服务。

## 排错

- `Token 检查失败` → 确认 Token 有效且含上述三项权限；`cf-tunnel-accel env` 看读的是哪个来源。
- `找不到域名 X 所属 Cloudflare zone` → 该域名不在本账号，用 `add ... --external` 走手动解析。
- 新增时报「找不到 SaaS 自定义主机名」→ 辅助域未配置 fallback origin，检查 `health` 输出里的 `SaaS fallback origin` 一行。
- 访问 502 → `health <域名> --https` 看是本地后端没起，还是 SaaS/证书未 active。
- `隧道配置不存在: /root/.cloudflared/config-home.yml`（新服务器上跑菜单 2/3/4 最常遇到）→ 这台机器还没有隧道配置。三种处理：
  1. 这台机器要跑隧道：`cloudflared tunnel login && cloudflared tunnel create <名字>` 后执行 `./install.sh --service`，install.sh 会自动生成配置和 systemd 服务；
  2. 配置放在别处：`export CF_TUNNEL_ACCEL_CONFIG=/实际/路径.yml`（写进 `~/.config/cf-tunnel-accel/env` 可永久生效）；
  3. 只想看云端信息：`list`/`status`/`health` 会跳过本地隧道部分，SaaS 与 DNS 依旧会显示。
  `add` 在配置缺失时会**直接失败且不改动云端**，属于预期行为。
- `新 Tunnel 配置未能生效，已自动回滚: 命令超时（60s）: systemctl restart ...` → cloudflared 收到 SIGTERM 后可能卡在退出流程不返回，systemd 要等 `TimeoutStopSec`（默认 90s）到点才 SIGKILL，`systemctl restart` 因此可能超过 60s，旧版脚本把它误判成失败并回滚。修复三件套：
  1. 隧道配置里加 `grace-period: 5s`（脚本日常写入时会自动补齐，`health` 缺这项会 WARN）；
  2. systemd 单元里加 `TimeoutStopSec=20`（`install.sh` 生成的模板已包含，老机器手动补一行 + `systemctl daemon-reload`）；
  3. 1.3.1 起脚本把「重启命令超时 / 非 0 退出」只当提示，成败由随后的 `is-active` 轮询 + `MainPID` 是否变化判定，只有进程没换才回滚。
- `health` 里一批域名全是 `FAIL DNS 检查: 读取 DNS 失败: [10000] Authentication error` → 那些域名所属 zone 已不在本账号（典型：zone 迁到别的账号，本地只剩 SaaS 自定义主机名）。1.3.1 起刷新 zone 清单会清掉账号里读不到的 zone，这类域名退化成 `INFO 域名不在本账号 zone 下，跳过 DNS 检查`，不再计入 FAIL。
