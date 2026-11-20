# 登录密钥与 Docker：验收规格

## 登录密钥

- 控制台全局只用一个 UI 密钥，与 Cloudflare API Token、Tunnel 凭据分开。显式来源为 `CF_TUNNEL_ACCEL_UI_TOKEN`，或 `--token-file PATH` / `CF_TUNNEL_ACCEL_UI_TOKEN_FILE`（命令行优先选择文件路径）；同时给出值和文件时报错。显式文件只读，缺失、空白或非法时失败，不回退。自定义值与已有密钥至少 32 个字符，须为单行可打印文本。
- 未配置显式来源时，复用现有 `ui-token`，或安全随机生成并以 0600 原子落盘。端口绑定失败、无效 profile 不消耗首次生成机会；成功首次启动的本地 stdout 只打印一次。重启不打印；可关闭 bootstrap 日志（`--no-print-initial-token` / `CF_TUNNEL_ACCEL_UI_PRINT_INITIAL_TOKEN=0`）。`--print-token` 是显式本地读取，仍兼容初始化缺失的默认托管密钥。`--rotate-token` 只适用于本地托管密钥，替换失败保留原文件。
- 使用真实进程和 HTTP 验证新密钥登录、重启保持、旧密钥拒绝、只读文件、自定义值与坏配置。测试日志仅输出判定，不输出有效密钥。

## Docker 运行边界

- Dockerfile、Compose 与只读配置模板；使用普通用户、桥接网络、回环端口发布、只读根文件系统和持久卷，无宿主 Docker/socket/PID/systemd 挂载。匿名健康检查仅说明控制台可响应，不表示 Tunnel/Cloudflare 健康。
- 容器管理模式保留多 profile、Cloudflare 读取、后端/公网诊断、测速、SaaS/优选 DNS 操作。`add/update/delete` 依赖本地文件写入及 Tunnel 重启，必须在 API 入队之前明确拒绝，UI 禁用对应控件。宿主监听和 Tunnel 进程状态为未管理/未知，非故障。Docker 诊断注明探测视角、略过宿主 systemd/cloudflared 校验，部分检查不是完整健康结论。
- 从干净配置启动 Compose，测试真实 Engine、HTTP 会话、重建保留密钥和审计、挂载密钥、拒绝写 ingress、与隔离的 API 合约端点交互。构建上下文仅允许运行文件，测试和 Git 跟踪内容不含主机数据与密钥。

## 非目标

不接管或重启当前服务；不运行特权容器或 Docker-in-Docker；不创建 Cloudflare 资源用于测试；不自动部署 cloudflared connector；不修改既有 Git 公开历史。测试平台与真实外部系统的验证边界分别说明。
