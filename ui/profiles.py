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

from functools import lru_cache
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType
from uuid import uuid4


@lru_cache(maxsize=1)
def _config_parser() -> ModuleType:
    # Installed CLI packages may differ; always use the parser beside this UI.
    parser_path = Path(__file__).resolve().parent.parent / 'cfa_config.py'
    spec = importlib.util.spec_from_file_location('_cfa_profile_config', parser_path)
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    return parser


def _registry(path: Path) -> list[dict[str, str]]:
    return _config_parser().read_profile_registry(path)


def _env_values(path: Path) -> dict[str, str]:
    # Keep exactly one parser for CLI and registry files; explicit selection is strict.
    if not path.exists():
        path.read_text(encoding='utf-8')  # Preserve actionable FileNotFoundError for explicit files.
    return _config_parser().read_env(path, required=True)


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
                env = _config_parser().merge_profile_environment(baseline, entry, values=values)
                config = Path(values["CF_TUNNEL_ACCEL_CONFIG"].strip()).resolve()
                service = values["CF_TUNNEL_ACCEL_SERVICE"].strip()
                # systemctl accepts both 'foo' and 'foo.service'.
                service = service if "." in service else service + ".service"
                if config in configs or service in services:
                    raise ValueError("profiles must use distinct config paths and services")
                configs.add(config)
                services.add(service)
            # Container entrypoint is a process-level boundary, not a profile preference.
            if baseline.get('CF_TUNNEL_ACCEL_RUNTIME') == 'container':
                env['CF_TUNNEL_ACCEL_RUNTIME'] = 'container'
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
