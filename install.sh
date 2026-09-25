#!/usr/bin/env bash
# cf-tunnel-accel 安装脚本
#
#   ./install.sh                 # 只装脚本
#   ./install.sh <api-token>     # 装脚本并写入 Cloudflare API Token
#
# 可用环境变量覆盖安装位置：
#   CFA_BIN_DIR    默认 /root/bin
#   CFA_LINK_DIR   默认 /usr/local/bin
#   CFA_CONFIG_DIR 默认 /root/.config/cf-tunnel-accel
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${CFA_BIN_DIR:-/root/bin}"
LINK_DIR="${CFA_LINK_DIR:-/usr/local/bin}"
CONFIG_DIR="${CFA_CONFIG_DIR:-/root/.config/cf-tunnel-accel}"
TOKEN_FILE="$CONFIG_DIR/token"
ENV_FILE="$CONFIG_DIR/env"

if [ "$(id -u)" != "0" ]; then
  echo "错误: 需要 root 权限（脚本会读写 /root/.cloudflared 与 systemd 单元）" >&2
  exit 1
fi

echo "==> 安装目录"
mkdir -p "$BIN_DIR" "$LINK_DIR" "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [ -f "$BIN_DIR/cf-tunnel-accel" ]; then
  backup="$BIN_DIR/cf-tunnel-accel.bak.$(date +%Y%m%d-%H%M%S)"
  cp -p "$BIN_DIR/cf-tunnel-accel" "$backup"
  echo "    旧版本已备份: $backup"
fi
install -m 755 "$SRC_DIR/cf-tunnel-accel" "$BIN_DIR/cf-tunnel-accel"
ln -sfn "$BIN_DIR/cf-tunnel-accel" "$LINK_DIR/cf-tunnel-accel"
echo "    $LINK_DIR/cf-tunnel-accel -> $BIN_DIR/cf-tunnel-accel"

if [ -n "${1:-}" ]; then
  (umask 077; printf '%s\n' "$1" > "$TOKEN_FILE")
  echo "==> Token 已写入 $TOKEN_FILE（权限 600）"
elif [ ! -s "$TOKEN_FILE" ]; then
  echo "==> 还没有 Cloudflare API Token，二选一："
  echo "    1) 重新执行: $0 <cloudflare-api-token>"
  echo "    2) 手动写入: (umask 077; echo '<token>' > $TOKEN_FILE)"
fi

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE.example" <<'EOF'
# 复制成 env 即可生效（KEY=VALUE，已存在的环境变量优先）
# 多台服务器部署同一份脚本时，在这里覆盖基础设施参数，不用改代码。
#
# CF_TUNNEL_ACCEL_TUNNEL_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
# CF_TUNNEL_ACCEL_TUNNEL_NAME=home
# CF_TUNNEL_ACCEL_CONFIG=/root/.cloudflared/config-home.yml
# CF_TUNNEL_ACCEL_SERVICE=cloudflared-home.service
# CF_TUNNEL_ACCEL_AUX_ZONE=v50kkp.eu.org
# CF_TUNNEL_ACCEL_AUX_ZONE_ID=beaf16d441346d4b739cdb0a6b03ca9c
# CF_TUNNEL_ACCEL_PREFERRED_TARGET=cf.090227.xyz
# CF_TUNNEL_ACCEL_TOKEN_FILE=/root/.config/cf-tunnel-accel/token
EOF
  echo "==> 已生成配置模板: $ENV_FILE.example"
fi

echo "==> 依赖检查"
missing=0
for bin in python3 curl dig systemctl; do
  if command -v "$bin" >/dev/null 2>&1; then
    echo "    OK   $bin"
  else
    echo "    缺失 $bin"
    missing=1
  fi
done
if command -v cloudflared >/dev/null 2>&1; then
  echo "    OK   cloudflared ($(command -v cloudflared))"
elif [ -x /usr/local/bin/cloudflared ]; then
  echo "    OK   cloudflared (/usr/local/bin/cloudflared)"
else
  echo "    缺失 cloudflared（用 CF_TUNNEL_ACCEL_CLOUDFLARED 指向实际路径）"
  missing=1
fi
[ "$missing" = 0 ] || echo "    提示: 缺依赖时脚本仍可安装，但对应功能不可用"

echo
echo "完成。下一步:"
echo "  cf-tunnel-accel env        # 核对该服务器生效的配置"
echo "  cf-tunnel-accel domains    # 看账号里有哪些域名可用（Token 就绪后）"
echo "  cf-tunnel-accel            # 交互菜单"
