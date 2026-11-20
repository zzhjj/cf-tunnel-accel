#!/usr/bin/env bash
# Install the complete, standard-library CLI + UI package. No Cloudflare API calls.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: install.sh [--user] [--source-dir DIR] [--no-deps] [--dry-run]
  --user                 Install under XDG_DATA_HOME and HOME/.local/bin
  --source-dir DIR       Package containing cf-tunnel-accel and ui/ (default: script dir)
  --bin-dir DIR          Package directory (legacy CFA_BIN_DIR)
  --link-dir DIR         Launcher symlink directory (legacy CFA_LINK_DIR)
  --config-dir DIR       Application config directory (legacy CFA_CONFIG_DIR)
  --config FILE          Explicit existing Tunnel config; never auto-discovered
  --tunnel-id UUID       Explicit Tunnel ID, required when creating a config
  --credentials-file FILE  Explicit JSON credentials, required for a new config
  --tunnel-name NAME     Optional display name (no personal default)
  --hostname NAME --backend URL  Required when creating a Tunnel config
  --service              Install the explicitly selected Tunnel systemd unit
  --service-name NAME    Explicit unit basename, also CF_TUNNEL_ACCEL_SERVICE
  --ui-service           Install cf-tunnel-ui.service, bound to 127.0.0.1:8642
  --ui-service-name NAME Override the UI unit basename
  --no-service           Do not install a Tunnel unit (the default)
  --token TOKEN          Write a private token file; legacy positional token accepted
  --no-deps              Skip optional dependencies; Python 3.9+ is still required
  --dry-run              Validate and print only: no files, downloads, packages or units
  -y, --yes              Accepted for compatibility; installation is non-interactive
  -h, --help             Show this help

System install requires root; defaults: /usr/local/lib/cf-tunnel-accel,
/usr/local/bin. Config defaults to ${XDG_CONFIG_HOME:-$HOME/.config}/cf-tunnel-accel.
User package: ${XDG_DATA_HOME:-$HOME/.local/share}/cf-tunnel-accel.
Legacy CFA_DRY_RUN, CFA_CFD_DIR, CFA_CFD_METHOD, CF_TUNNEL_ACCEL_* are supported.
CFA_GH_MIRROR is not trusted: downloads use official HTTPS release assets only.
System units require root + running systemd. Units are NEVER started or enabled.
Missing systemd degrades to foreground CLI/UI. --no-deps needs no cloudflared.
EOF
}
info() { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
need_value() { [ "$#" -ge 2 ] && [ -n "$2" ] && [[ "$2" != --* ]] || die "$1 requires a value"; }

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
USER_MODE=0; DRY_RUN="${CFA_DRY_RUN:-0}"; INSTALL_DEPS=1
TUNNEL_SERVICE=0; UI_SERVICE=0; TOKEN_ARG=""; TOKEN_SET=0
BIN_DIR="${CFA_BIN_DIR:-}"; LINK_DIR="${CFA_LINK_DIR:-}"; CONFIG_DIR="${CFA_CONFIG_DIR:-}"
TUNNEL_CONFIG="${CF_TUNNEL_ACCEL_CONFIG:-}"
TUNNEL_ID="${CF_TUNNEL_ACCEL_TUNNEL_ID:-}"
TUNNEL_NAME="${CF_TUNNEL_ACCEL_TUNNEL_NAME:-}"
CREDENTIALS="${CF_TUNNEL_ACCEL_CREDENTIALS_FILE:-}"
SERVICE_NAME="${CF_TUNNEL_ACCEL_SERVICE:-}"
HOSTNAME="${CF_TUNNEL_ACCEL_FALLBACK_ORIGIN:-}"
BACKEND="${CF_TUNNEL_ACCEL_BACKEND:-}"
UI_SERVICE_NAME="${CFA_UI_SERVICE_NAME:-cf-tunnel-ui.service}"
SYSTEMD_DIR="${CFA_SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMD_RUNTIME_DIR="${CFA_SYSTEMD_RUNTIME_DIR:-/run/systemd/system}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --user) USER_MODE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --no-deps) INSTALL_DEPS=0 ;;
    -y|--yes) : ;;
    --service) TUNNEL_SERVICE=1 ;;
    --service=*) TUNNEL_SERVICE=1; SERVICE_NAME="${1#*=}"; [ -n "$SERVICE_NAME" ] || die '--service requires a nonempty name' ;;
    --no-service) TUNNEL_SERVICE=0 ;;
    --ui-service) UI_SERVICE=1 ;;
    --source-dir|--bin-dir|--link-dir|--config-dir|--config|--tunnel-id|--tunnel-name|--credentials-file|--credentials|--service-name|--ui-service-name|--hostname|--backend|--token)
      option="$1"; need_value "$@"; value="$2"; shift
      case "$option" in
        --source-dir) SOURCE_DIR="$value" ;; --bin-dir) BIN_DIR="$value" ;;
        --link-dir) LINK_DIR="$value" ;; --config-dir) CONFIG_DIR="$value" ;;
        --config) TUNNEL_CONFIG="$value" ;; --tunnel-id) TUNNEL_ID="$value" ;;
        --tunnel-name) TUNNEL_NAME="$value" ;; --credentials-file|--credentials) CREDENTIALS="$value" ;;
        --service-name) SERVICE_NAME="$value" ;; --ui-service-name) UI_SERVICE_NAME="$value" ;;
        --hostname) HOSTNAME="$value" ;; --backend) BACKEND="$value" ;;
        --token) TOKEN_ARG="$value"; TOKEN_SET=1 ;;
      esac ;;
    --token=*) TOKEN_ARG="${1#*=}"; [ -n "$TOKEN_ARG" ] || die '--token requires a value'; TOKEN_SET=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "Unknown option: $1" ;;
    *) [ "$TOKEN_SET" = 0 ] || die 'Unexpected positional argument'; TOKEN_ARG="$1"; TOKEN_SET=1 ;;
  esac
  shift
done
case "$DRY_RUN" in 0|1) ;; *) die 'CFA_DRY_RUN must be 0 or 1' ;; esac
: "${HOME:?HOME must be set}"
if [ "$USER_MODE" = 1 ]; then
  BIN_DIR="${BIN_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/cf-tunnel-accel}"
  LINK_DIR="${LINK_DIR:-$HOME/.local/bin}"
else
  [ "$(id -u)" = 0 ] || die 'System installation requires root; use --user'
  BIN_DIR="${BIN_DIR:-/usr/local/lib/cf-tunnel-accel}"
  LINK_DIR="${LINK_DIR:-/usr/local/bin}"
fi
CONFIG_DIR="${CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/cf-tunnel-accel}"
ENV_FILE="${CF_TUNNEL_ACCEL_ENV_FILE:-$CONFIG_DIR/env}"
TOKEN_FILE="${CF_TUNNEL_ACCEL_TOKEN_FILE:-$CONFIG_DIR/token}"
CLOUDFLARED_BIN="${CF_TUNNEL_ACCEL_CLOUDFLARED:-}"
if [ -z "$CLOUDFLARED_BIN" ]; then
  CLOUDFLARED_BIN="$(command -v cloudflared || true)"
  CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-$LINK_DIR/cloudflared}"
fi
CFD_METHOD="${CFA_CFD_METHOD:-auto}"
case "$CFD_METHOD" in auto|binary|repo) ;; *) die 'CFA_CFD_METHOD must be auto, binary or repo' ;; esac
if [ -n "${CFA_GH_MIRROR:-}" ]; then
  [ "$INSTALL_DEPS" = 0 ] || die 'CFA_GH_MIRROR is unsupported; use official HTTPS releases or --no-deps'
  warn 'CFA_GH_MIRROR ignored (--no-deps)'
fi
[ "$CFD_METHOD" != repo ] || warn 'Legacy repo method uses verified official binaries; no repository configuration is modified'
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die 'Python 3.9+ is required. Install python3 first, then rerun the installer.'
"$PYTHON" -B -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' || die 'Python 3.9+ is required'

# Normalize relative overrides against the invocation cwd, not the package directory.
absolute_path() { "$PYTHON" -B -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1"; }
for var in SOURCE_DIR BIN_DIR LINK_DIR CONFIG_DIR ENV_FILE TOKEN_FILE CLOUDFLARED_BIN SYSTEMD_DIR SYSTEMD_RUNTIME_DIR; do
  printf -v "$var" '%s' "$(absolute_path "${!var}")"
done
[ -z "$TUNNEL_CONFIG" ] || TUNNEL_CONFIG="$(absolute_path "$TUNNEL_CONFIG")"
[ -z "$CREDENTIALS" ] || CREDENTIALS="$(absolute_path "$CREDENTIALS")"
export CFA_INSTALL_SOURCE="$SOURCE_DIR" CFA_INSTALL_BIN="$BIN_DIR" CFA_INSTALL_LINK="$LINK_DIR"
export CFA_INSTALL_CONFIG_DIR="$CONFIG_DIR" CFA_INSTALL_ENV="$ENV_FILE" CFA_INSTALL_TOKEN_FILE="$TOKEN_FILE"
export CFA_INSTALL_CFD="$CLOUDFLARED_BIN" CFA_INSTALL_SYSTEMD="$SYSTEMD_DIR"
export CFA_INSTALL_TUNNEL_CONFIG="$TUNNEL_CONFIG" CFA_INSTALL_TUNNEL_ID="$TUNNEL_ID"
export CFA_INSTALL_CREDENTIALS="$CREDENTIALS" CFA_INSTALL_HOST="$HOSTNAME" CFA_INSTALL_BACKEND="$BACKEND"
export CFA_INSTALL_NAME="$TUNNEL_NAME" CFA_INSTALL_SERVICE="$SERVICE_NAME" CFA_INSTALL_UI_SERVICE="$UI_SERVICE_NAME"
export CFA_INSTALL_MANAGE_TUNNEL="$TUNNEL_SERVICE" CFA_INSTALL_TOKEN="$TOKEN_ARG"
SYSTEMD_OK=0
if [ "$TUNNEL_SERVICE" = 1 ] || [ "$UI_SERVICE" = 1 ]; then
  [ "$USER_MODE" = 0 ] && [ "$(id -u)" = 0 ] || die 'System service options require a root system installation (not --user)'
  if command -v systemctl >/dev/null 2>&1 && [ -d "$SYSTEMD_RUNTIME_DIR" ]; then
    SYSTEMD_OK=1
  else
    warn 'systemd is not running; skipping units. Use the installed foreground CLI/UI.'
  fi
fi
export CFA_INSTALL_SYSTEMD_OK="$SYSTEMD_OK"

# Entire read-only preflight runs before mkdir, package managers, downloads or staging.
"$PYTHON" -B - <<'PY'
import ast, json, os, re, sys, uuid
from pathlib import Path
from urllib.parse import urlsplit

def fail(message):
    sys.exit('Error: ' + message)
def get(name):
    return os.environ['CFA_INSTALL_' + name]
def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents
source = Path(get('SOURCE'))
required = ['cf-tunnel-accel', 'ui/server.py', 'ui/profiles.py',
            'ui/static/index.html', 'ui/static/app.js', 'ui/static/style.css']
for name in required:
    path = source / name
    if not path.is_file() or not os.access(path, os.R_OK) or not path.stat().st_size:
        fail('Incomplete source package: missing/unreadable ' + name)
if (source / 'cf-tunnel-accel').is_symlink() or (source / 'ui').is_symlink():
    fail('CLI and UI sources must be regular paths inside the package')
helpers = sorted(source.glob('cfa_*.py'))
for path in helpers:
    if path.is_symlink() or not path.is_file() or not os.access(path, os.R_OK) or not path.stat().st_size:
        fail('Incomplete source package: invalid helper ' + str(path))
for path in [source / 'cf-tunnel-accel', *helpers, *sorted((source / 'ui').rglob('*.py'))]:
    try:
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except (SyntaxError, OSError, UnicodeError) as exc:
        fail('Invalid Python source: ' + str(exc))
for path in (source / 'ui').rglob('*'):
    if path.is_symlink():
        fail('Source UI must not contain symlinks: ' + str(path))
# Every imported cfa_* helper must be shipped beside the extensionless CLI.
import ast
cli_tree = ast.parse((source / 'cf-tunnel-accel').read_text(encoding='utf-8'))
imports = set()
for node in ast.walk(cli_tree):
    if isinstance(node, ast.Import):
        imports.update(alias.name.split('.')[0] for alias in node.names if alias.name.startswith('cfa_'))
    elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('cfa_'):
        imports.add(node.module.split('.')[0])
for name in imports:
    if not (source / (name + '.py')).is_file():
        fail('Incomplete source package: missing CLI helper ' + name + '.py')
paths = [Path(get(k)) for k in ('BIN', 'LINK', 'CONFIG_DIR')]
for path in paths:
    if path == Path('/') or path == Path.home() or (path.exists() and not path.is_dir()):
        fail('Invalid installation directory: ' + str(path))
if paths[0].is_symlink():
    fail('Package destination must not be a symlink')
if overlaps(paths[0].resolve(), source.resolve()):
    fail('Package destination and source directory must not overlap')
for other in paths[1:]:
    if overlaps(paths[0].resolve(), other.resolve()):
        fail('Package, links and configuration directories must be separate')
files = [Path(get(k)) for k in ('ENV', 'TOKEN_FILE', 'CFD')]
if len(set(files)) != len(files):
    fail('env, token and cloudflared paths must be distinct')
for label, path in zip(('ENV', 'TOKEN_FILE', 'CFD'), files):
    if path.is_symlink() and (label != 'CFD' or not path.is_file() or not os.access(path, os.X_OK)):
        fail('Invalid file destination: ' + str(path))
    if path.is_dir() or overlaps(path.resolve(), paths[0].resolve()):
        fail('Invalid file destination: ' + str(path))
    if path in paths or path == source / 'cf-tunnel-accel':
        fail('File destination collides with package/directory')
for name in ('cf-tunnel-accel', 'cf-tunnel-ui'):
    path = paths[1] / name
    if path.is_dir() and not path.is_symlink():
        fail('Launcher destination is a directory: ' + str(path))
    if path in files:
        fail('Launcher destination collides with a configuration/dependency file')
for key, value in os.environ.items():
    if (key.startswith(('CFA_INSTALL_', 'CF_TUNNEL_ACCEL_')) and
            any(ord(c) < 32 or ord(c) == 127 for c in value)):
        fail('Control characters in ' + key)
for name in (get('SERVICE'), get('UI_SERVICE')):
    if name and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service', name):
        fail('Service name must be an explicit .service basename')
if get('SERVICE') == get('UI_SERVICE'):
    fail('Tunnel and UI service names must differ')
config, tid, cred = get('TUNNEL_CONFIG'), get('TUNNEL_ID'), get('CREDENTIALS')
if tid:
    try:
        if str(uuid.UUID(tid)) != tid.lower():
            fail('Tunnel ID must be a canonical UUID')
    except ValueError:
        fail('Tunnel ID must be a UUID')
if config:
    path = Path(config)
    if path.is_dir() or path.is_symlink():
        fail('Tunnel config must be a regular file path')
    if overlaps(path.resolve(), paths[0].resolve()) or path in files or path in paths:
        fail('Tunnel config collides with the installation')
    if not path.is_file() and get('MANAGE_TUNNEL') != '1':
        fail('Selected config does not exist; use --service with explicit creation inputs')
if cred:
    try:
        credentials = json.loads(Path(cred).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        fail('Credentials must be an explicit readable JSON file')
    if not isinstance(credentials, dict) or not credentials.get('TunnelID'):
        fail('Credentials are missing TunnelID')
    if tid and str(credentials['TunnelID']).lower() != tid.lower():
        fail('Credentials TunnelID does not match the selected Tunnel ID')
if get('MANAGE_TUNNEL') == '1':
    if not config or not get('SERVICE'):
        fail('--service requires explicit --config and --service-name (or environment equivalents)')
    if not Path(config).is_file():
        if not all((tid, cred, get('HOST'), get('BACKEND'))):
            fail('New config requires --tunnel-id, --credentials-file, --hostname and --backend; no credentials are auto-selected')
        if not all(credentials.get(k) for k in ('AccountTag', 'TunnelSecret')):
            fail('Credentials are missing required Tunnel fields')
        if not re.fullmatch(r'(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9-]{2,63}', get('HOST')):
            fail('Invalid hostname')
        try:
            url = urlsplit(get('BACKEND'))
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
                fail('Backend must be an http(s) URL without credentials')
            if url.port is not None and not 1 <= url.port <= 65535:
                fail('Invalid backend port')
        except ValueError:
            fail('Invalid backend URL')
PY

CFD_PRESENT=0
if [ -x "$CLOUDFLARED_BIN" ]; then
  cfd_version="$("$CLOUDFLARED_BIN" --version 2>/dev/null)" || die 'Selected cloudflared failed --version'
  [[ "$cfd_version" =~ ^cloudflared\ version\ [0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2} ]] || die 'Selected cloudflared returned an invalid version'
  CFD_PRESENT=1
elif [ -e "$CLOUDFLARED_BIN" ]; then
  die 'Selected cloudflared is not executable'
fi
if [ "$TUNNEL_SERVICE" = 1 ] && [ "$SYSTEMD_OK" = 1 ] && [ "$CFD_PRESENT" = 0 ] && [ "$INSTALL_DEPS" = 0 ]; then
  die '--service needs cloudflared for offline ingress validation; install it first or omit --service'
fi
if [ "$DRY_RUN" = 0 ] && [ "$TUNNEL_SERVICE" = 1 ] && [ "$SYSTEMD_OK" = 1 ] && [ "$CFD_PRESENT" = 1 ] && [ -f "$TUNNEL_CONFIG" ]; then
  "$CLOUDFLARED_BIN" --config "$TUNNEL_CONFIG" tunnel ingress validate >/dev/null || die 'Selected Tunnel config failed ingress validation'
fi
info "Package: $SOURCE_DIR -> $BIN_DIR"
info "Launchers: $LINK_DIR; configuration: $CONFIG_DIR; env: $ENV_FILE"
if [ "$DRY_RUN" = 1 ]; then
  info '[dry-run] Stage CLI + full UI, install launchers and persist explicitly selected non-secret configuration'
  [ "$TOKEN_SET" = 0 ] || info '[dry-run] Write private token file (value omitted)'
  if [ "$INSTALL_DEPS" = 1 ]; then
    info '[dry-run] Check optional curl/dig; missing cloudflared uses official HTTPS release with version/executable/checksum validation'
  fi
  [ "$TUNNEL_SERVICE" = 0 ] || info "[dry-run] Explicit Tunnel unit: $SERVICE_NAME (no startup)"
  [ "$UI_SERVICE" = 0 ] || info "[dry-run] UI unit: $UI_SERVICE_NAME, loopback only (no startup)"
  info '[dry-run] No files, downloads, packages or service mutations performed'
  exit 0
fi

# Transaction journal: sibling staging + rename; rollback includes symlinks/config/units.
TX_ID="$(date -u +%Y%m%dT%H%M%S).$$"
STAGES=(); CREATED_DIRS=(); TARGETS=(); BACKUPS=(); EXISTED=(); COMMITTED=0; RELOADED=0
cleanup() {
  local status=$? i
  trap - EXIT INT TERM
  if [ "$COMMITTED" = 0 ]; then
    for ((i=${#TARGETS[@]}-1; i>=0; i--)); do
      if [ "${EXISTED[i]}" = 1 ] && { [ -e "${BACKUPS[i]}" ] || [ -L "${BACKUPS[i]}" ]; }; then
        rm -rf -- "${TARGETS[i]}"
        mv -- "${BACKUPS[i]}" "${TARGETS[i]}" || warn "Rollback restore failed: ${TARGETS[i]}"
      elif [ "${EXISTED[i]}" = 0 ]; then
        rm -rf -- "${TARGETS[i]}"
      fi
    done
    if [ "$RELOADED" = 1 ]; then systemctl daemon-reload >/dev/null 2>&1 || true; fi
    [ "$status" = 0 ] || warn 'Installation failed; previous package/configuration/units restored'
  fi
  for i in "${STAGES[@]}"; do rm -rf -- "$i"; done
  for ((i=${#CREATED_DIRS[@]}-1; i>=0; i--)); do rmdir -- "${CREATED_DIRS[i]}" 2>/dev/null || true; done
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
ensure_dir() {
  [ -d "$1" ] && return 0
  [ ! -e "$1" ] && [ ! -L "$1" ] || die "Not a directory: $1"
  ensure_dir "$(dirname -- "$1")"
  mkdir -- "$1"
  CREATED_DIRS+=("$1")
}
stage_file() {
  ensure_dir "$(dirname -- "$1")"
  STAGED_FILE="$(mktemp "${1}.stage.XXXXXX")"
  STAGES+=("$STAGED_FILE")
}
replace() {
  local staged="$1" target="$2" backup="${2}.bak.${TX_ID}" existed=0
  [ ! -e "$backup" ] && [ ! -L "$backup" ] || die "Backup already exists: $backup"
  [ ! -e "$target" ] && [ ! -L "$target" ] || existed=1
  TARGETS+=("$target"); BACKUPS+=("$backup"); EXISTED+=("$existed")
  if [ "$existed" = 1 ]; then mv -- "$target" "$backup"; fi
  mv -- "$staged" "$target"
}

ensure_dir "$(dirname -- "$BIN_DIR")"
PACKAGE_STAGE="$(mktemp -d "${BIN_DIR}.stage.XXXXXX")"; STAGES+=("$PACKAGE_STAGE")
# Keep unrelated local files in legacy package directories; replace shipped components.
if [ -d "$BIN_DIR" ]; then cp -a -- "$BIN_DIR/." "$PACKAGE_STAGE/"; fi
rm -rf -- "$PACKAGE_STAGE/ui" "$PACKAGE_STAGE/launchers" "$PACKAGE_STAGE/cf-tunnel-ui" "$PACKAGE_STAGE/cf-tunnel-accel"
find "$PACKAGE_STAGE" -maxdepth 1 -type f -name 'cfa_*.py' -delete
install -m 0755 -- "$SOURCE_DIR/cf-tunnel-accel" "$PACKAGE_STAGE/cf-tunnel-accel"
find "$SOURCE_DIR" -maxdepth 1 -type f -name 'cfa_*.py' -exec cp -a -- {} "$PACKAGE_STAGE/" \;
cp -a -- "$SOURCE_DIR/ui" "$PACKAGE_STAGE/ui"
if [ -f "$SOURCE_DIR/LICENSE" ]; then
  install -m 0644 -- "$SOURCE_DIR/LICENSE" "$PACKAGE_STAGE/LICENSE"
else
  rm -f -- "$PACKAGE_STAGE/LICENSE"
fi
# Runtime state, tests and bytecode are not part of the installed UI payload.
rm -rf -- "$PACKAGE_STAGE/ui/tests"
find "$PACKAGE_STAGE/ui" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$PACKAGE_STAGE/ui" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
mkdir -- "$PACKAGE_STAGE/launchers"
make_launcher() {
  local destination="$1" kind="$2"
  {
    printf '#!/usr/bin/env bash\nset -euo pipefail\n'
    printf ': "${CF_TUNNEL_ACCEL_ENV_FILE:=%s}"\n' "$(printf '%s' "$ENV_FILE" | sed 's/[\\"`$]/\\&/g')"
    printf ': "${CF_TUNNEL_ACCEL_CONFIG_DIR:=%s}"\n' "$(printf '%s' "$CONFIG_DIR" | sed 's/[\\"`$]/\\&/g')"
    printf ': "${CF_TUNNEL_ACCEL_SCRIPT:=%s}"\n' "$(printf '%s' "$BIN_DIR/cf-tunnel-accel" | sed 's/[\\"`$]/\\&/g')"
    printf 'export CF_TUNNEL_ACCEL_CONFIG_DIR CF_TUNNEL_ACCEL_ENV_FILE CF_TUNNEL_ACCEL_SCRIPT\n'
    if [ "$kind" = cli ]; then
      printf 'exec %q -B -u %q "$@"\n' "$PYTHON" "$BIN_DIR/cf-tunnel-accel"
    else
      printf 'exec %q -B -u %q --module "$CF_TUNNEL_ACCEL_SCRIPT" "$@"\n' "$PYTHON" "$BIN_DIR/ui/server.py"
    fi
  } > "$destination"
  chmod 0755 -- "$destination"
}
make_launcher "$PACKAGE_STAGE/launchers/cf-tunnel-accel" cli
make_launcher "$PACKAGE_STAGE/cf-tunnel-ui" ui
chmod 0755 -- "$PACKAGE_STAGE"
ensure_dir "$CONFIG_DIR"
if [[ " ${CREATED_DIRS[*]} " == *" $CONFIG_DIR "* ]]; then chmod 0700 -- "$CONFIG_DIR"; fi
stage_file "$ENV_FILE"; ENV_STAGE="$STAGED_FILE"
"$PYTHON" -B - "$ENV_STAGE" <<'PY'
import os, re, sys
from pathlib import Path
path = Path(os.environ['CFA_INSTALL_ENV'])
text = path.read_text(encoding='utf-8') if path.exists() else '# cf-tunnel-accel: KEY=VALUE, inherited environment takes precedence\n'
# Keep existing entries/comments; replace only explicitly selected public values.
selected = {}
for key, value in os.environ.items():
    if (key.startswith('CF_TUNNEL_ACCEL_') and value and
            not re.search(r'TOKEN|SECRET|PASSWORD|SALT|KEY', key) and key not in ('CF_TUNNEL_ACCEL_ENV_FILE',)):
        selected[key] = value
for key, name in [('CONFIG', 'TUNNEL_CONFIG'), ('TUNNEL_ID', 'TUNNEL_ID'),
                  ('TUNNEL_NAME', 'NAME'), ('SERVICE', 'SERVICE'),
                  ('CREDENTIALS_FILE', 'CREDENTIALS'), ('FALLBACK_ORIGIN', 'HOST')]:
    value = os.environ['CFA_INSTALL_' + name]
    if value:
        selected['CF_TUNNEL_ACCEL_' + key] = value
config_dir = os.environ['CFA_INSTALL_CONFIG_DIR']
selected.update(CF_TUNNEL_ACCEL_CLOUDFLARED=os.environ['CFA_INSTALL_CFD'],
                CF_TUNNEL_ACCEL_TOKEN_FILE=os.environ['CFA_INSTALL_TOKEN_FILE'],
                CF_TUNNEL_ACCEL_CONFIG_DIR=config_dir,
                CF_TUNNEL_ACCEL_ZONE_CACHE=os.path.join(config_dir, 'zones.json'),
                CF_TUNNEL_ACCEL_SCRIPT=os.environ['CFA_INSTALL_BIN'] + '/cf-tunnel-accel',
                CF_TUNNEL_ACCEL_UI_DIR=config_dir)
lines = [line for line in text.splitlines() if line.partition('=')[0].strip() not in selected]
lines.extend(key + '=' + value for key, value in sorted(selected.items()))
Path(sys.argv[1]).write_text('\n'.join(lines) + '\n', encoding='utf-8')
PY
chmod 0600 -- "$ENV_STAGE"
TOKEN_STAGE=""
if [ "$TOKEN_SET" = 1 ]; then
  stage_file "$TOKEN_FILE"; TOKEN_STAGE="$STAGED_FILE"
  (umask 077; printf '%s\n' "$TOKEN_ARG" > "$TOKEN_STAGE")
  chmod 0600 -- "$TOKEN_STAGE"
fi

# Optional dependencies are never a prerequisite for offline CLI/UI installation.
install_optional_tools() {
  local cmd package manager=""
  [ "$INSTALL_DEPS" = 1 ] || return 0
  for cmd in curl dig; do
    command -v "$cmd" >/dev/null 2>&1 && continue
    if [ "$(id -u)" != 0 ] || [ "$USER_MODE" = 1 ]; then
      warn "Optional $cmd is missing; install it for network diagnostics/downloads"
      continue
    fi
    for manager in apt-get dnf yum apk; do command -v "$manager" >/dev/null 2>&1 && break; manager=""; done
    [ -n "$manager" ] || { warn "Optional $cmd missing; no supported package manager"; continue; }
    package="$cmd"
    if [ "$cmd" = dig ]; then
      case "$manager" in apt-get) package=dnsutils ;; apk) package=bind-tools ;; *) package=bind-utils ;; esac
    fi
    case "$manager" in
      apt-get) env DEBIAN_FRONTEND=noninteractive apt-get install -y "$package" || warn "Optional $cmd install failed" ;;
      dnf|yum) "$manager" install -y "$package" || warn "Optional $cmd install failed" ;;
      apk) apk add --no-cache "$package" || warn "Optional $cmd install failed" ;;
    esac
  done
}
https_download() {
  [[ "$1" == https://* ]] || die 'Download URL must use HTTPS'
  command -v curl >/dev/null 2>&1 || die 'curl is required for official HTTPS downloads; install curl or use --no-deps'
  curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 2 --connect-timeout 15 --max-time 180 --output "$2" "$1"
}
CFD_STAGE=""
install_optional_tools
if [ "$CFD_PRESENT" = 0 ] && [ "$INSTALL_DEPS" = 1 ]; then
  [ "$(uname -s)" = Linux ] || die 'Automatic cloudflared downloads support Linux only; use --no-deps'
  case "$(uname -m)" in
    x86_64|amd64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;;
    armv7l|armv6l|arm) ARCH=arm ;; i386|i686|386) ARCH=386 ;;
    *) die 'Unsupported cloudflared architecture; install it manually or use --no-deps' ;;
  esac
  stage_file "$CLOUDFLARED_BIN"; CFD_STAGE="$STAGED_FILE"
  RELEASE_META="$PACKAGE_STAGE/release.json"
  https_download https://api.github.com/repos/cloudflare/cloudflared/releases/latest "$RELEASE_META"
  RELEASE_INFO="$PACKAGE_STAGE/release-info"
  "$PYTHON" -B - "$RELEASE_META" "$ARCH" > "$RELEASE_INFO" <<'PY'
import json, re, sys
release = json.load(open(sys.argv[1], encoding='utf-8'))
tag = release.get('tag_name', '')
if not re.fullmatch(r'v?\d{4}\.\d{1,2}\.\d{1,2}', tag):
    sys.exit('Error: Invalid official cloudflared release tag')
name = 'cloudflared-linux-' + sys.argv[2]
assets = release.get('assets', [])
found = [a for a in assets if a.get('name') == name]
if len(found) != 1:
    sys.exit('Error: Official release has no unique matching binary')
prefix = 'https://github.com/cloudflare/cloudflared/releases/download/' + tag + '/'
def url(asset):
    target = asset.get('browser_download_url', '')
    if target != prefix + asset['name']:
        sys.exit('Error: Non-official release asset URL')
    return target
asset = found[0]
digest = asset.get('digest') or ''
if digest and not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
    sys.exit('Error: Invalid published SHA256 digest')
checks = [a for a in assets if a.get('name') in
          (name + '.sha256', name + '.sha256sum', 'checksums.txt', 'SHA256SUMS', 'sha256sum.txt')]
print(url(asset)); print(digest.removeprefix('sha256:')); print(tag.lstrip('v'))
print(url(checks[0]) if checks else '')
PY
  mapfile -t release < "$RELEASE_INFO"
  https_download "${release[0]}" "$CFD_STAGE"
  CHECKSUM_FILE=""
  if [ -z "${release[1]}" ] && [ -n "${release[3]:-}" ]; then
    CHECKSUM_FILE="$PACKAGE_STAGE/checksums"
    https_download "${release[3]}" "$CHECKSUM_FILE"
  fi
  "$PYTHON" -B - "$CFD_STAGE" "$ARCH" "${release[1]}" "$CHECKSUM_FILE" <<'PY'
import hashlib, re, struct, sys
from pathlib import Path
path, arch, expected, checks = sys.argv[1:]
data = Path(path).read_bytes()
if checks:
    name = 'cloudflared-linux-' + arch
    matches = []
    for line in Path(checks).read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64})(?:\s+\*?(\S+))?', line.strip())
        if match and (match[2] in (None, name, './' + name)):
            matches.append(match[1])
    if len(matches) != 1:
        sys.exit('Error: No unique published checksum for cloudflared')
    expected = matches[0]
if expected and hashlib.sha256(data).hexdigest() != expected.lower():
    sys.exit('Error: cloudflared SHA256 mismatch')
if not expected:
    print('Warning: Official release publishes no checksum; validating ELF and release version', file=sys.stderr)
if len(data) < 20 or data[:4] != b'\x7fELF' or data[5] != 1:
    sys.exit('Error: Downloaded cloudflared is not a Linux ELF executable')
machine = struct.unpack('<H', data[18:20])[0]
if (data[4], machine) != {'amd64': (2,62), 'arm64': (2,183), 'arm': (1,40), '386': (1,3)}[arch]:
    sys.exit('Error: Downloaded cloudflared architecture mismatch')
PY
  chmod 0755 -- "$CFD_STAGE"
  cfd_version="$("$CFD_STAGE" --version 2>/dev/null)" || die 'Downloaded cloudflared is not executable on this host'
  [[ "$cfd_version" == "cloudflared version ${release[2]}"* ]] || die 'Downloaded cloudflared version does not match official release'
  rm -f -- "$RELEASE_META" "$RELEASE_INFO" "$PACKAGE_STAGE/checksums"
fi
if [ "$CFD_PRESENT" = 0 ] && [ -z "$CFD_STAGE" ]; then
  warn 'cloudflared is missing; CLI --version/env and foreground UI remain available. Install cloudflared before Tunnel operations.'
fi
for cmd in curl dig; do command -v "$cmd" >/dev/null 2>&1 || warn "Optional $cmd missing; required for related network checks"; done

CONFIG_STAGE=""; UNIT_STAGES=(); UNIT_TARGETS=()
if [ "$TUNNEL_SERVICE" = 1 ] && [ "$SYSTEMD_OK" = 1 ]; then
  VALIDATE_CONFIG="$TUNNEL_CONFIG"
  if [ ! -f "$TUNNEL_CONFIG" ]; then
    stage_file "$TUNNEL_CONFIG"; CONFIG_STAGE="$STAGED_FILE"; VALIDATE_CONFIG="$CONFIG_STAGE"
    "$PYTHON" -B - "$CONFIG_STAGE" <<'PY'
import json, os, sys
from pathlib import Path
get = lambda k: os.environ['CFA_INSTALL_' + k]
quote = lambda value: json.dumps(value, ensure_ascii=False)
Path(sys.argv[1]).write_text('tunnel: ' + quote(get('TUNNEL_ID')) + '\ncredentials-file: ' + quote(get('CREDENTIALS')) +
    '\ningress:\n  - hostname: ' + quote(get('HOST')) + '\n    service: ' + quote(get('BACKEND')) +
    '\n  - service: http_status:404\n', encoding='utf-8')
PY
    chmod 0600 -- "$CONFIG_STAGE"
  fi
  VALIDATOR="${CFD_STAGE:-$CLOUDFLARED_BIN}"
  "$VALIDATOR" --config "$VALIDATE_CONFIG" tunnel ingress validate >/dev/null || die 'Tunnel config failed ingress validation; no installation replaced'
fi
make_unit() {
  local name="$1" kind="$2"
  stage_file "$SYSTEMD_DIR/$name"
  "$PYTHON" -B - "$STAGED_FILE" "$kind" <<'PY'
import os, sys
from pathlib import Path
get = lambda k: os.environ['CFA_INSTALL_' + k]
def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
if sys.argv[2] == 'ui':
    command = quote(get('BIN') + '/cf-tunnel-ui') + ' --host 127.0.0.1 --port 8642'
    description = 'cf-tunnel-accel loopback web console'
else:
    command = quote(get('CFD')) + ' --no-autoupdate --config ' + quote(get('TUNNEL_CONFIG')) + ' tunnel run'
    description = 'cf-tunnel-accel explicitly selected Tunnel'
text = '[Unit]\nDescription=' + description + '\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nExecStart=' + command + '\nRestart=on-failure\nRestartSec=5s\nTimeoutStopSec=20\nNoNewPrivileges=yes\nPrivateTmp=yes\n\n[Install]\nWantedBy=multi-user.target\n'
Path(sys.argv[1]).write_text(text, encoding='utf-8')
PY
  chmod 0644 -- "$STAGED_FILE"
  UNIT_STAGES+=("$STAGED_FILE"); UNIT_TARGETS+=("$SYSTEMD_DIR/$name")
}
if [ "$SYSTEMD_OK" = 1 ]; then
  [ "$TUNNEL_SERVICE" = 0 ] || make_unit "$SERVICE_NAME" tunnel
  [ "$UI_SERVICE" = 0 ] || make_unit "$UI_SERVICE_NAME" ui
fi
ensure_dir "$LINK_DIR"
LINK_STAGES=()
for name in cf-tunnel-accel cf-tunnel-ui; do
  stage_file "$LINK_DIR/$name"; rm -f -- "$STAGED_FILE"
  if [ "$name" = cf-tunnel-accel ]; then target="$BIN_DIR/launchers/$name"; else target="$BIN_DIR/$name"; fi
  ln -s -- "$target" "$STAGED_FILE"; LINK_STAGES+=("$STAGED_FILE")
done
# Only local --version smoke test, against the staged source. No startup or API self-test.
CF_TUNNEL_ACCEL_ENV_FILE="$ENV_STAGE" "$PYTHON" -B "$PACKAGE_STAGE/cf-tunnel-accel" --version >/dev/null || die 'Staged CLI failed --version'
replace "$PACKAGE_STAGE" "$BIN_DIR"
replace "$ENV_STAGE" "$ENV_FILE"
[ -z "$TOKEN_STAGE" ] || replace "$TOKEN_STAGE" "$TOKEN_FILE"
[ -z "$CFD_STAGE" ] || replace "$CFD_STAGE" "$CLOUDFLARED_BIN"
[ -z "$CONFIG_STAGE" ] || replace "$CONFIG_STAGE" "$TUNNEL_CONFIG"
replace "${LINK_STAGES[0]}" "$LINK_DIR/cf-tunnel-accel"
replace "${LINK_STAGES[1]}" "$LINK_DIR/cf-tunnel-ui"
for ((i=0; i<${#UNIT_STAGES[@]}; i++)); do replace "${UNIT_STAGES[i]}" "${UNIT_TARGETS[i]}"; done
if [ "${#UNIT_STAGES[@]}" -gt 0 ]; then
  RELOADED=1
  systemctl daemon-reload || die 'systemd daemon-reload failed'
fi
COMMITTED=1
info 'Installed CLI + UI successfully. Previous artifacts kept as adjacent .bak.* backups.'
info "Run: $LINK_DIR/cf-tunnel-accel env"
info "UI (foreground, loopback by default): $LINK_DIR/cf-tunnel-ui"
for unit in "${UNIT_TARGETS[@]}"; do info "Unit installed, not enabled or started: $(basename -- "$unit")"; done
