# CLI 配置选择与新增向导：修复验收

- 在实际 CLI 的第一次配置/凭据解析前处理全局前缀 `--profile ID`、`--profiles PATH`（也支持 `--key=value`）。无显式 ID 时可读取本地 env 的 `CF_TUNNEL_ACCEL_PROFILE`，registry 默认配置目录中的 `profiles.json`，可用 `CF_TUNNEL_ACCEL_PROFILES` 改路径。显式 ID 优先；registry 路径并不隐式选择首个 profile。标记和注册表只由管理员在本机配置，公开代码不得含个人数据。
- 使用 UI 同一注册表和凭据来源组语义；只读取所选 profile 的 env/凭据。未知、重复、缺参数、坏 registry/env 非零退出，不读取默认或其他 profile 的凭据，不静默回落。`ui --profiles` 的参数仍属于 UI；非主程序导入 CLI 不解析宿主 `sys.argv`。
- 本地预检与联网核验分开。实际 `add` 入口在锁、Token 联网验证和第一个输入之前验证辅助域/入口/Tunnel/配置对应关系；直接新增向导也同样预检。读域名清单、帮助、版本、环境页面保持可用。
- 新增流程在任何 DNS/SaaS/ingress 更改前确认优选已有 CNAME 或明确目标；没有目标或读取失败零写入，已存在 CNAME 时不要求填写默认目标。配置失败提示实际 env/profile 与恢复命令，不打印空的成功链路。
- 验证：干净 HOME 的安装脚手架 + fixture Token 启动新增不出现任何输入；显式/默认 profile 选择、错误配置负向控制；真实本机 CLI 只读确认配置与 route 数据；完整 Python/HTTP/浏览器/Docker 回归与 CI。当前业务请求不自动新增域名、不重启 Tunnel、不写 DNS。
