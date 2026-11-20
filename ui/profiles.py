"""Startup-only loader for administrator-owned CLI profile definitions.

Each profile imports the same CLI with a validated env-file selection. Import
environment changes are restored before starting threads. General settings use
profile values over inherited values; an explicit credential source clears all
inherited credential aliases/files as one group. No arbitrary paths are accepted
from HTTP requests.

CLI 2.x resolves a per-module settings snapshot and does not read live os.environ
from runtime helpers. Subprocesses still inherit the server environment; this is
not a process sandbox. Behaviour tests cover bearer credentials, scoped caches
and cross-process mutation locks. The AST guard is only an additional regression
alarm, not proof of all isolation properties. Full worker-process isolation is
not implemented. Default mode keeps the CLI's normal environment/file precedence.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from types import ModuleType
from uuid import uuid4


_REQUIRED = (
    "CF_TUNNEL_ACCEL_TUNNEL_ID",
    "CF_TUNNEL_ACCEL_CONFIG",
    "CF_TUNNEL_ACCEL_SERVICE",
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def _registry(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"profiles"}:
        raise ValueError("registry must contain only 'profiles'")
    entries = data["profiles"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("profiles must be a nonempty list")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "name", "env_file"}:
            raise ValueError("profile must contain only id, name, env_file")
        if any(not isinstance(v, str) or not v.strip() for v in entry.values()):
            raise ValueError("profile fields must be nonempty strings")
        if not _ID.fullmatch(entry["id"]):
            raise ValueError("profile id must contain only letters, digits, '_' or '-'")
        if entry["id"] in seen:
            raise ValueError("duplicate profile id")
        seen.add(entry["id"])
        if not Path(entry["env_file"]).is_absolute():
            raise ValueError("profile env_file must be absolute")
    return entries


def _env_values(path: Path) -> dict[str, str]:
    # Keep exactly one parser for CLI and registry files; explicit selection is strict.
    if not path.exists():
        path.read_text(encoding='utf-8')  # Preserve actionable FileNotFoundError for explicit files.
    import importlib.util
    parser_path = Path(__file__).resolve().parent.parent / 'cfa_config.py'
    spec = importlib.util.spec_from_file_location('_cfa_profile_config', parser_path)
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    return parser.read_env(path, required=True)


def _import_cli(path: Path) -> ModuleType:
    name = "_cf_tunnel_profile_" + uuid4().hex
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def profile_metadata(module: ModuleType) -> dict[str, str | None]:
    """Return display metadata only, as a copy (no environment snapshot)."""
    return dict(module._profile_metadata)


def load_profiles(
    module_path: str | Path, registry_path: str | Path | None = None
) -> dict[str, ModuleType]:
    """Load independent modules at startup, restoring the entire environment.

    Invalid registries/settings raise ValueError; JSON and filesystem failures
    propagate. Failure returns no partial result. None alone selects default
    mode; a missing/invalid explicitly selected registry never falls back.
    """
    baseline = dict(os.environ)
    modules = {}
    try:
        entries = _registry(Path(registry_path)) if registry_path is not None else [
            {"id": "default", "name": "Default", "env_file": None}
        ]
        # Validate every file and local target before executing any CLI code.
        environments = []
        configs, services = set(), set()
        for entry in entries:
            env = dict(baseline)
            if registry_path is not None:
                values = _env_values(Path(entry["env_file"]))
                if any(not values.get(key, "").strip() for key in _REQUIRED):
                    raise ValueError("profile env file must explicitly set tunnel/config/service")
                config = Path(values["CF_TUNNEL_ACCEL_CONFIG"].strip()).resolve()
                service = values["CF_TUNNEL_ACCEL_SERVICE"].strip()
                # systemctl accepts both 'foo' and 'foo.service'.
                service = service if "." in service else service + ".service"
                if config in configs or service in services:
                    raise ValueError("profiles must use distinct config paths and services")
                configs.add(config)
                services.add(service)
                credential_keys = ('CF_TUNNEL_ACCEL_API_TOKEN', 'CF_API_TOKEN',
                                   'CLOUDFLARE_API_TOKEN', 'CF_TUNNEL_ACCEL_TOKEN_FILE')
                if any(values.get(key, '').strip() for key in credential_keys):
                    # Explicit profile credential source wins as a group, including alias/file.
                    for key in credential_keys:
                        env.pop(key, None)
                if not Path(values['CF_TUNNEL_ACCEL_CONFIG']).is_absolute():
                    raise ValueError('profile config must be an absolute path')
                for key, value in values.items():
                    if key.startswith('CF_') or key == 'CLOUDFLARE_API_TOKEN' or key not in env:
                        env[key] = value
                # A file cannot redirect the CLI to a different env file.
                env["CF_TUNNEL_ACCEL_ENV_FILE"] = entry["env_file"]
            environments.append(env)
        for entry, env in zip(entries, environments):
            os.environ.clear()
            os.environ.update(env)
            try:
                module = _import_cli(Path(module_path))
                if registry_path is not None and getattr(module, '_CONFIG_ERROR', ''):
                    sys.modules.pop(module.__name__, None)
                    raise ValueError(f'profile {entry["id"]} 配置无效: {module._CONFIG_ERROR}')
                module._profile_metadata = dict(entry)
                modules[entry["id"]] = module
            finally:
                os.environ.clear()
                os.environ.update(baseline)
        return modules
    except BaseException:
        for module in modules.values():
            sys.modules.pop(module.__name__, None)
        raise
    finally:
        os.environ.clear()
        os.environ.update(baseline)
