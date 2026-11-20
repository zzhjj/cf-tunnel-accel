# Web 管理界面

Linux / Python 3.9+ 的标准库 HTTP 服务，与 CLI 共用 Cloudflare 和 Tunnel 实现。默认仅监听 `127.0.0.1:8642`；不是公开多租户平台，不提供 Token 管理或 Cloudflare onboarding。

先按[主 README](../README.md) 安装完整 CLI/UI 包并初始化本机配置；账户、SaaS、Token、Tunnel 和后端由管理员准备。[部署说明](../docs/DEPLOYMENT.md) 定义配置优先级与多 profile 边界。

## 前台启动

用户安装后：

```bash
cf-tunnel-ui --host 127.0.0.1 --port 8642
# 也可由 CLI 查找并启动 UI
cf-tunnel-accel ui
```

仓库内：

```bash
python3 ui/server.py --module "$PWD/cf-tunnel-accel" --host 127.0.0.1 --port 8642
```

包需保留相邻的 `cfa_config.py`、`cfa_ingress.py`、`cfa_cdn.py` 和完整静态资源，不要只更新 `server.py`。运行用户须能读 env/Token/Tunnel 凭据；写路由和重启 systemd 服务仍需相应权限。没有 systemd 时可前台查看 UI，但不等于所有服务管理操作可用。

浏览器访问 `http://127.0.0.1:8642/`。UI 管理凭据首次生成于配置目录的 `ui-token`（0600），与 Cloudflare API Token 不同。默认目录是 `${XDG_CONFIG_HOME:-$HOME/.config}/cf-tunnel-accel`，可用 `CF_TUNNEL_ACCEL_UI_DIR` 覆盖。启动横幅只提示凭据文件，不回显内容；需查看时，在本机受保护终端使用 `cf-tunnel-ui --print-token`，不要将输出送入共享日志、截图或 issue。`--rotate-token` 用于显式重置本机 UI 凭据，不是 API Token 管理。

## 六页路由优先布局

| 页面 | 内容与边界 |
|---|---|
| **路由**（默认） | 搜索业务域名/后端，新增、改后端、切入口、修复、诊断和删除；删除需明确确认目标 |
| **本机服务** | 监听端口、进程、映射状态和可选 HTTP 探测；发现失败或权限受限不等于本机没有服务 |
| **优选** | 当前共享入口、候选、network-only 与业务 Host/SNI 验证、测速与确认切换；不保证提速 |
| **诊断** | 指定业务域名或全部检查，输出检查结果；任务结束不代表健康检查通过 |
| **任务审计** | 全局任务、归属 profile、日志、结果、错误和审计记录；不是云端事务日志 |
| **环境** | 仅本地检查路径、依赖、就绪状态和凭据来源，不显示凭据内容；不在此开户或录入 API Token |

旧的拓扑画布、印刷风版式或自动外部 DNS 接入不属于当前界面说明。

### 错误、部分数据与任务恢复

- API、配置、zone、SaaS、DNS 或服务读取失败应显示具体分区错误/未知状态；其它可读部分仍可展示。空列表、过期快照、未知和健康状态不是同一件事。
- 界面显示当前 profile、更新时点与任务状态；切换 profile 后不应让旧请求覆盖新视图。优先检查当前配置，再判断是否需要变更。
- 写任务全局单写准入；冲突可返回 409，不静默并行重启 Tunnel。前端等待/断线不意味着任务未提交，不重复点击来“补执行”。
- 诊断可以执行完但 `unhealthy`；变更返回失败也可能已经写了部分 DNS/SaaS/本地配置。以结果、日志及实际状态核对，不用“进度结束”替代成功判断。
- 最近任务快照保存在 `ui-jobs.json`，有记录/日志容量上限，不是永久历史。UI 重启后未确认的 queued/running 任务标记 `interrupted`，**不自动重放、不从快照重新派发**。先检查目标资源，再人工决定下一步。
- 任务存储或审计失败会削弱追踪能力，应处理存储错误；持久化不等于所有云端步骤具有持久事务。服务停止、代码回滚也不会撤销已经发生的外部变更。

### CDN 与外部 DNS

UI 调用与 CLI 相同的 `cfa_cdn.py` 策略。业务验证/样本门槛、自动优选和手动切换的区别、恢复边界详见[主 README](../README.md#cdn-测速不等于业务验证)。目标显示“已验证”仅指 API 已确认的入口值；不等同所有业务域名或地区均已验收。

测速分清网络样本与业务验证：候选自身 HTTPS、TCP、ping 只说明当前网络测量；自动 `best` 必须配置真实 `CF_TUNNEL_ACCEL_PROBE_HOSTNAME`，使用业务 Host/SNI、至少 2 个健康 HTTPS 样本且成功率 ≥80%。`force` 只跳过改善阈值，不能绕过有效性。当前健康码含 2xx–4xx，并非业务功能断言。显式切换仍需检查验证和有界回滚结果。

相同 `PREFERRED_ENTRY` 的所有业务域名受切换影响；profile 不隔离共享 DNS。API 回读成功不代表各地缓存已刷新，失败恢复也可能失败。

**外部手工 DNS 只走交互式 CLI**，UI 只提供说明，不提交需要 stdin 确认的外部 add 任务：

```bash
# 显式选择与页面相同的 env，并核对进程环境没有意外覆盖
CF_TUNNEL_ACCEL_ENV_FILE=/etc/cf-tunnel-accel/alpha/env cf-tunnel-accel env
CF_TUNNEL_ACCEL_ENV_FILE=/etc/cf-tunnel-accel/alpha/env \
  cf-tunnel-accel add app.example.net http://127.0.0.1:8080 --external
```

按终端提示在域名的 DNS 平台添加 ownership/DCV/ACME 与 DNS-only CNAME。外部 DNS 切换/删除同样需要人工处理。没有跨 Tunnel/SaaS/DNS 的统一事务，中断后不能盲目重放。

## 可选 systemd 服务

当前安装器安装完整 UI，可选生成 unit；不再要求另拷一份 UI 到个人目录：

```bash
sudo bash ./install.sh --source-dir "$PWD" --no-deps --ui-service
```

这要求 root 系统安装和运行中的 systemd；不能与 `--user` 混用，无 systemd 时跳过 unit。默认单元 `cf-tunnel-ui.service`，可用 `--ui-service-name` 覆盖。**安装器不 enable/start**，管理员核对生成单元的包/配置路径、运行用户和监听后，才显式执行：

```bash
sudo systemctl enable --now cf-tunnel-ui.service
sudo systemctl is-active cf-tunnel-ui.service
```

仓库的 `cf-tunnel-ui.service` 是手动部署模板，若选用须替换 `@UI_DIR@` 并检查 `ExecStart`、模块、env 与运行身份；不要把模板内容当作任何机器的实际配置。unit 不通过 shell 展开 `$HOME` / `$PWD`，使用自己的绝对路径。unit/drop-in 变化后需 `daemon-reload` 再重启；仅更新包文件也要重启以加载新 Python。

多 profile 可在 UI unit 的 drop-in 配置 `Environment=CF_TUNNEL_ACCEL_UI_PROFILES=/etc/cf-tunnel-accel/profiles.json`。与启动器固定 env 路径保持一致，不继承不期望的父进程 Token。格式、凭据来源组和缓存分离见[多 profile 部署](../docs/DEPLOYMENT.md#多-profile-注册表)。

## 远程访问

### SSH 转发（推荐）

在操作员电脑运行并保持会话：

```bash
ssh -N -L 127.0.0.1:8642:127.0.0.1:8642 operator@server.example.net
```

本地浏览器访问 `http://127.0.0.1:8642/`；服务器无需开放 8642。端口冲突时只改转发的第一个端口和浏览器访问端口。

### TLS 反向代理

同机代理仍连接 `http://127.0.0.1:8642`，只对外提供 HTTPS。示例 nginx 片段需替换证书路径：

```nginx
server {
    listen 443 ssl;
    server_name console.example.net;
    ssl_certificate /etc/ssl/console/fullchain.pem;
    ssl_certificate_key /etc/ssl/console/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8642;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

保留浏览器 Origin、Host/端口和 CSRF 头/cookie，不通过关闭检查来解决 403。应用不能仅凭转发头建立客户端身份或 TLS 信任；代理后的限流可能共享代理 IP，应在代理侧另设访问控制与限流。

非回环绑定需要显式 `--allow-remote-http`，否则启动拒绝；参数本身**不启用 TLS**。只有确实需要不同主机/容器上游时才使用，并在防火墙/容器网络限制仅受信 TLS 代理可达。不要直接公开明文管理端口。HTTP 标准库服务不是公网防护层。

## 配置与维护

- 单实例读取 CLI 默认 HOME/XDG env；多 profile 使用 `--profiles /absolute/profiles.json` 或 UI 目录自动加载注册表。显式错误注册表不回退默认。
- 配置目录内 `ui-token`、`ui-audit.log`、`ui-jobs.json` 和按 profile 分键的 CDN 历史都是管理数据，备份和日志分享须脱敏并限制权限。
- 多 profile 是同进程模块，不是进程沙箱或管理员权限隔离；AST guard 不能证明完整隔离。
- 无 Token UI、无自动开户/onboarding、无跨云事务。源码测试、mock HTTP 和浏览器测试不证明真实 Cloudflare 开通或公网表现。
- 升级、旧 root 配置迁移、等待任务、备份和恢复见[维护手册](../docs/MAINTENANCE.md)；检查要求见[维护规范](../docs/MAINTENANCE-SPEC.md)。
