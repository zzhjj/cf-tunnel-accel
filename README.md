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

外部域名（不在本账号、解析要手动加）也能用：`add x.yourdomain.com 127.0.0.1:8080 --external`，脚本会打印需要手动添加的 ownership / DCV / ACME 记录。

## 依赖

- `python3`（仅标准库，无第三方包）
- `cloudflared`（默认 `/usr/local/bin/cloudflared`）
- `curl`、`dig`（验收与测速）
- `systemd`（隧道服务与定时优选）

## 安装

```bash
git clone https://github.com/<you>/cf-tunnel-accel.git
cd cf-tunnel-accel
./install.sh                  # 安装到 /root/bin + 软链到 /usr/local/bin
./install.sh <CF_API_TOKEN>   # 顺带写入 Token（权限 600）
```

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
   把隧道写入 `/root/.cloudflared/config-home.yml`（`tunnel:`、`credentials-file:`、`ingress:`），并创建 systemd 服务（默认名 `cloudflared-home.service`）。
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
