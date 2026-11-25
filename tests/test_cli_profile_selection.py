"""Offline profile selection contract; all files and credentials are fixtures."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'cf-tunnel-accel'
spec = importlib.util.spec_from_file_location('profile_selection_config', ROOT / 'cfa_config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class ProfileSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.folder = self.home / '.config/cf-tunnel-accel'
        self.folder.mkdir(parents=True)
        self.process = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}
        self.entries = []
        for name in ('alpha', 'beta'):
            env_file = self.home / (name + '.env')
            env_file.write_text(self.values_text(name), encoding='utf-8')
            self.entries.append({'id': name, 'name': name.title(), 'env_file': str(env_file)})
        self.registry = self.folder / 'profiles.json'
        self.registry.write_text(json.dumps({'profiles': self.entries}), encoding='utf-8')

    def values_text(self, name):
        return ('CF_TUNNEL_ACCEL_TUNNEL_ID=11111111-1111-1111-1111-111111111111\n'
                f'CF_TUNNEL_ACCEL_CONFIG={self.home / (name + ".yml")}\n'
                f'CF_TUNNEL_ACCEL_SERVICE=cloudflared-{name}.service\n')

    def select(self, args, process=None):
        return config.select_cli_profile(self.process if process is None else process, ['cli', *args])

    def markers(self, text):
        (self.folder / 'env').write_text(text, encoding='utf-8')

    def test_no_selection_ignores_existing_registry_and_credentials(self):
        self.markers('CF_TUNNEL_ACCEL_TOKEN_FILE=/must-not-read\nCF_API_TOKEN=bootstrap-fixture\n')
        self.registry.write_text('broken registry', encoding='utf-8')
        original = Path.read_text
        reads = []
        def read(path, *args, **kwargs):
            reads.append(path)
            if path != self.folder / 'env':
                raise AssertionError(f'unexpected read: {path}')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', read):
            effective, argv, metadata = self.select(['env'])
        self.assertEqual(effective, self.process)
        self.assertEqual(argv, ['cli', 'env'])
        self.assertEqual(metadata, {})
        self.assertEqual(reads, [self.folder / 'env'])

    def test_selected_profile_does_not_read_default_token_or_unselected_env(self):
        self.markers('CF_TUNNEL_ACCEL_PROFILE=beta\nCF_API_TOKEN=ignored-fixture\n')
        original = Path.read_text
        allowed = {self.folder / 'env', self.registry, Path(self.entries[0]['env_file'])}
        def read(path, *args, **kwargs):
            self.assertIn(path, allowed, f'unselected file was read: {path}')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', read):
            env, argv, meta = self.select(['--profile=alpha', 'env'])
        self.assertEqual(env['CF_TUNNEL_ACCEL_SERVICE'], 'cloudflared-alpha.service')
        self.assertNotIn('CF_API_TOKEN', env)
        self.assertEqual(argv, ['cli', 'env'])
        self.assertEqual(meta, {'profile_id': 'alpha', 'name': 'Alpha', 'registry': str(self.registry)})
        self.assertNotIn('TOKEN', json.dumps(meta))

    def test_flags_override_process_and_file_markers(self):
        self.markers('CF_TUNNEL_ACCEL_PROFILE=beta\nCF_TUNNEL_ACCEL_PROFILES=/bad-registry\n')
        process = dict(self.process, CF_TUNNEL_ACCEL_PROFILE='beta', CF_TUNNEL_ACCEL_PROFILES='/bad-process')
        for args in (['--profile', 'alpha', '--profiles', str(self.registry), 'env'],
                     ['--profiles=' + str(self.registry), '--profile=alpha', 'env']):
            with self.subTest(args=args):
                env, argv, meta = self.select(args, process)
                self.assertEqual(meta['profile_id'], 'alpha')
                self.assertEqual(meta['registry'], str(self.registry))
                self.assertEqual(argv, ['cli', 'env'])
                self.assertEqual(env['CF_TUNNEL_ACCEL_ENV_FILE'], self.entries[0]['env_file'])
        self.assertEqual(process['CF_TUNNEL_ACCEL_PROFILE'], 'beta')

    def test_process_markers_override_file_markers(self):
        self.markers('CF_TUNNEL_ACCEL_PROFILE=beta\nCF_TUNNEL_ACCEL_PROFILES=/bad-file\n')
        process = dict(self.process, CF_TUNNEL_ACCEL_PROFILE='alpha',
                       CF_TUNNEL_ACCEL_PROFILES=str(self.registry))
        self.assertEqual(self.select(['env'], process)[2]['profile_id'], 'alpha')

    def test_file_markers_select_profile(self):
        self.markers(f'CF_TUNNEL_ACCEL_PROFILE=beta\nCF_TUNNEL_ACCEL_PROFILES={self.registry}\n')
        self.assertEqual(self.select(['env'])[2]['profile_id'], 'beta')

    def test_registry_only_never_picks_first(self):
        for process, args in ((self.process, ['--profiles', str(self.registry), 'env']),
                              (dict(self.process, CF_TUNNEL_ACCEL_PROFILES=str(self.registry)), ['env'])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.select(args, process)
        self.markers('CF_TUNNEL_ACCEL_PROFILE=beta\n')
        self.assertEqual(self.select(['--profiles', str(self.registry), 'env'])[2]['profile_id'], 'beta')

    def test_subcommand_flags_are_preserved(self):
        self.assertEqual(self.select(['ui', '--profiles', '/ui-owned'])[1],
                         ['cli', 'ui', '--profiles', '/ui-owned'])
        self.assertEqual(self.select(['--profile=alpha', 'ui', '--profiles=/ui-owned'])[1],
                         ['cli', 'ui', '--profiles=/ui-owned'])

    def test_invalid_global_flags_fail(self):
        cases = [['--profile'], ['--profiles'], ['--profile='], ['--profiles='],
                 ['--profile', '--profiles', str(self.registry)], ['--profile', 'bad/id'],
                 ['--profile=alpha', '--profile', 'alpha'],
                 ['--profiles=' + str(self.registry), '--profiles', str(self.registry)],
                 ['--profile', ' alpha'], ['--profiles', '\n']]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.select(args)

    def test_unknown_profile_and_missing_registry_fail_closed(self):
        with self.assertRaises(ValueError):
            self.select(['--profile', 'unknown', 'env'])
        self.registry.unlink()
        with self.assertRaises(FileNotFoundError):
            self.select(['--profile=alpha', 'env'])

    def test_selected_missing_env_fails_unselected_missing_env_does_not(self):
        Path(self.entries[1]['env_file']).unlink()
        self.assertEqual(self.select(['--profile=alpha', 'env'])[2]['profile_id'], 'alpha')
        with self.assertRaises(ValueError):
            self.select(['--profile=beta', 'env'])

    def test_registry_schema_rejects_malformed_entries(self):
        invalid = [None, [], {}, {'profiles': []}, {'profiles': self.entries, 'extra': 1},
                   {'profiles': [self.entries[0], self.entries[0]]}]
        for field, value in [('id', 'bad/id'), ('id', ''), ('name', ' '),
                             ('env_file', 'relative.env'), ('name', 5)]:
            invalid.append({'profiles': [dict(self.entries[0], **{field: value})]})
        invalid.append({'profiles': [dict(self.entries[0], extra='x')]})
        for data in invalid:
            with self.subTest(data=data):
                self.registry.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(ValueError):
                    config.read_profile_registry(self.registry)
        self.registry.write_text('{invalid', encoding='utf-8')
        with self.assertRaises(ValueError):
            config.read_profile_registry(self.registry)

    def test_required_values_are_explicit_nonblank_and_config_absolute(self):
        values = config.read_env(Path(self.entries[0]['env_file']))
        for key in config._PROFILE_REQUIRED:
            for value in (None, '', ' '):
                broken = dict(values)
                if value is None:
                    broken.pop(key)
                else:
                    broken[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    config.merge_profile_environment(values, self.entries[0], broken)
        with self.assertRaises(ValueError):
            config.merge_profile_environment({}, self.entries[0],
                                             dict(values, CF_TUNNEL_ACCEL_CONFIG='relative.yml'))

    def test_credential_group_cf_overrides_env_redirect_and_container_boundary(self):
        values = config.read_env(Path(self.entries[0]['env_file']))
        process = dict(self.process, **{key: 'inherited-fixture' for key in config.TOKEN_KEYS},
                       CF_TUNNEL_ACCEL_RUNTIME='container', CF_OTHER='old', OTHER='keep')
        for credential in config.TOKEN_KEYS:
            with self.subTest(credential=credential):
                chosen = dict(values, **{credential: 'selected-fixture', 'CF_OTHER': 'new', 'OTHER': 'file',
                                       'CF_TUNNEL_ACCEL_ENV_FILE': '/redirect',
                                       'CF_TUNNEL_ACCEL_RUNTIME': 'host'})
                env = config.merge_profile_environment(process, self.entries[0], chosen)
                self.assertEqual(env[credential], 'selected-fixture')
                self.assertTrue(all(key not in env for key in config.TOKEN_KEYS if key != credential))
                self.assertEqual(env['CF_OTHER'], 'new')
                self.assertEqual(env['OTHER'], 'keep')
                self.assertEqual(env['CF_TUNNEL_ACCEL_RUNTIME'], 'container')
                self.assertEqual(env['CF_TUNNEL_ACCEL_ENV_FILE'], self.entries[0]['env_file'])
        self.assertTrue(all(process[key] == 'inherited-fixture' for key in config.TOKEN_KEYS))

    def test_no_explicit_profile_credentials_preserves_process_group(self):
        process = dict(self.process, CF_API_TOKEN='process-fixture')
        self.assertEqual(self.select(['--profile=alpha'], process)[0]['CF_API_TOKEN'], 'process-fixture')

    def test_bootstrap_explicit_env_config_dir_and_xdg_paths(self):
        for kind in ('env', 'dir', 'xdg'):
            with self.subTest(kind=kind):
                folder = self.home / kind
                folder.mkdir()
                process = dict(self.process)
                if kind == 'env':
                    bootstrap = folder / 'custom.env'
                    process['CF_TUNNEL_ACCEL_ENV_FILE'] = str(bootstrap)
                    registry = self.registry
                else:
                    if kind == 'dir':
                        process['CF_TUNNEL_ACCEL_CONFIG_DIR'] = str(folder)
                    else:
                        process['XDG_CONFIG_HOME'] = str(folder)
                        folder = folder / 'cf-tunnel-accel'
                        folder.mkdir()
                    bootstrap, registry = folder / 'env', folder / 'profiles.json'
                    registry.write_text(json.dumps({'profiles': self.entries}), encoding='utf-8')
                bootstrap.write_text(f'CF_TUNNEL_ACCEL_PROFILE=beta\nCF_TUNNEL_ACCEL_PROFILES={registry}\n')
                self.assertEqual(self.select(['env'], process)[2]['profile_id'], 'beta')

    def test_actual_cli_honors_default_marker_and_rejects_invalid_id_without_fallback(self):
        self.markers(f'CF_TUNNEL_ACCEL_PROFILE=beta\nCF_TUNNEL_ACCEL_PROFILES={self.registry}\n')
        result = subprocess.run([sys.executable, str(CLI), 'env'], env=self.process,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn(str(self.home / 'beta.env'), result.stdout)
        self.assertIn('CLI profile = beta', result.stdout)
        self.assertNotIn(str(self.home / 'alpha.env'), result.stdout)
        result = subprocess.run([sys.executable, str(CLI), '--profile', 'missing', 'env'],
                                env=self.process, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('配置选择失败', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('CLI profile', result.stdout)

    def test_module_import_does_not_consume_parent_process_profile_flags(self):
        import importlib.machinery
        import os
        with mock.patch.dict(os.environ, self.process, clear=True), \
                mock.patch.object(sys, 'argv', ['server.py', '--profile', 'not-a-cli-profile']):
            loader = importlib.machinery.SourceFileLoader('imported_cli_profile_boundary', str(CLI))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
        self.assertEqual(module.ENV_FILE, self.folder / 'env')
        self.assertFalse(module._PROFILE_META)


    def test_actual_cli_global_profile_flags_print_matching_paths_and_service(self):
        # Exercise the extensionless entry point before any settings/credentials are resolved.
        for args in (['--profile', 'alpha', '--profiles', str(self.registry), 'env'],
                     ['--profiles=' + str(self.registry), '--profile=beta', 'env']):
            name = 'beta' if '--profile=beta' in args else 'alpha'
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(CLI), *args], env=self.process,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertIn(str(self.home / (name + '.env')), result.stdout)
                self.assertIn(str(self.home / (name + '.yml')), result.stdout)
                self.assertIn('cloudflared-' + name + '.service', result.stdout)


if __name__ == '__main__':
    unittest.main()
