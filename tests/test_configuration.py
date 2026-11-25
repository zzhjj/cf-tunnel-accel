"""Public configuration contract: isolated process environments, no real credentials."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'cf-tunnel-accel'


# Global CLI selectors apply before the command; UI selectors remain after `ui`.
def load_cli(env):
    with mock.patch.dict(os.environ, env, clear=True):
        name = 'config_contract_' + str(id(env))
        loader = importlib.machinery.SourceFileLoader(name, str(CLI))
        spec = importlib.util.spec_from_loader(name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module


class GenericConfiguration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', ''), 'LANG': 'C.UTF-8'}

    def test_empty_home_has_no_infrastructure_seeds_and_uses_own_directories(self):
        cli = load_cli(self.env)
        self.assertEqual(cli.ACCOUNT_ID, '')
        self.assertEqual(cli.TUNNEL_ID, '')
        self.assertEqual(cli.AUX_ZONE_NAME, '')
        self.assertEqual(cli.AUX_ZONE_ID, '')
        self.assertEqual(cli.PREFERRED_ENTRY, '')
        self.assertEqual(cli.FALLBACK_ORIGIN, '')
        self.assertEqual(cli.KNOWN_ZONES, {})
        self.assertEqual(cli.CDN_CANDIDATES, [])
        self.assertEqual(cli.CONFIG_DIR, self.home / '.config/cf-tunnel-accel')
        self.assertEqual(cli.TOKEN_FILE, cli.CONFIG_DIR / 'token')
        self.assertTrue(cli.LOCK_PATH.is_relative_to(cli.CONFIG_DIR))
        self.assertEqual(list(self.home.iterdir()), [], 'import must not create state')

    def test_xdg_and_explicit_paths_do_not_reach_other_home(self):
        env = dict(self.env, XDG_CONFIG_HOME=str(self.home / 'xdg'),
                   CF_TUNNEL_ACCEL_CONFIG=str(self.home / 'tunnels/team.yml'),
                   CF_TUNNEL_ACCEL_TUNNEL_NAME='team')
        cli = load_cli(env)
        self.assertEqual(cli.ENV_FILE, self.home / 'xdg/cf-tunnel-accel/env')
        self.assertEqual(cli.CONFIG_PATH, self.home / 'tunnels/team.yml')
        self.assertEqual(cli.SERVICE_NAME, 'cloudflared-team.service')

    def test_inherited_alias_token_beats_file_canonical_without_exposing_it(self):
        env_file = self.home / 'settings.env'
        env_file.write_text('CF_TUNNEL_ACCEL_API_TOKEN=file-fixture\n')
        cli = load_cli(dict(self.env, CF_TUNNEL_ACCEL_ENV_FILE=str(env_file), CF_API_TOKEN='process-fixture'))
        self.assertTrue(cli.CF_API_TOKEN == 'process-fixture', 'token source precedence must be grouped')
        self.assertEqual(cli.TOKEN_SOURCE, 'environment')
        status = cli.configuration_status()
        self.assertTrue(status['credentials']['configured'])
        self.assertNotIn('process-fixture', json.dumps(status))
        self.assertNotIn('file-fixture', json.dumps(status))

    def test_explicit_token_file_beats_other_credential_in_configuration_file(self):
        token_file = self.home / 'chosen-token'
        token_file.write_text('chosen-fixture')
        env_file = self.home / 'settings.env'
        env_file.write_text('CF_API_TOKEN=file-env-fixture\n')
        cli = load_cli(dict(self.env, CF_TUNNEL_ACCEL_ENV_FILE=str(env_file),
                            CF_TUNNEL_ACCEL_TOKEN_FILE=str(token_file)))
        self.assertTrue(cli.CF_API_TOKEN == 'chosen-fixture', 'explicit file source must win')

    def test_standard_cloudflare_token_alias_is_supported_without_exposing_it(self):
        cli = load_cli(dict(self.env, CLOUDFLARE_API_TOKEN='standard-fixture'))
        self.assertTrue(cli.CF_API_TOKEN == 'standard-fixture')
        self.assertEqual(cli.TOKEN_SOURCE, 'environment')
        self.assertNotIn('standard-fixture', json.dumps(cli.configuration_status()))

    def test_incomplete_setup_fails_before_external_writes(self):
        cli = load_cli(self.env)
        with mock.patch.object(cli, 'api') as api, mock.patch.object(cli, 'run') as run:
            with self.assertRaises(cli.CFError):
                cli.prepare_configuration(require_local=True)
            api.assert_not_called()
            run.assert_not_called()
        self.assertFalse(cli.configuration_status()['ready'])

    def test_add_cli_rejects_installer_scaffold_before_prompt_network_or_lock(self):
        folder = self.home / '.config/cf-tunnel-accel'
        folder.mkdir(parents=True)
        (folder / 'token').write_text('fixture-only')
        (folder / 'env').write_text('CF_TUNNEL_ACCEL_TOKEN_FILE=' + str(folder / 'token') + '\n')
        config = self.home / '.cloudflared/config.yml'
        config.parent.mkdir()
        config.write_text('tunnel: 11111111-1111-1111-1111-111111111111\ningress:\n  - service: http_status:404\n')
        for args in (['add'], ['add', 'example.org'], ['add', 'app.example.org', '18888']):
            cli = load_cli(self.env)
            messages = []
            with mock.patch.object(cli, 'check_token') as token_check, \
                    mock.patch.object(cli, 'exclusive_lock') as lock, \
                    mock.patch.object(cli, 'api') as api, \
                    mock.patch.object(cli, 'add_accelerated_service') as add, \
                    mock.patch('builtins.input') as prompt, \
                    mock.patch.object(cli, 'cprint', side_effect=lambda msg='', color=None: messages.append(str(msg))):
                self.assertNotEqual(cli.main([str(CLI), *args]), 0)
                prompt.assert_not_called()
                token_check.assert_not_called()
                lock.assert_not_called()
                api.assert_not_called()
                add.assert_not_called()
            text = '\n'.join(messages)
            self.assertIn('init', text)
            self.assertIn(str(folder / 'env'), text)
            self.assertNotIn('即将配置', text)
            self.assertFalse(cli.LOCK_PATH.exists())


    def test_interactive_add_prechecks_before_hostname_and_backend_prompts(self):
        cli = load_cli(self.env)
        with mock.patch.object(cli, 'choose_hostname_interactive') as pick, \
                mock.patch.object(cli, 'api') as api, \
                mock.patch('builtins.input') as prompt:
            with self.assertRaises(cli.CFError):
                cli.interactive_add()
            pick.assert_not_called()
            prompt.assert_not_called()
            api.assert_not_called()


    def test_env_is_local_even_with_a_configured_token(self):
        cli = load_cli(dict(self.env, CF_API_TOKEN='fixture-only'))
        with mock.patch.object(cli, 'api', side_effect=AssertionError('env must stay offline')):
            self.assertEqual(cli.main([str(CLI), 'env']), 0)

    def test_tunnel_id_can_be_read_from_explicit_local_config_without_network(self):
        config = self.home / 'tunnel.yml'
        config.write_text('tunnel: 11111111-1111-1111-1111-111111111111\ningress:\n  - service: http_status:404\n')
        cli = load_cli(dict(self.env, CF_TUNNEL_ACCEL_CONFIG=str(config)))
        self.assertEqual(cli.TUNNEL_ID, '11111111-1111-1111-1111-111111111111')

    def test_https_verification_failure_does_not_fall_back_to_insecure_or_tcp_success(self):
        import ssl
        cli = load_cli(self.env)
        failure = ssl.SSLCertVerificationError(1, 'fixture certificate mismatch')
        calls = []
        def verified_request(request, **kwargs):
            calls.append(kwargs.get('context'))
            raise failure
        with mock.patch('urllib.request.urlopen', side_effect=verified_request), \
                mock.patch('socket.create_connection') as tcp:
            ok, detail = cli.probe_local_backend('https://backend.example.org:443', no_tls_verify=False)
        self.assertFalse(ok)
        self.assertTrue(all(context is None for context in calls), 'explicit certificate verification must not silently weaken')
        tcp.assert_not_called()


    def test_certificate_pending_is_not_successful_onboarding(self):
        cli = load_cli(self.env)
        pending = {'status': 'active', 'ssl': {'status': 'pending_deployment'}}
        with mock.patch.object(cli, 'refresh_custom_hostname', return_value=pending), \
                mock.patch.object(cli.time, 'time', side_effect=[0, 0, 50, 100]), \
                mock.patch.object(cli.time, 'sleep'):
            with self.assertRaises(cli.CFError):
                cli.wait_custom_hostname_active('fixture-host', timeout=80)


    def test_unknown_command_returns_nonzero_without_prompt_or_network(self):
        cli = load_cli(self.env)
        with mock.patch.object(cli, 'api') as api, mock.patch('builtins.input', side_effect=AssertionError('no prompt')):
            self.assertNotEqual(cli.main([str(CLI), 'does-not-exist']), 0)
            api.assert_not_called()


    def test_ui_launch_uses_the_immutable_cli_selection_environment(self):
        cli = load_cli(dict(self.env, CF_TUNNEL_ACCEL_ENV_FILE=str(self.home / 'chosen.env')))
        with mock.patch.object(cli, 'run', return_value=mock.Mock(stdout='inactive')), \
                mock.patch.object(cli.subprocess, 'call', return_value=0) as launch, \
                mock.patch.dict(os.environ, {'CF_TUNNEL_ACCEL_ENV_FILE': '/unrelated-live.env'}, clear=True):
            self.assertEqual(cli.launch_ui(['--port', '0']), 0)
        self.assertEqual(launch.call_args.kwargs['env']['CF_TUNNEL_ACCEL_ENV_FILE'], str(self.home / 'chosen.env'))


    def test_menu_reports_the_selected_config_not_a_private_fixture(self):
        config = self.home / 'team-tunnel.yml'
        config.write_text('tunnel: 11111111-1111-1111-1111-111111111111\ningress:\n  - service: http_status:404\n')
        cli = load_cli(dict(self.env, CF_TUNNEL_ACCEL_CONFIG=str(config)))
        printed = []
        with mock.patch.object(cli, 'get_current_preferred_target', return_value=''), \
                mock.patch.object(cli, 'cprint', side_effect=lambda *a, **k: printed.append(' '.join(str(x) for x in a))), \
                mock.patch('builtins.input', return_value='0'):
            cli.menu()
        text = '\n'.join(printed)
        self.assertIn(str(config), text)
        self.assertNotIn('config-home.yml', text)

class InitContract(unittest.TestCase):
    setUp = GenericConfiguration.setUp
    def test_init_accepts_installer_scaffold_without_losing_paths_or_credentials(self):
        folder = self.home / '.config/cf-tunnel-accel'
        folder.mkdir(parents=True)
        settings = folder / 'env'
        settings.write_text('CF_TUNNEL_ACCEL_CLOUDFLARED=/custom/bin/cloudflared\nCF_API_TOKEN=fixture-preserved\n')
        cli = load_cli(self.env)
        args = [str(CLI), 'init', '--non-interactive', '--tunnel-id',
                '11111111-1111-1111-1111-111111111111', '--service', 'example.service',
                '--aux-zone', 'example.net']
        with mock.patch.object(cli, 'api') as api:
            self.assertEqual(cli.main(args), 0)
            api.assert_not_called()
        self.assertIn('CF_TUNNEL_ACCEL_CLOUDFLARED=/custom/bin/cloudflared', settings.read_text())
        self.assertTrue('CF_API_TOKEN=fixture-preserved' in settings.read_text())
        self.assertNotEqual(cli.main(args), 0)


    def test_init_writes_complete_explicit_configuration_in_own_home(self):
        config = self.home / 'tunnel.yml'
        config.write_text('tunnel: 11111111-1111-1111-1111-111111111111\ningress:\n  - service: http_status:404\n')
        cli = load_cli(self.env)
        args = [str(CLI), 'init', '--non-interactive', '--config', str(config),
                '--service', 'cloudflared-team.service', '--aux-zone', 'example.net',
                '--preferred-target', 'edge.example.net', '--probe-hostname', 'app.example.org']
        with mock.patch.object(cli, 'api') as api:
            self.assertEqual(cli.main(args), 0)
            api.assert_not_called()
        settings = self.home / '.config/cf-tunnel-accel/env'
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        text = settings.read_text()
        self.assertIn('CF_TUNNEL_ACCEL_AUX_ZONE=example.net', text)
        self.assertIn('CF_TUNNEL_ACCEL_TUNNEL_ID=11111111-1111-1111-1111-111111111111', text)
        self.assertIn('CF_TUNNEL_ACCEL_PROBE_HOSTNAME=app.example.org', text)
        # An existing configuration is never silently overwritten.
        self.assertNotEqual(cli.main(args), 0)
        self.assertEqual(settings.read_text(), text)


    def test_interactive_init_needs_only_the_auxiliary_zone(self):
        tunnel = self.home / '.cloudflared/config.yml'
        tunnel.parent.mkdir()
        tunnel.write_text('tunnel: 11111111-1111-1111-1111-111111111111\ningress:\n  - service: http_status:404\n')
        token = self.home / '.config/cf-tunnel-accel/token'
        token.parent.mkdir(parents=True)
        token.write_text('fixture-token')
        cli = load_cli(self.env)
        with mock.patch('builtins.input', side_effect=['example.net', '']) as prompt:
            self.assertEqual(cli.main([str(CLI), 'init']), 0)
        self.assertEqual(prompt.call_count, 2)
        text = (self.home / '.config/cf-tunnel-accel/env').read_text()
        self.assertIn('CF_TUNNEL_ACCEL_AUX_ZONE=example.net', text)
        self.assertIn('CF_TUNNEL_ACCEL_SERVICE=cloudflared.service', text)
        self.assertIn('CF_TUNNEL_ACCEL_CONFIG=' + str(tunnel), text)
        self.assertIn('CF_TUNNEL_ACCEL_TUNNEL_ID=11111111-1111-1111-1111-111111111111', text)
        self.assertIn('CF_TUNNEL_ACCEL_TOKEN_FILE=' + str(token), text)
        self.assertNotIn('CF_TUNNEL_ACCEL_ACCOUNT_ID=', text)
        self.assertNotIn('CF_TUNNEL_ACCEL_AUX_ZONE_ID=', text)

    def test_init_without_config_explains_the_prerequisite_before_writing(self):
        cli = load_cli(self.env)
        with mock.patch('builtins.input') as prompt:
            result = cli.main([str(CLI), 'init', '--non-interactive', '--aux-zone', 'example.net', '--service', 'cloudflared.service'])
        prompt.assert_not_called()
        self.assertEqual(result, 1)
        self.assertFalse((self.home / '.config/cf-tunnel-accel/env').exists())
    def test_init_reports_the_exact_missing_token_path_without_a_secret(self):
        tunnel = self.home / '.cloudflared/config.yml'
        tunnel.parent.mkdir()
        tunnel.write_text('tunnel: 11111111-1111-1111-1111-111111111111\ningress:\n  - service: http_status:404\n')
        cli = load_cli(self.env)
        printed = []
        with mock.patch('builtins.input', side_effect=['example.net', '']), \
                mock.patch.object(cli, 'cprint', side_effect=lambda *a, **k: printed.append(' '.join(str(x) for x in a))):
            self.assertEqual(cli.main([str(CLI), 'init']), 0)
        text = '\n'.join(printed)
        self.assertIn('尚未找到 API Token:', text)
        self.assertIn(str(self.home / '.config/cf-tunnel-accel/token'), text)
        self.assertNotIn('fixture-token', text)


if __name__ == '__main__':
    unittest.main()
