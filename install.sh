#!/usr/bin/env bash
# cf-tunnel-accel 一体化安装脚本
#
#   ./install.sh                      # 交互式（每步都有默认值，直接回车即可）
#   ./install.sh -y                   # 全部用默认值，不询问（无人值守）
#   ./install.sh -y --token xxxxx     # 顺带写入 Cloudflare API Token
#   ./install.sh -y --service         # 额外创建/修复 systemd 隧道服务
#
# 主要选项：
#   -y, --yes              不问，全部用默认值（非交互终端自动进入该模式）
#       --token <TOKEN>    Cloudflare API Token（也可用位置参数）
#       --no-deps          不自动安装缺少的依赖
#       --service          创建/修复 systemd 隧道服务
#       --no-service       不碰 systemd
#       --tunnel-name <n>  隧道名（默认 home）
#       --config <path>    隧道配置文件（默认 /root/.cloudflared/config-<隧道名>.yml）
#       --hostname <域名>  写进隧道配置的 hostname（默认 origin.<辅助域>）
#       --backend <url|ip:port>  本地服务（默认 http://127.0.0.1:8080）
#       --dry-run          只打印将要执行的动作，不落盘
#   -h, --help             帮助
#
# 可用环境变量：
#   CFA_BIN_DIR / CFA_LINK_DIR / CFA_CONFIG_DIR   安装位置
#   CFA_GH_MIRROR=https://ghproxy.net/            GitHub 下载慢时的镜像前缀
#   CFA_CFD_METHOD=auto|repo|binary               cloudflared 安装方式
#   CFA_CFD_DIR=/root/.cloudflared                凭据/配置目录
#   CFA_DRY_RUN=1                                 等同 --dry-run
#   CF_TUNNEL_ACCEL_*                             与脚本一致的基础设施参数
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF_SCRIPT="$SCRIPT_DIR/cf-tunnel-accel"

# ---------------- 默认值（可用环境变量覆盖） ----------------
BIN_DIR="${CFA_BIN_DIR:-/root/bin}"
LINK_DIR="${CFA_LINK_DIR:-/usr/local/bin}"
CONFIG_DIR="${CFA_CONFIG_DIR:-/root/.config/cf-tunnel-accel}"
TOKEN_FILE="${CF_TUNNEL_ACCEL_TOKEN_FILE:-$CONFIG_DIR/token}"
ENV_FILE="${CF_TUNNEL_ACCEL_ENV_FILE:-$CONFIG_DIR/env}"
CLOUDFLARED_BIN="${CF_TUNNEL_ACCEL_CLOUDFLARED:-/usr/local/bin/cloudflared}"
CLOUDFLARED_DIR="${CFA_CFD_DIR:-/root/.cloudflared}"
CFD_METHOD="${CFA_CFD_METHOD:-auto}"       # auto | repo | binary
CFD_MIRROR="${CFA_GH_MIRROR:-}"            # 例: https://ghproxy.net/

TUNNEL_NAME="${CF_TUNNEL_ACCEL_TUNNEL_NAME:-home}"
TUNNEL_CONFIG="${CF_TUNNEL_ACCEL_CONFIG:-}"
SERVICE_NAME="${CF_TUNNEL_ACCEL_SERVICE:-}"
# 记录用户是否显式指定（显式指定时不自动探测覆盖）
EXPLICIT_TUNNEL_NAME=0
if [ -n "${CF_TUNNEL_ACCEL_TUNNEL_NAME:-}" ]; then EXPLICIT_TUNNEL_NAME=1; fi
EXPLICIT_CONFIG=0
if [ -n "${CF_TUNNEL_ACCEL_CONFIG:-}" ]; then EXPLICIT_CONFIG=1; fi
EXPLICIT_SERVICE=0
if [ -n "${CF_TUNNEL_ACCEL_SERVICE:-}" ]; then EXPLICIT_SERVICE=1; fi
AUX_ZONE="${CF_TUNNEL_ACCEL_AUX_ZONE:-v50kkp.eu.org}"
SERVICE_HOSTNAME="${CF_TUNNEL_ACCEL_FALLBACK_ORIGIN:-origin.${AUX_ZONE}}"
SERVICE_BACKEND="http://127.0.0.1:8080"

ASSUME_YES=0
INSTALL_DEPS=1
SETUP_SERVICE="auto"     # auto | yes | no
DRY_RUN=0
TOKEN_ARG=""

if [ -t 0 ]; then IS_TTY=1; else IS_TTY=0; fi

# ---------------- 输出 ----------------
if [ "$IS_TTY" = 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_CYN=$'\033[36m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_CYN=""; C_BLD=""; C_RST=""
fi
step() { printf '\n%s==> %s%s\n' "$C_BLD" "$*" "$C_RST"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  %sOK%s   %s\n' "$C_GRN" "$C_RST" "$*"; }
skip() { printf '  %s--%s   %s\n' "$C_CYN" "$C_RST" "$*"; }
warn() { printf '  %s警告%s %s\n' "$C_YEL" "$C_RST" "$*"; }
die()  { printf '%s错误%s: %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }
run()  {
  if [ "$DRY_RUN" = 1 ]; then printf '  %s[dry-run]%s %s\n' "$C_CYN" "$C_RST" "$*"; return 0; fi
  "$@"
}
usage() { sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'; }

# ---------------- 交互 ----------------
confirm() {  # confirm "问题" [y|n]
  local q="$1" def="${2:-y}"
  if [ "$ASSUME_YES" = 1 ] || [ "$IS_TTY" = 0 ]; then
    if [ "$def" = y ]; then return 0; else return 1; fi
  fi
  local tip="Y/n"
  if [ "$def" != y ]; then tip="y/N"; fi
  local ans=""
  read -r -p "$q [$tip]: " ans || true
  if [ -z "$ans" ]; then ans="$def"; fi
  case "$ans" in y|Y|yes|YES|是) return 0 ;; *) return 1 ;; esac
}
ask() {  # ask "问题" "默认值"
  local q="$1" def="${2:-}"
  if [ "$ASSUME_YES" = 1 ] || [ "$IS_TTY" = 0 ]; then printf '%s' "$def"; return 0; fi
  local ans=""
  read -r -p "$q [$def]: " ans || true
  printf '%s' "${ans:-$def}"
}

# ---------------- 参数解析 ----------------
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --token) shift; TOKEN_ARG="${1:-}" ;;
    --token=*) TOKEN_ARG="${1#*=}" ;;
    --no-deps) INSTALL_DEPS=0 ;;
    --service) SETUP_SERVICE=yes ;;
    --no-service) SETUP_SERVICE=no ;;
    --tunnel-name) shift; TUNNEL_NAME="${1:-home}"; EXPLICIT_TUNNEL_NAME=1 ;;
    --config) shift; TUNNEL_CONFIG="${1:-}"; EXPLICIT_CONFIG=1 ;;
    --hostname) shift; SERVICE_HOSTNAME="${1:-}" ;;
    --backend) shift; SERVICE_BACKEND="${1:-}" ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "未知参数: $1（--help 看用法）" ;;
    *)
      if [ -z "$TOKEN_ARG" ]; then TOKEN_ARG="$1"; else die "多余参数: $1"; fi
      ;;
  esac
  shift
done
# 本机已有安装时，默认值直接对齐现状（自己用，少敲字）
AUTODETECTED=""
if [ -z "$TUNNEL_CONFIG" ]; then
  if [ "$EXPLICIT_TUNNEL_NAME" = 1 ]; then
    TUNNEL_CONFIG="$CLOUDFLARED_DIR/config-${TUNNEL_NAME}.yml"
  elif [ -f "$CLOUDFLARED_DIR/config-${TUNNEL_NAME}.yml" ]; then
    TUNNEL_CONFIG="$CLOUDFLARED_DIR/config-${TUNNEL_NAME}.yml"
  else
    _newest="$(ls -1t "$CLOUDFLARED_DIR"/config*.yml 2>/dev/null | head -n1 || true)"
    if [ -n "$_newest" ]; then
      TUNNEL_CONFIG="$_newest"
      AUTODETECTED="$AUTODETECTED config=$_newest"
    else
      TUNNEL_CONFIG="$CLOUDFLARED_DIR/config-${TUNNEL_NAME}.yml"
    fi
  fi
fi
if [ "$EXPLICIT_TUNNEL_NAME" = 0 ] && [ -f "$TUNNEL_CONFIG" ]; then
  _base="$(basename "$TUNNEL_CONFIG")"; _base="${_base#config-}"; _base="${_base%.yml}"
  if [ -n "$_base" ] && [ "$_base" != "$TUNNEL_NAME" ]; then
    TUNNEL_NAME="$_base"
    AUTODETECTED="$AUTODETECTED tunnel=$_base"
  fi
fi
if [ -z "$SERVICE_NAME" ]; then
  if [ "$EXPLICIT_TUNNEL_NAME" = 1 ]; then
    SERVICE_NAME="cloudflared-${TUNNEL_NAME}.service"
  elif [ -f "/etc/systemd/system/cloudflared-${TUNNEL_NAME}.service" ]; then
    SERVICE_NAME="cloudflared-${TUNNEL_NAME}.service"
  else
    _unit="$(ls -1 /etc/systemd/system/cloudflared-*.service 2>/dev/null | head -n1 || true)"
    if [ -n "$_unit" ]; then
      SERVICE_NAME="$(basename "$_unit")"
      AUTODETECTED="$AUTODETECTED service=$SERVICE_NAME"
    else
      SERVICE_NAME="cloudflared-${TUNNEL_NAME}.service"
    fi
  fi
fi
if [ -n "${CFA_DRY_RUN:-}" ]; then DRY_RUN=1; fi

# ---------------- 系统探测 ----------------
detect_os() {
  OS_NAME="unknown"; PKG=""
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_NAME="${PRETTY_NAME:-${ID:-unknown}}"
  fi
  if   command -v apt-get >/dev/null 2>&1; then PKG=apt
  elif command -v dnf     >/dev/null 2>&1; then PKG=dnf
  elif command -v yum     >/dev/null 2>&1; then PKG=yum
  elif command -v apk     >/dev/null 2>&1; then PKG=apk
  elif command -v pacman  >/dev/null 2>&1; then PKG=pacman
  fi
}

pkg_install() {  # pkg_install <deb包> <rpm包> <apk包>
  local deb="$1" rpm="${2:-$1}" apk="${3:-$1}"
  case "$PKG" in
    apt)
      run apt-get update -qq || true
      run env DEBIAN_FRONTEND=noninteractive apt-get install -y "$deb"
      ;;
    dnf)    run dnf install -y "$rpm" ;;
    yum)    run yum install -y "$rpm" ;;
    apk)    run apk add --no-cache "$apk" ;;
    pacman) run pacman -Sy --noconfirm "$deb" ;;
    *)      warn "没识别到包管理器，请手动安装: $deb"; return 1 ;;
  esac
}

ensure_cmd() {  # ensure_cmd <命令> <apt包> [rpm包] [apk包]
  local cmd="$1" deb="$2" rpm="${3:-$2}" apk="${4:-$2}"
  if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd -> $(command -v "$cmd")"; return 0; fi
  if [ "$INSTALL_DEPS" != 1 ]; then warn "$cmd 缺失（--no-deps 已关闭自动安装）"; return 1; fi
  if ! confirm "  缺少 $cmd，自动安装？" y; then warn "$cmd 缺失，相关功能会不可用"; return 1; fi
  info "安装 $cmd ..."
  pkg_install "$deb" "$rpm" "$apk" || true
  if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd 安装完成"; return 0; fi
  warn "$cmd 仍不可用"
  return 1
}

download() {  # download <url> <目标文件>
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --connect-timeout 15 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$2" "$1"
  else
    return 1
  fi
}

systemd_usable() {
  command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}

# ---------------- cloudflared ----------------
cfd_arch() {
  case "$(uname -m)" in
    x86_64|amd64)      echo amd64 ;;
    aarch64|arm64)     echo arm64 ;;
    armv7l|armv6l|arm) echo arm ;;
    i386|i686|386)     echo 386 ;;
    *)                 echo "" ;;
  esac
}

cloudflared_present() {
  command -v cloudflared >/dev/null 2>&1 || [ -x "$CLOUDFLARED_BIN" ]
}

cloudflared_from_repo() {
  case "$PKG" in
    apt)
      local codename=""
      if [ -r /etc/os-release ]; then . /etc/os-release; codename="${VERSION_CODENAME:-}"; fi
      if [ -z "$codename" ] && command -v lsb_release >/dev/null 2>&1; then
        codename="$(lsb_release -cs 2>/dev/null || echo "")"
      fi
      if [ -z "$codename" ]; then warn "取不到发行版代号，跳过 apt 源方式"; return 1; fi
      if [ "$DRY_RUN" = 1 ]; then
        printf '  %s[dry-run]%s curl pkg.cloudflare.com/cloudflare-main.gpg -> /usr/share/keyrings/cloudflare-main.gpg\n' "$C_CYN" "$C_RST"
        printf '  %s[dry-run]%s 写 /etc/apt/sources.list.d/cloudflared.list（%s main）\n' "$C_CYN" "$C_RST" "$codename"
        printf '  %s[dry-run]%s apt-get install -y cloudflared\n' "$C_CYN" "$C_RST"
        return 0
      fi
      run install -d -m 0755 /usr/share/keyrings
      download https://pkg.cloudflare.com/cloudflare-main.gpg /tmp/cloudflare-main.gpg || return 1
      run install -m 0644 /tmp/cloudflare-main.gpg /usr/share/keyrings/cloudflare-main.gpg
      printf 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared %s main\n' "$codename" \
        > /etc/apt/sources.list.d/cloudflared.list
      run apt-get update -qq || true
      run env DEBIAN_FRONTEND=noninteractive apt-get install -y cloudflared
      ;;
    dnf|yum)
      if [ "$DRY_RUN" = 1 ]; then
        printf '  %s[dry-run]%s 写 /etc/yum.repos.d/cloudflared.repo 并 %s install -y cloudflared\n' "$C_CYN" "$C_RST" "$PKG"
        return 0
      fi
      download https://pkg.cloudflare.com/cloudflared.repo /etc/yum.repos.d/cloudflared.repo || return 1
      run "$PKG" install -y cloudflared
      ;;
    *) return 1 ;;
  esac
  cloudflared_present
}

cloudflared_from_binary() {
  local arch; arch="$(cfd_arch)"
  if [ -z "$arch" ]; then warn "不支持的架构 $(uname -m)，请手动安装 cloudflared"; return 1; fi
  local base="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"
  local urls=("$base")
  if [ -n "$CFD_MIRROR" ]; then urls=("${CFD_MIRROR%/}/$base" "$base"); fi
  if [ "$DRY_RUN" = 1 ]; then
    printf '  %s[dry-run]%s 下载 %s 并安装到 %s\n' "$C_CYN" "$C_RST" "$base" "$CLOUDFLARED_BIN"
    return 0
  fi
  local tmp; tmp="$(mktemp)"
  local url
  for url in "${urls[@]}"; do
    info "下载 $url"
    if download "$url" "$tmp"; then
      run install -m 0755 "$tmp" "$CLOUDFLARED_BIN"
      rm -f "$tmp"
      return 0
    fi
    warn "下载失败，换下一个源"
  done
  rm -f "$tmp"
  return 1
}

install_cloudflared() {
  if cloudflared_present; then
    ok "cloudflared -> $("$CLOUDFLARED_BIN" --version 2>/dev/null || command -v cloudflared)"
    return 0
  fi
  if [ "$INSTALL_DEPS" != 1 ]; then warn "cloudflared 缺失（--no-deps 已关闭自动安装）"; return 1; fi
  if ! confirm "  缺少 cloudflared，自动安装？" y; then warn "cloudflared 缺失，隧道功能不可用"; return 1; fi
  local order="binary repo"
  case "$CFD_METHOD" in
    repo)   order="repo binary" ;;
    binary) order="binary repo" ;;
    auto)
      case "$PKG" in
        apt|dnf|yum) order="repo binary" ;;
        *)           order="binary repo" ;;
      esac
      ;;
  esac
  local m
  for m in $order; do
    info "尝试 ${m} 方式安装 cloudflared ..."
    if "cloudflared_from_${m}"; then
      ok "cloudflared 安装完成: $("$CLOUDFLARED_BIN" --version 2>/dev/null || echo "$CLOUDFLARED_BIN")"
      return 0
    fi
  done
  warn "cloudflared 自动安装失败，手动下载: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
  return 1
}

# ---------------- systemd 隧道服务 ----------------
find_credentials() {
  local f
  for f in "$CLOUDFLARED_DIR"/*.json; do
    if [ ! -f "$f" ]; then continue; fi
    case "$(basename "$f")" in cert.json) continue ;; esac
    printf '%s' "$f"
    return 0
  done
  return 1
}

write_tunnel_config() {
  local cred="$1"
  local uuid; uuid="$(basename "$cred" .json)"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  %s[dry-run]%s 生成 %s (tunnel=%s, hostname=%s, service=%s)\n' \
      "$C_CYN" "$C_RST" "$TUNNEL_CONFIG" "$uuid" "$SERVICE_HOSTNAME" "$SERVICE_BACKEND"
    return 0
  fi
  mkdir -p "$(dirname "$TUNNEL_CONFIG")"
  cat > "$TUNNEL_CONFIG" <<EOF
tunnel: $uuid
credentials-file: $cred

protocol: http2

ingress:
  # install.sh 生成的最小配置：$SERVICE_HOSTNAME -> $SERVICE_BACKEND
  - hostname: $SERVICE_HOSTNAME
    service: $SERVICE_BACKEND

  - service: http_status:404
EOF
  ok "已生成隧道配置 $TUNNEL_CONFIG"
}

setup_service() {
  if ! systemd_usable; then
    warn "当前环境没有可用的 systemd，跳过服务创建（容器里属正常）"
    return 0
  fi
  if [ -f "$TUNNEL_CONFIG" ]; then
    ok "已有隧道配置 $TUNNEL_CONFIG"
  else
    local cred=""
    if cred="$(find_credentials)"; then
      ok "找到隧道凭据 $cred"
      write_tunnel_config "$cred"
    else
      warn "没找到隧道凭据（$CLOUDFLARED_DIR/*.json）"
      info "先在交互式终端执行下面两步，再重跑: ./install.sh --service"
      info "  cloudflared tunnel login"
      info "  cloudflared tunnel create $TUNNEL_NAME"
      return 0
    fi
  fi
  local unit="/etc/systemd/system/$SERVICE_NAME"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  %s[dry-run]%s 写 %s 并 systemctl enable --now %s\n' "$C_CYN" "$C_RST" "$unit" "$SERVICE_NAME"
    return 0
  fi
  if [ -f "$unit" ]; then
    cp -p "$unit" "$unit.bak.$(date +%Y%m%d-%H%M%S)"
    ok "已备份原单元 $unit"
  fi
  cat > "$unit" <<EOF
[Unit]
Description=cloudflared ${TUNNEL_NAME} tunnel (cf-tunnel-accel)
After=network-online.target
Wants=network-online.target

[Service]
TimeoutStartSec=15
Type=notify
ExecStart=${CLOUDFLARED_BIN} --no-autoupdate --config ${TUNNEL_CONFIG} tunnel run
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF
  run systemctl daemon-reload
  run systemctl enable --now "$SERVICE_NAME"
  if [ "$DRY_RUN" != 1 ]; then
    sleep 2
    if systemctl is-active --quiet "$SERVICE_NAME"; then
      ok "$SERVICE_NAME 已启动"
    else
      warn "$SERVICE_NAME 未处于 active，看日志: journalctl -u $SERVICE_NAME -n 50 --no-pager"
    fi
    if [ -x "$CLOUDFLARED_BIN" ]; then
      if "$CLOUDFLARED_BIN" --config "$TUNNEL_CONFIG" tunnel ingress validate >/dev/null 2>&1; then
        ok "ingress 校验通过"
      else
        warn "ingress 校验失败，检查 $TUNNEL_CONFIG"
      fi
    fi
  fi
}

# ---------------- 主流程 ----------------
if [ "$(id -u)" != 0 ]; then die "需要 root 权限（会写 $BIN_DIR / $CONFIG_DIR / systemd 单元）"; fi
if [ ! -f "$SELF_SCRIPT" ]; then die "同目录下找不到 cf-tunnel-accel，请在仓库目录里执行本脚本"; fi

detect_os
printf '%s\n' "${C_BLD}cf-tunnel-accel 安装程序${C_RST}"
info "系统: $OS_NAME  包管理器: ${PKG:-未识别}  架构: $(uname -m)  交互: $([ "$IS_TTY" = 1 ] && echo 是 || echo 否)"
info "隧道: $TUNNEL_NAME  配置: $TUNNEL_CONFIG  服务: $SERVICE_NAME"
if [ -n "$AUTODETECTED" ]; then info "按本机现状自动探测:$AUTODETECTED"; fi
if [ "$DRY_RUN" = 1 ]; then info "模式: dry-run（只打印动作）"; fi

step "1/6 安装脚本本体"
mkdir -p "$BIN_DIR" "$LINK_DIR" "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR" || true
if [ -f "$BIN_DIR/cf-tunnel-accel" ]; then
  backup="$BIN_DIR/cf-tunnel-accel.bak.$(date +%Y%m%d-%H%M%S)"
  run cp -p "$BIN_DIR/cf-tunnel-accel" "$backup"
  ok "旧版本已备份 $backup"
fi
run install -m 0755 "$SELF_SCRIPT" "$BIN_DIR/cf-tunnel-accel"
run ln -sfn "$BIN_DIR/cf-tunnel-accel" "$LINK_DIR/cf-tunnel-accel"
ok "$LINK_DIR/cf-tunnel-accel -> $BIN_DIR/cf-tunnel-accel"

step "2/6 检查依赖（缺什么装什么）"
ensure_cmd python3 python3 python3 python3 || die "python3 不可用，脚本无法运行"
ensure_cmd curl curl curl curl || true
ensure_cmd dig dnsutils bind-utils bind-tools || true
ensure_cmd systemctl systemd systemd systemd || true
install_cloudflared || true

step "3/6 配置 Cloudflare API Token"
if [ -n "$TOKEN_ARG" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    printf '  %s[dry-run]%s 写入 %s\n' "$C_CYN" "$C_RST" "$TOKEN_FILE"
  else
    (umask 077; printf '%s\n' "$TOKEN_ARG" > "$TOKEN_FILE")
    ok "Token 已写入 $TOKEN_FILE（权限 600）"
  fi
elif [ -s "$TOKEN_FILE" ]; then
  ok "已有 Token（$TOKEN_FILE）"
else
  warn "还没有 API Token（权限需要 Zone:Read / DNS:Edit / SSL and Certificates:Edit）"
  ans="$(ask "  现在粘贴 Token（留空跳过）" "")"
  if [ -n "$ans" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      printf '  %s[dry-run]%s 写入 %s\n' "$C_CYN" "$C_RST" "$TOKEN_FILE"
    else
      (umask 077; printf '%s\n' "$ans" > "$TOKEN_FILE")
      ok "Token 已写入 $TOKEN_FILE（权限 600）"
    fi
  else
    skip "先跳过；稍后可重跑 install.sh --token <TOKEN>"
  fi
fi

step "4/6 配置覆盖文件（可选，多机部署用）"
if [ -f "$ENV_FILE" ]; then
  ok "已有 $ENV_FILE"
elif confirm "  生成 $ENV_FILE 模板？" n; then
  if [ "$DRY_RUN" = 1 ]; then
    printf '  %s[dry-run]%s 写 %s\n' "$C_CYN" "$C_RST" "$ENV_FILE"
  else
    cat > "$ENV_FILE" <<EOF
# cf-tunnel-accel 服务器级配置（KEY=VALUE，环境变量优先级更高）
# 改完用 cf-tunnel-accel env 核对生效值。
CF_TUNNEL_ACCEL_TUNNEL_NAME=$TUNNEL_NAME
CF_TUNNEL_ACCEL_CONFIG=$TUNNEL_CONFIG
CF_TUNNEL_ACCEL_SERVICE=$SERVICE_NAME
# CF_TUNNEL_ACCEL_TUNNEL_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
# CF_TUNNEL_ACCEL_AUX_ZONE=$AUX_ZONE
EOF
    chmod 600 "$ENV_FILE"
    ok "已生成 $ENV_FILE"
  fi
else
  skip "跳过"
fi

step "5/6 systemd 隧道服务"
case "$SETUP_SERVICE" in
  no) skip "按 --no-service 跳过" ;;
  yes) setup_service ;;
  auto)
    if systemd_usable && [ -f "$TUNNEL_CONFIG" ]; then
      setup_service
    elif systemd_usable && confirm "  没有隧道配置，现在创建/修复 systemd 服务？（需要先 tunnel login/create）" n; then
      SERVICE_HOSTNAME="$(ask "  写进配置的 hostname" "$SERVICE_HOSTNAME")"
      SERVICE_BACKEND="$(ask "  本地服务地址" "$SERVICE_BACKEND")"
      setup_service
    else
      skip "跳过；之后可执行 install.sh --service"
    fi
    ;;
esac

step "6/6 自检"
if [ "$DRY_RUN" = 1 ]; then
  skip "dry-run 结束，未做任何修改"
  exit 0
fi
if "$LINK_DIR/cf-tunnel-accel" --version >/dev/null 2>&1; then
  ok "脚本可用: $("$LINK_DIR/cf-tunnel-accel" --version)"
else
  warn "脚本执行失败，请检查 python3"
fi
"$LINK_DIR/cf-tunnel-accel" env 2>/dev/null | sed -n '/生效配置/,$p' | head -n 12 || true
if [ -s "$TOKEN_FILE" ] || [ -n "${CF_API_TOKEN:-}" ] || [ -n "${CF_TUNNEL_ACCEL_API_TOKEN:-}" ]; then
  info "验证 Token 并读取域名清单 ..."
  "$LINK_DIR/cf-tunnel-accel" domains 2>&1 | sed -n '1,8p' || warn "域名清单读取失败，稍后用 cf-tunnel-accel env 排查"
fi

printf '\n%s安装完成。%s\n' "$C_BLD" "$C_RST"
info "  cf-tunnel-accel env        # 核对该服务器生效的配置"
info "  cf-tunnel-accel domains    # 看账号里有哪些域名可用"
info "  cf-tunnel-accel            # 交互菜单（新增域名 / 测速 / 健康检查）"
