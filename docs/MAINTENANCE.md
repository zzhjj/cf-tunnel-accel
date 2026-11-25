# 维护、升级与回滚

适用于自己的 Linux 部署，不包含任何公共示例机器的实际值。入口和配置见 [README](../README.md)、[部署说明](DEPLOYMENT.md)，UI 访问见 [UI README](../ui/README.md)，发布检查标准见 [MAINTENANCE-SPEC](MAINTENANCE-SPEC.md)。

## 变更前：先固定目标与备份

1. 本地确认包版本、启动器指向、执行用户、HOME/XDG、所选 env、Tunnel config、systemd service，以及 profile 注册表。2.2.1 用 `cf-tunnel-accel --profile alpha env` 明确核对注册 profile；registry 不在默认配置目录时加命令前的 `--profiles PATH`。`env` 是本地来源检查；输出仍含账户/zone/path 元数据，不原样粘贴公共 issue。
2. 等待写任务完成，暂停新的管理请求和相关自动优选 timer。必要时停止 UI，但不要把停止 UI 当作撤销 Cloudflare 变更。
3. 备份**成套包**：`cf-tunnel-accel`、`cfa_config.py`、`cfa_ingress.py`、`cfa_cdn.py`、完整 UI/静态资源与启动器；同时备份 env、registry、API/UI Token、Tunnel 配置/JSON 凭据、缓存、任务记录、审计、相关 unit/drop-in。
4. 备份目录限制为 0700，凭据/配置文件 0600，保留所有权；按运维策略加密并验证恢复方法。备份、真实 env、凭据和日志不进入仓库或共享测试 artifact。unit 也可能含敏感环境变量。
5. 私下记录云端 DNS/SaaS 的当前归属及入口目标，用于逐项恢复。备份文件不等于云端资源快照，不公开账户、zone、Tunnel 标识或实际服务详情。

## 2.2.1：CLI profile 选择与新增预检

升级后 CLI 支持命令前的全局 `--profile ID` / `--profiles PATH`，也支持 `--profile=ID` / `--profiles=PATH`。选择先于设置/凭据读取，显式命令行优先于进程环境，再优先于默认本机 env 的 `CF_TUNNEL_ACCEL_PROFILE` / `CF_TUNNEL_ACCEL_PROFILES` marker；进程指定 `CF_TUNNEL_ACCEL_ENV_FILE` 时从该文件读取 marker。registry 未指定时为 `config_dir/profiles.json`。仅指定 registry 路径而无 ID/marker 会报错，不选首个 profile。

```bash
cf-tunnel-accel --profile alpha env
cf-tunnel-accel --profiles /etc/cf-tunnel-accel/profiles.json --profile beta env
cf-tunnel-accel --profile=beta --profiles=/etc/cf-tunnel-accel/profiles.json env
```

先确认注册表和各 profile 的私有 env 已就绪，再在默认本机 env 加入常用选择：

```ini
CF_TUNNEL_ACCEL_PROFILE=alpha
# 可选：registry 不在默认配置目录时指定
CF_TUNNEL_ACCEL_PROFILES=/etc/cf-tunnel-accel/profiles.json
```

**marker 不入 Git**，实际 registry、env 与凭据同样仅保存在本机；公开文档只用 `alpha` / `beta`、`example.net` 等占位示例。显式 `--profile beta` 覆盖 `alpha` marker。无 marker/全局 flags 时，裸 CLI 仍使用默认 env，即使 UI 已加载多 profile。UI 保持 `cf-tunnel-accel ui --profiles /etc/cf-tunnel-accel/profiles.json`（参数在 `ui` 后）及原选择器，与终端 CLI 不联动。未知 ID、重复 flags、参数缺失、坏 registry/所选 env 均报错，不静默读取其它 profile 的配置/凭据。完整覆盖规则见[部署说明](DEPLOYMENT.md#多-profile-注册表)。

新增失败先区分两个阶段，避免反复进入向导或盲目重试：

- **本地配置阶段**：`main add` / 无参数入口，以及直接交互新增，都在锁、网络和第一次输入前检查 `AUX_ZONE`、入口/origin、Tunnel UUID 与本地 config。错误给出实际 env、当前 profile 和 `init` / `--profile alpha env` 恢复方向。新用户先准备 Token、已有 Tunnel 配置/凭据，再 `init` 只填 SaaS `AUX_ZONE` 并接受默认服务；origin/cdn 从辅助 zone 派生。业务 zone 管理业务 DNS，SaaS 辅助 zone 承载 origin/入口，二者不是同一个配置角色。
- **入口目标阶段**：新增在任何 DNS 写之前严格预检 preferred target，先于 origin/SaaS/ingress 更改。已有有效 CNAME 时沿用，不要求默认目标；确认不存在时需要明确的 `PREFERRED_TARGET` 或先执行 `cdn set <目标域名>`。读取失败直接停止，不 fallback、不把现有目标改回默认值。此预检失败尚未写入；后续阶段失败仍可能留下部分变更。`cdn set` 本身会写 DNS，应单独核对后执行。

## 旧 root 安装迁移：必须先显式配置

旧版本可能把某台服务器的账户、zone、Tunnel、个人目录或候选入口固化在脚本里。新版不承诺保留这些值，也不自动从服务器上选择其它配置或凭据。**先为现有部署写好显式配置，再升级**：

- 确认安装后使用的 `CF_TUNNEL_ACCEL_ENV_FILE`，不要假设 sudo 会保留当前用户的 HOME 或环境。
- 将自己的 `ACCOUNT_ID`、`AUX_ZONE`、`AUX_ZONE_ID`、`TUNNEL_ID`、`CONFIG`、`SERVICE` 显式保存到该私有 env；同时记录 `TOKEN_FILE`、`FALLBACK_ORIGIN`、`PREFERRED_ENTRY`、`PREFERRED_TARGET`、`CDN_CANDIDATES`、`PROBE_HOSTNAME`，如使用独立源站还要 `CUSTOM_ORIGIN`。
- 使用自己的真实**绝对路径**，env 不展开 `$HOME`。本文件不打印迁移值；[env.example](env.example) 仅说明键名和占位格式。
- Token 推荐在单独的 0600 文件，移除不期望的继承内联 Token/别名；来源组规则见[部署说明](DEPLOYMENT.md#配置优先级与凭据来源组)。不同账户/profile 选择不同凭据和缓存。
- 检查配置 UUID、凭据和服务 ExecStart 相互匹配，辅助 zone 属于所选账户，origin 没有指向别人的 Tunnel。不要用 `ALLOW_ORIGIN_TAKEOVER=1` 绕过归属检查来完成普通升级。
- 用候选包的 `env` 本地核对新解析结果，必要时在维护窗口另行做只读云端检查。根目录旧脚本、旧 symlink 和旧 unit 在验证恢复路径前不要删除。

用户安装和 root 安装可以有不同配置目录；改变安装方式不自动迁移云端或本地服务身份。安装器保存的启动器/env 路径与 UI unit、timer 必须一致。

## 离线验证与发布准备

在独立检出目录按 [CONTRIBUTING](../CONTRIBUTING.md) 做语法、测试和文档检查。Python 3.9+ 是兼容目标，实际执行过的版本另行记录，不能用语法检查代替运行兼容性；CI 定义见仓库 workflow，不在公共文档维护过期测试数。

发布说明应列出候选版本/提交、改动范围、实测命令和版本、未测项、配置变化、已知限制与恢复步骤。没有真实 Cloudflare 创建/证书/DNS 验收时，明确写“仅本地离线验证”；测试通过不等于生产或所有地区可用。

公共仓库已采用干净历史，维护与发布只基于当前公开基线向前推进；不要回推旧 history、合并旧个人部署分支，或把历史中的个人数据重新带入文档/提交。旧部署备份留在私有存储，迁移只写本机配置，不作为公共仓库回滚来源。根目录本机 `/profiles.json` 由 `.gitignore` 忽略，[profiles.example.json](profiles.example.json) 仍是可跟踪的通用文档模板；忽略规则不负责清除已经跟踪的数据，发布前仍需检查实际 diff 和本地文档链接。

开发验证不得读取个人 env/Token、操作现有 systemd、启动自动优选或调用真实 Cloudflare。安装器测试应使用临时 HOME/XDG、临时前缀、stub 和 `--no-deps`；网络/系统行为必须拦截，不对真实安装执行试装。

## 更新包与可选服务

1. 候选代码放在与目标不同的暂存目录，核对来源和完整 diff，保留他人在途修改。完整包必须含三个 `cfa_*.py` 辅助模块；不要新 CLI 配旧 helper，或新 server 配旧静态资源。
2. 先用与部署匹配的 `install.sh --user --no-deps --dry-run --source-dir /absolute/package`，或由管理员使用 root 系统安装参数查看计划。`--dry-run` 只检查本地包/路径计划，不安装、不下载、不改服务，不验收云端。
3. 由部署负责人执行同样参数、不带 `--dry-run` 的更新。已有 root 部署明确选择原 config/env；`--service` 须显式 `--config` 与 `--service-name`。`--ui-service` 可选生成 UI 单元；安装器不会 enable/start，也不会自动重启已经在跑的旧 Python 进程。
4. 安装器将旧包/配置/启动器/unit 保留为相邻 `.bak.*`，本地失败会尝试恢复相关文件。不要据此宣称包管理器、下载或云端有全局事务；先查看实际结果和备份，再决定下一步。
5. 若 unit/drop-in 有变，管理员执行 daemon-reload；用已核对的具体单元重启所需服务。UI 启动后检查回环监听、登录、所选 profile、六页只读数据和错误状态。SSH/TLS 代理部署还要检查 Host/Origin、任务轮询和上游端口不能被绕过访问。
6. 需要云端写验收时，单独安排维护窗口、目标与恢复计划；先检查业务 Host/SNI、SaaS/证书、DNS 和后端，不把进程 active 当成全链路成功。最后再恢复自动优选计划。

UI 重新启动通常需要重新登录；最近任务快照保留，但未完成任务标为 `interrupted`，**不会自动重放**。任务记录不保留可供自动恢复的完整事务。浏览器超时、HTTP 断线或失败结果不能说明任务没写入云端，先核对实际状态再提交下一次操作。

## 分层回滚

### 本地包/服务

1. 停止新的 UI/CLI 写请求，暂停相关 timer，等待或核对在途任务；避免旧/新实例同时管理相同资源。
2. 恢复同一版本的 CLI、三个辅助模块、UI、启动器、unit/drop-in。若配置迁移过，恢复相匹配的 env、registry 和 Tunnel config，保持访问权限。
3. unit 变化后 daemon-reload，再启动明确选择的服务。本地核对版本、配置来源、监听、登录与只读视图。
4. 不在验证恢复前删除安装器 `.bak.*`；定期按保留策略清理，它们可能含凭据或运行数据。

### DNS / SaaS / Tunnel 状态

代码回滚不会撤销已提交的 DNS/SaaS 或本地 ingress 变更。按事前记录、任务日志和权威 API 逐项对比：业务 DNS、DCV、custom hostname、fallback/custom origin、优选 CNAME、Tunnel 配置与进程。先明确哪个阶段已完成，再恢复该资源；不要把全部 add/delete/switch 盲目重放。

CDN 手动切换保存原始记录，API 确认失败时尝试恢复；自动优选使用共享业务 HTTPS 策略，切换后检查失败则恢复旧目标并复核。恢复请求、权限或 DNS 传播仍可能失败，按任务返回的失败与恢复结果检查真实记录。API 成功不代表全球缓存已更新，此机制不覆盖完整业务事务。

## 常见问题

| 现象 | 核对与处理 |
|---|---|
| CLI 导入失败或 UI 功能不匹配 | 包是否缺 `cfa_config.py` / `cfa_ingress.py` / `cfa_cdn.py`；CLI/UI/静态资源是否同一版本 |
| 配置缺失、选到错误账户 | HOME/XDG、启动器固化 env、显式 `ENV_FILE`、父进程覆盖值；用命令前的 `--profile alpha env` 核对 registry 和实际 env，不靠旧默认常量 |
| UI 有 profile，裸 CLI 却未加载 | UI 选择器不修改终端；CLI 显式 `--profile alpha env` 或在默认本机 env 设置 `CF_TUNNEL_ACCEL_PROFILE=alpha` marker；仅有 registry 不自动选首个 |
| `add` / 无参数入口未提示输入就报错 | 新的本地预检先于锁/联网/输入；按错误中的实际 env/profile 修复 AUX_ZONE、入口/origin、Tunnel config/UUID，先 `init` 或选择已有 profile |
| 新增提示优选入口未初始化或读取失败 | 确认已有 CNAME 或明确 `PREFERRED_TARGET`；缺目标时先配置或显式 `cdn set`，读错先处理权限/API，不靠默认目标 fallback |
| Token 文件已改但仍用旧凭据 | 来源组优先级和父进程内联 Token；修改配置后重启 UI 以重新加载 profile |
| zone/SaaS 读失败或视图只显示一部分 | 权限范围、账户过滤、active zone 与 SaaS 启用状态；未知/失败不是“没有资源” |
| 自动 best 不切 | 是否设置真实 `PROBE_HOSTNAME`，≥2 健康样本且 ≥80%，证书/HTTP/计时是否有效，以及改善阈值；`--force` 不能绕过有效性 |
| 自动计划与手动结果不符 | timer 的运行用户、显式 env/Token 和业务 probe；不要依赖交互 shell 的 export |
| YAML 被拒绝 | 当前仅纯块式子集，不强行重排复杂 YAML；保留原文件，人工简化后用 cloudflared 验证 |
| UI 任务失败/中断 | 查看日志和实际资源，先检查部分变更；不自动重试，任务完成也不等于 health 通过 |
| HTTPS 后端 502 | 连通性、证书名称/信任、ingress 与 noTLSVerify；跳过证书仅排除校验问题，不证明源站身份 |
| 代理写操作 403 | 外部 Host/端口、Origin、CSRF cookie/头一致性；不要禁用检查 |
| 多 profile 显示相同优选入口 | 是否有意共享辅助 zone/PREFERRED_ENTRY；profile 不隔离相同云资源 |

Profile CLI 配置在独立模块导入时固化；profile 间不共享运行期环境字典。任务/审计/session、单 worker、锁及相同 DNS/SaaS/cloud 资源仍共享。
