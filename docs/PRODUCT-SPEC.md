# 通用产品化与功能型控制台验收规格

状态：锁定的验收基线与完成闸门；实际验证结果只以对应提交的测试记录和发布说明为准。

## 目标与支持边界

- 让其他用户无需修改源码即可配置自己的 Cloudflare Tunnel + 双域名 SaaS 优选入口。
- 这是 **Cloudflare for SaaS 辅助域 + DNS-only CNAME 候选线路选择**，不是自动改变 Cloudflare Anycast 路由、隧道传输协议或保证所有地区提速的工具。
- 支持 Linux / Python 3.9+；CLI 与前台 UI 可在普通用户目录运行。自动安装依赖、系统级 Tunnel/UI/timer 单元需 root + 可用 systemd。无 systemd 时明确降级为前台，不假装已启动服务。
- 保留标准库实现和既有 CLI/API；保持已存在配置文件、显式环境变量、default/profile 切换、origin 抢占保护、失败回滚与访问认证。

## 锁定的非目标

- 不自动开户、创建 Cloudflare 账户或接管未明确配置的域名/隧道。
- 不把 macOS/Windows/OpenWrt/所有容器宣称为完整部署已验证。
- 不引入前端构建链、复杂 IPC/multi-worker 架构、动画拓扑或营销首页。
- 本轮不修改任何已运行 Tunnel 的配置、DNS/SaaS 或 restart 它们；开发在独立 Git worktree，云端写流程仅使用本地 HTTP contract server 验证。
- 外部域名手动 DNS 继续使用明确标注的 CLI 流程；UI 不提交需要 stdin 的任务。
- 不把多阶段 Cloudflare 写操作宣称为跨服务事务；失败显示实际状态和可能的部分修改。

## 验收 1：通用配置与凭据

1. 干净 HOME、无 env 时：个人 account/zone/tunnel/domain/端口 seed 为零，不读取 `/root` 个人路径；配置/缓存/锁/UI 状态跟随 HOME/XDG 或显式路径。
2. 账户、Tunnel UUID、辅助域/zone、config/service 有明确配置来源；缺必需项在首次写入前失败，不向空路径/别人的基础设施写入。
3. 选定配置可以由 `init` 在本机生成（交互或显式参数），不需要改代码；`env` 默认纯本地且不泄露凭据；帮助/版本/UI/离线清单/显式候选网络测速不以 Cloudflare Token 为前置条件。
4. profile 显式 Token/alias/Token-file 优先于宿主继承凭据；同名配置优先级和凭据来源组分别覆盖负向用例。
5. 显式 account 过滤 zone 清单与解析；缓存标注账户/配置作用域，错作用域不导入；成功空刷新写入空状态，重载不恢复旧 zone。
6. 既有本机部署通过完整显式 env 保持兼容，不靠恢复公共源码里的个人默认值；本机档案不进仓库。

## 验收 2：安装、更新和调度

1. `--dry-run` 零 mkdir/chmod/文件写/下载/包安装/systemctl mutation。
2. 显式目录、用户安装模式、非标准 cwd/PATH、amd64/arm64/不支持架构、无 systemd、缺 Python 等有可复现结果。
3. 不随机选“最新 config / 第一个 service / 第一个 JSON 凭据”；明确选择绑定到同一 Tunnel/config/service，歧义报错。
4. 默认安装不重写或 restart 既有 Tunnel；服务管理需显式参数。配置校验先于启用；失败保留旧安装/单元并退出非零。
5. CLI+完整 UI 成套安装；调度保留实际 env-file/CLI 路径，timer Unit 绑定自定义 service basename；不把 Token 写进 unit。

## 验收 3：优选与配置保真

1. CLI/UI 共用 HTTPS eligible 判定；TCP-only、curl 非零、无效/NaN 延迟、业务 5xx 不自动选择，`--force` 不越过 eligibility。
2. 有业务验证 hostname 时，使用业务 Host/SNI 固定候选 IP，而不是只测候选自己的网站；区分“网络测速”与“业务验证”。自动切换需要业务验证，全部候选失败零 DNS 写入。
3. 可用性恢复优先于延迟防抖；同等可用性下低于改善阈值不切换；写后 API/链路确认失败恢复原 CNAME，并分别报告恢复结果。
4. HTTPS 验收看最终响应及 curl exit，301→502 / exit 60 为失败；验证成功的 HTTPS 后端不因 `noTLSVerify=false` 被强制标失败。
5. 双栈均有采样；候选长度/并发有上界；候选无配置时明确提示而非用个人列表。
6. ingress 支持普通 block YAML（包含常见 indentless sequence），保留注释/无关 originRequest 配置；复杂/歧义结构写前明确拒绝，零文件修改。自定义 origin 路径不再注入共享 fallback 或固定个人端口。

## 验收 4：控制面可靠性

1. UI mutation 参加 CLI 文件锁；已有锁时零副作用；profile 审计明确归属。
2. 日志游标单调，超过保留窗口仍收到新行并报告省略；任务列表及刷新/断联重新连接可用。
3. health `result.ok=false` 显示“执行结束，检查未通过”，不显示成功；失败任务也刷新状态，不用空/默认值冒充已确认信息。
4. worker 有公开 close/drain；停止时不以线程 `SystemExit` 退出；关闭后拒绝新提交。测试把线程警告当错误。
5. 环境诊断不泄露 Token；缺 config/dependencies/API 权限在 UI 有具体原因与恢复步骤。请求、路由及认证原有负向控制不回退。

## 验收 5：UI 重构

- 路由为默认页；导航覆盖 路由 / 本机服务 / 优选 / 诊断 / 任务审计 / 环境。
- 轻量浅色管理台、系统无衬线字体、白色面板、少量蓝色强调、清楚的绿/黄/红状态文字；去巨大英文 hero、grain、跟随光标、ticker/marquee、Canvas 动画。
- profile 常驻且有效偏好保留；切换丢弃旧响应，任务仍有全局归属；无审计/CDN 时其它区域可用。
- 表单显式标签、校验/错误贴近输入、失败保留数据；delete/CDN 更改展示影响并确认；键盘焦点进入/约束/Esc/返回。
- 请求有超时、401 恢复登录、429 等待提示；断联任务为“状态待同步”，不假报失败或重放写请求。
- 320/375/768/1440 px 可操作，无整页横向溢出；触控目标至少 44 px，空/加载/未探测/失败不同状态。

## 预先约定的测试边界（seams）

- CLI subprocess（独立 HOME/ENV/cwd）、init/env/CDN 公共函数；外部 Cloudflare/curl/systemd 边界以本地 contract server/命令 fixture 控制。
- installer subprocess +临时目录/PATH/无 systemd fixture，实测文件差异及退出码。
- 实际 loopback HTTP server 的认证/任务/profile/environment 接口。
- 实际 Chromium 对完整后端与静态资源：加载/切换/搜索/提交/任务/错误/重试/移动/键盘。
- 已有 AST guard 仅作为架构报警，不当作账户隔离或运行期无问题的充分证明；行为测试独立验证凭据/缓存/文件锁。

## 交付闸门

1. 原始基线 `19e9f45` 的 92 项测试通过但有 14 条未处理线程警告；必须修根因，不能滤掉警告。
2. 新行为先红后绿，关键误判包含独立负向 fixture；完整 Python/JS/安装器检查通过，无秘密/运行数据进入 Git。
3. 清理公开文档的本机实例、个人账号/域名/PID，给出从空机器配置至可用控制台的完整步骤及真实限制。
4. 本地 E2E 与 GitHub Python matrix 通过；只报告实际完成部分，任何未完成闸门继续推进。
5. 成套发布有可回滚备份；正式更新前复核已运行服务及 config hashes；单项异常即恢复，锁定成果不回退。
