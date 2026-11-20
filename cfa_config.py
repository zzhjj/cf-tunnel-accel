"""Local configuration for cf-tunnel-accel; no imports of or requests to Cloudflare.

Resolve settings once per CLI module import. Functions never log credential values.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Mapping
from uuid import UUID

TOKEN_ENV_KEYS = ('CF_TUNNEL_ACCEL_API_TOKEN', 'CF_API_TOKEN', 'CLOUDFLARE_API_TOKEN')
TOKEN_KEYS = (*TOKEN_ENV_KEYS, 'CF_TUNNEL_ACCEL_TOKEN_FILE')
DOMAIN = re.compile(r'(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+\Z')
SERVICE = re.compile(r'[A-Za-z0-9_@.:-]+(?:\.service)?\Z')


def read_env(path: Path, required: bool = False) -> dict[str, str]:
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        if required:
            raise ValueError(f'配置文件不存在: {path}') from None
        return {}
    except OSError as exc:
        raise ValueError(f'配置文件读取失败: {path} ({exc.__class__.__name__})') from None
    values = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()):
            raise ValueError(f'配置格式错误: {path}:{number}，应为 KEY=VALUE')
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f'配置引号不匹配: {path}:{number}')
            value = value[1:-1]
        values.setdefault(key.strip(), value)
    return values


def resolve_settings(environ: Mapping[str, str]) -> dict:
    process = dict(environ)
    home = Path(process.get('HOME') or Path.home()).expanduser()
    config_dir = Path(process.get('CF_TUNNEL_ACCEL_CONFIG_DIR') or
                      str(Path(process.get('XDG_CONFIG_HOME') or home / '.config') / 'cf-tunnel-accel')).expanduser()
    config_file = Path(process.get('CF_TUNNEL_ACCEL_ENV_FILE') or config_dir / 'env').expanduser()
    file_values = read_env(config_file)  # Optional in default mode; registry files are validated by their loader.
    effective = {**file_values, **process}
    # Precedence applies to the credential source GROUP, not each spelling.
    source_group = process if any(process.get(k, '').strip() for k in TOKEN_KEYS) else file_values
    token_file = Path(source_group.get('CF_TUNNEL_ACCEL_TOKEN_FILE') or config_dir / 'token').expanduser()
    token, source = '', 'missing'
    for key in TOKEN_ENV_KEYS:
        if source_group.get(key, '').strip():
            token = source_group[key].strip()
            source = 'environment' if source_group is process else 'configuration'
            break
    if not token:
        try:
            token = token_file.read_text(encoding='utf-8').strip()
            source = 'file' if token else 'missing'
        except FileNotFoundError:
            pass
        except OSError:
            source = 'unreadable-file'
    def val(key, default=''):
        return effective.get('CF_TUNNEL_ACCEL_' + key, '').strip() or str(default)
    name = val('TUNNEL_NAME', 'default')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', name):
        raise ValueError('TUNNEL_NAME 只允许字母、数字、下划线或连字符')
    default_config = home / '.cloudflared' / ('config.yml' if name == 'default' else f'config-{name}.yml')
    tunnel_config = Path(val('CONFIG', default_config)).expanduser()
    tunnel_id = val('TUNNEL_ID')
    if not tunnel_id and tunnel_config.is_file():
        # Import is local, bounded to explicitly selected config or own HOME.
        import cfa_ingress
        tunnel_id = cfa_ingress.get_tunnel_id(tunnel_config.read_text(encoding='utf-8'))
    aux = val('AUX_ZONE').lower().rstrip('.')
    account = val('ACCOUNT_ID')
    scope = {'account_id': account, 'aux_zone': aux, 'env_file': str(config_file.resolve()),
             'token_file': str(token_file.resolve()) if source == 'file' else '',
             'credential_scope': hashlib.sha256(token.encode()).hexdigest() if token else ''}
    scope_id = hashlib.sha256(repr(sorted(scope.items())).encode()).hexdigest()[:16]
    return {
        'env': effective, 'file_values': file_values, 'config_dir': config_dir, 'env_file': config_file,
        'token': token, 'token_file': token_file, 'token_source': source,
        'account_id': account, 'tunnel_id': tunnel_id, 'tunnel_name': name,
        'config': tunnel_config, 'service': val('SERVICE', 'cloudflared.service' if name == 'default' else f'cloudflared-{name}.service'),
        'aux_zone': aux, 'aux_zone_id': val('AUX_ZONE_ID'),
        'fallback_origin': val('FALLBACK_ORIGIN', f'origin.{aux}' if aux else ''),
        'preferred_entry': val('PREFERRED_ENTRY', f'cdn.{aux}' if aux else ''),
        'preferred_target': val('PREFERRED_TARGET'), 'custom_origin': val('CUSTOM_ORIGIN').lower().rstrip('.'),
        'fallback_service': val('FALLBACK_SERVICE', 'http_status:404'),
        'candidates': [x.strip() for x in val('CDN_CANDIDATES').split(',') if x.strip()],
        'probe_hostname': val('PROBE_HOSTNAME').lower().rstrip('.'),
        'cloudflared': val('CLOUDFLARED', shutil.which('cloudflared') or 'cloudflared'),
        'lock': Path(val('LOCK', config_dir / 'mutation.lock')).expanduser(),
        'zone_cache': Path(val('ZONE_CACHE', config_dir / f'zones-{scope_id}.json')).expanduser(),
        'cache_scope': scope,
    }


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def initialize(argv: list[str], settings: dict) -> Path:
    parser = argparse.ArgumentParser(prog='cf-tunnel-accel init', description='只生成本地配置，不创建云端资源')
    for option in ('account-id', 'tunnel-id', 'tunnel-name', 'config', 'service', 'aux-zone', 'aux-zone-id',
                   'preferred-target', 'cdn-candidates', 'probe-hostname', 'custom-origin', 'fallback-service', 'token-file'):
        parser.add_argument('--' + option)
    parser.add_argument('--output', type=Path, default=settings['env_file'])
    parser.add_argument('--non-interactive', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    config = Path(args.config or settings['config']).expanduser().resolve()
    existing = read_env(args.output) if args.output.exists() else {}
    is_configured = bool(existing.get('CF_TUNNEL_ACCEL_TUNNEL_ID') or existing.get('CF_TUNNEL_ACCEL_AUX_ZONE'))
    if is_configured and not args.force:
        raise ValueError(f'配置已存在: {args.output}；检查后用 --force 覆盖')
    values = vars(args)
    required = {
        'aux_zone': ('Cloudflare for SaaS 辅助域名', settings.get('aux_zone', '')),
        'service': ('管理的 systemd 服务名', settings.get('service', '')),
    }
    for key, (label, default) in required.items():
        if not values[key] and not args.non_interactive:
            prompt = label + (f' [{default}]' if default else '') + ': '
            values[key] = input(prompt).strip() or default
        if not values[key]:
            raise ValueError(f'缺少 --{key.replace("_", "-")}')
    aux = values['aux_zone'].lower().rstrip('.')
    if not DOMAIN.fullmatch(aux):
        raise ValueError('辅助域名格式错误')
    if not SERVICE.fullmatch(values['service']) or values['service'].startswith('-'):
        raise ValueError('systemd 服务名格式错误')
    tunnel = args.tunnel_id or settings.get('tunnel_id', '')
    if not tunnel:
        if not config.is_file():
            raise ValueError(f'未找到 Tunnel 配置: {config}；请先用 cloudflared tunnel create 生成，或用 --config 指定')
        import cfa_ingress
        tunnel = cfa_ingress.get_tunnel_id(config.read_text(encoding='utf-8'))
    try:
        UUID(tunnel)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('需要有效 Tunnel UUID：--tunnel-id 或现有 --config 顶层 tunnel 字段') from None
    for value in (args.account_id, args.aux_zone_id):
        if value and not re.fullmatch(r'[0-9a-fA-F]{32}', value):
            raise ValueError('Account ID / Zone ID 应为 32 位十六进制')
    for value in (args.preferred_target, args.probe_hostname, args.custom_origin):
        if value and not DOMAIN.fullmatch(value.lower().rstrip('.')):
            raise ValueError('候选目标、业务探测域名和独立回退源需使用完整域名')
    rows = {
        'TUNNEL_ID': tunnel, 'TUNNEL_NAME': args.tunnel_name or 'default', 'CONFIG': str(config),
        'SERVICE': args.service, 'AUX_ZONE': aux, 'ACCOUNT_ID': args.account_id or '',
        'AUX_ZONE_ID': args.aux_zone_id or '', 'PREFERRED_TARGET': args.preferred_target or '',
        'CDN_CANDIDATES': args.cdn_candidates or args.preferred_target or '',
        'PROBE_HOSTNAME': args.probe_hostname or '', 'CUSTOM_ORIGIN': args.custom_origin or '',
        'FALLBACK_SERVICE': args.fallback_service or 'http_status:404',
        'TOKEN_FILE': str(Path(args.token_file).expanduser().resolve() if args.token_file else settings['token_file']),
    }
    for key, value in rows.items():
        if any(c in str(value) for c in '\r\n\x00'):
            raise ValueError(f'{key} 含非法控制字符')
    text = '# cf-tunnel-accel 配置；环境变量优先。凭据另放 0600 Token 文件。\n'
    replacements = {f'CF_TUNNEL_ACCEL_{k}': str(v) for k, v in rows.items() if v}
    original = args.output.read_text(encoding='utf-8').splitlines() if args.output.exists() else []
    text += '\n'.join(line for line in original if line.partition('=')[0].strip() not in replacements)
    if not text.endswith('\n'):
        text += '\n'
    text += ''.join(f'{k}={v}\n' for k, v in replacements.items())
    write_private(args.output, text)
    return args.output
