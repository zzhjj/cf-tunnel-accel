"""Account, cache and profile isolation through the real extensionless CLI."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

try:  # pytest package imports and direct unittest discovery
    from .test_configuration import CLI, load_cli
except ImportError:
    from test_configuration import CLI, load_cli


ACCOUNT_A = 'a' * 32
ACCOUNT_B = 'b' * 32
AUX_ID = '1' * 32
OTHER_ID = '2' * 32
TUNNEL = '11111111-1111-1111-1111-111111111111'
AUX_NAME = 'aux.example.net'
CONFIG = f'tunnel: {TUNNEL}\ningress:\n  - service: http_status:404\n'


def zone(zone_id, name, account):
    return {'id': zone_id, 'name': name, 'account': {'id': account}, 'status': 'active'}


class ScopeContract(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.env = {'HOME': temp.name, 'PATH': '', 'LANG': 'C.UTF-8'}
        self.config = self.home / 'tunnel.yml'
        self.scoped_env = dict(self.env, CF_TUNNEL_ACCEL_ACCOUNT_ID=ACCOUNT_A,
                               CF_TUNNEL_ACCEL_API_TOKEN='scope-fixture-only')
        self.enter_patch(mock.patch.object(sys, 'path', [str(CLI.parent), *sys.path]))
        self.transport = self.enter_patch(mock.patch(
            'urllib.request.urlopen', side_effect=AssertionError('unexpected HTTP transport')))
        self.command = self.enter_patch(mock.patch(
            'subprocess.run', side_effect=AssertionError('unexpected subprocess')))
        self.enter_patch(mock.patch('socket.create_connection', side_effect=AssertionError('unexpected TCP')))
        self.enter_patch(mock.patch('socket.getaddrinfo', side_effect=AssertionError('unexpected DNS')))

    def enter_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def configured_env(self):
        return dict(self.scoped_env, CF_TUNNEL_ACCEL_TUNNEL_ID=TUNNEL,
                    CF_TUNNEL_ACCEL_CONFIG=str(self.config), CF_TUNNEL_ACCEL_AUX_ZONE=AUX_NAME,
                    CF_TUNNEL_ACCEL_AUX_ZONE_ID=AUX_ID,
                    CF_TUNNEL_ACCEL_SERVICE='cloudflared-scope-test.service')

    def test_list_zones_filters_foreign_and_unknown_accounts_on_every_page(self):
        cli = load_cli(self.scoped_env)
        own = zone(AUX_ID, AUX_NAME, ACCOUNT_A)
        foreign = zone(OTHER_ID, 'other.example.org', ACCOUNT_B)
        missing_owner = {'id': '3' * 32, 'name': 'unknown.example.com', 'account': None}
        # Page one contains only a foreign record but must not end pagination.
        with mock.patch.object(cli, 'api', side_effect=[
            {'success': True, 'result': [foreign], 'result_info': {'total_pages': 2}},
            {'success': True, 'result': [own, missing_owner], 'result_info': {'total_pages': 2}},
        ]) as api:
            self.assertEqual([row['id'] for row in cli.list_zones()], [AUX_ID])
        queries = []
        for call in api.call_args_list:
            method, path = call.args[:2]
            self.assertEqual(method, 'GET')
            request = urlsplit(path)
            self.assertEqual(request.path, '/zones')
            query = parse_qs(request.query)
            self.assertEqual(query.get('account.id'), [ACCOUNT_A])
            queries.append(query.get('page'))
        self.assertEqual(queries, [['1'], ['2']])

    def test_custom_origin_uses_its_own_proxied_tunnel_route_without_shared_claim(self):
        cli = load_cli(self.configured_env())
        custom = 'origin-this-tunnel.aux.example.net'
        with mock.patch.object(cli, 'CUSTOM_ORIGIN', custom), \
                mock.patch.object(cli, 'FALLBACK_ORIGIN', 'origin-shared.aux.example.net'), \
                mock.patch.object(cli, 'get_dns_record', side_effect=[None,
                    {'id':'custom','name':custom,'type':'CNAME','content':f'{TUNNEL}.cfargotunnel.com.','proxied':True}]) as get, \
                mock.patch.object(cli, 'upsert_dns') as write:
            cli.ensure_origin_dns()
        get.assert_any_call(AUX_ID, custom, 'CNAME')
        write.assert_called_once_with(AUX_ID, 'CNAME', custom, f'{TUNNEL}.cfargotunnel.com',
                                      proxied=True, comment=f'custom SaaS origin for {cli.TUNNEL_NAME} tunnel')
        self.assertNotEqual(write.call_args.args[2], 'origin-shared.aux.example.net')

    def test_custom_origin_hostname_activation_and_switch_use_per_profile_origin(self):
        self.config.write_text(CONFIG, encoding='utf-8')
        cli = load_cli(self.configured_env())
        custom = 'origin-alpha.aux.example.net'
        zone_data = zone(AUX_ID, AUX_NAME, ACCOUNT_A)
        from contextlib import ExitStack
        with ExitStack() as stack:
            patches = {
                'CUSTOM_ORIGIN': custom,
                'api': mock.DEFAULT,
            }
            for attr, value in patches.items():
                patcher = mock.patch.object(cli, attr, value)
                stack.enter_context(patcher)
            api_mock = mock.patch.object(cli, 'api', return_value={'success': True, 'result': zone_data})
            stack.enter_context(api_mock)
            for attr, value in (
                ('prepare_configuration', None),
                ('refresh_zone_cache', None),
                ('get_custom_hostname', {'id':'custom','hostname':'app.example.org','status':'active'}),
                ('resolve_zone', ('example.org', AUX_ID)),
                ('check_local_backend', None),
                ('ensure_origin_dns', None),
                ('ensure_preferred_entry_dns', None),
                ('ensure_fallback_origin', None),
                ('get_current_preferred_target', 'edge.example.net'),
                ('upsert_tunnel_hostname', False),
                ('_systemd_active', (True,'active')),
                ('create_or_get_custom_hostname', {'id':'custom'}),
                ('refresh_custom_hostname', {'id':'custom','hostname':'app.example.org'}),
                ('apply_custom_origin', {'id':'custom','hostname':'app.example.org'}),
                ('apply_saas_validation_records', None),
                ('upsert_dns', None),
                ('wait_dns_cname', None),
                ('wait_custom_hostname_active', {'id':'custom','status':'active','ssl':{'status':'active'}}),
                ('verify_https', (True,'HTTP 200')),
            ):
                kwargs = {'side_effect': [custom, cli.PREFERRED_ENTRY, cli.PREFERRED_ENTRY]} if attr == 'wait_dns_cname' else {}
                replacement = mock.Mock(return_value=value, **kwargs) if attr != 'api' else None
                stack.enter_context(mock.patch.object(cli, attr, replacement))
                if attr == 'upsert_dns':
                    write_dns = replacement
                if attr == 'apply_custom_origin':
                    apply = replacement
            stack.enter_context(mock.patch.object(cli.time, 'sleep'))
            result = cli.add_accelerated_service('app.example.org','http://127.0.0.1:8080')
            cli.switch_to_preferred('app.example.org')
        targets = [call.args[3] for call in write_dns.call_args_list if call.args[1:3] == ('CNAME','app.example.org')]
        self.assertEqual(targets, [custom, cli.PREFERRED_ENTRY, cli.PREFERRED_ENTRY])
        self.assertTrue(all(target != 'origin-shared.aux.example.net' for target in targets))
        apply.assert_called_once()
        self.assertTrue(result['ok'])


    def test_custom_origin_occupied_by_another_tunnel_is_not_overwritten(self):
        cli = load_cli(self.configured_env())
        custom = 'origin-this-tunnel.aux.example.net'
        foreign = {'id':'custom','name':custom,'type':'CNAME','content':'foreign.cfargotunnel.com','proxied':True}
        with mock.patch.object(cli, 'CUSTOM_ORIGIN', custom), \
                mock.patch.object(cli, 'FALLBACK_ORIGIN', 'origin-shared.aux.example.net'), \
                mock.patch.object(cli, 'get_dns_record', return_value=foreign), \
                mock.patch.object(cli, 'upsert_dns') as write:
            with self.assertRaises(cli.CFError):
                cli.ensure_origin_dns()
        write.assert_not_called()

    def test_aux_zone_mismatch_stops_before_dns_saas_or_local_changes(self):
        self.config.write_text(CONFIG, encoding='utf-8')
        cases = {
            'id': zone(OTHER_ID, AUX_NAME, ACCOUNT_A),
            'name': zone(AUX_ID, 'other.example.org', ACCOUNT_A),
            'account': zone(AUX_ID, AUX_NAME, ACCOUNT_B),
        }
        for mismatch, response in cases.items():
            for operation in ('add', 'cdn set'):
                with self.subTest(mismatch=mismatch, operation=operation):
                    cli = load_cli(self.configured_env())
                    # Only the authoritative lookup is expected. Do not fake prepare_configuration.
                    def api_fixture(method, path, *args, **kwargs):
                        self.assertEqual((method, path), ('GET', f'/zones/{AUX_ID}'))
                        return {'success': True, 'result': response}

                    with mock.patch.object(cli, 'api', side_effect=api_fixture) as api:
                        with self.assertRaises(cli.CFError):
                            if operation == 'add':
                                cli.add_accelerated_service('app.example.org', 'http://127.0.0.1:8000')
                            else:
                                cli.set_preferred_target('new-edge.example.org')
                    self.assertEqual([(call.args[0], call.args[1]) for call in api.call_args_list],
                                     [('GET', f'/zones/{AUX_ID}')])
                    self.command.assert_not_called()
                    self.transport.assert_not_called()
                    self.assertEqual(self.config.read_text(encoding='utf-8'), CONFIG)
                    self.assertEqual(list(self.home.iterdir()), [self.config])

    def test_incomplete_setup_stops_add_before_any_external_or_file_write(self):
        for scenario, env in (('empty home', self.env),
                              ('missing local config', self.configured_env())):
            with self.subTest(scenario=scenario):
                cli = load_cli(env)
                with mock.patch.object(cli, 'api', side_effect=AssertionError('setup should fail locally')) as api:
                    with self.assertRaises(cli.CFError):
                        cli.add_accelerated_service('app.example.org', 'http://127.0.0.1:8000')
                api.assert_not_called()
                self.command.assert_not_called()
                self.transport.assert_not_called()
                self.assertEqual(list(self.home.iterdir()), [])

    def test_real_cache_written_for_one_scope_is_not_loaded_by_another(self):
        shared = self.home / 'shared-cache.json'
        env = dict(self.scoped_env, CF_TUNNEL_ACCEL_ZONE_CACHE=str(shared))
        cli = load_cli(env)
        with mock.patch.object(cli, 'api', return_value={
            'success': True, 'result': [zone(AUX_ID, AUX_NAME, ACCOUNT_A)],
        }):
            cli.refresh_zone_cache()

        # Positive control: the same fixture is usable, not corrupt or stale.
        self.assertEqual(load_cli(env).zone_of_host(f'app.{AUX_NAME}'), AUX_NAME)
        for scope, change in (
            ('account', {'CF_TUNNEL_ACCEL_ACCOUNT_ID': ACCOUNT_B}),
            ('credential', {'CF_TUNNEL_ACCEL_API_TOKEN': 'another-scope-fixture-only'}),
            ('settings file', {'CF_TUNNEL_ACCEL_ENV_FILE': str(self.home / 'alternate.env')}),
        ):
            with self.subTest(scope=scope):
                foreign_cli = load_cli(dict(env, **change))
                self.assertIsNone(foreign_cli.zone_of_host(f'app.{AUX_NAME}'))
                self.assertIsNone(foreign_cli.zone_cache_age())
        self.transport.assert_not_called()

    def test_successful_empty_refresh_replaces_nonempty_cache_on_disk_and_reload(self):
        cli = load_cli(self.scoped_env)
        host = f'app.{AUX_NAME}'
        with mock.patch.object(cli, 'api', side_effect=[
            {'success': True, 'result': [zone(AUX_ID, AUX_NAME, ACCOUNT_A)]},
            {'success': True, 'result': []},
        ]) as api:
            cli.refresh_zone_cache()
            self.assertEqual(cli.zone_of_host(host), AUX_NAME)
            cli.refresh_zone_cache()
        self.assertEqual(api.call_count, 2)
        self.assertIsNone(cli.zone_of_host(host))
        self.assertEqual(json.loads(cli.ZONE_CACHE_PATH.read_text(encoding='utf-8'))['zones'], [])
        reloaded = load_cli(self.scoped_env)
        self.assertIsNone(reloaded.zone_of_host(host))
        self.assertIsNotNone(reloaded.zone_cache_age(), 'empty snapshot must load as a valid refresh')
        self.transport.assert_not_called()

    def test_real_profile_api_uses_explicit_alias_or_file_over_parent_canonical_token(self):
        from ui.profiles import load_profiles

        parent = 'parent-credential-fixture-only'
        alias = 'alias-credential-fixture-only'
        file_token = 'file-credential-fixture-only'
        token_file = self.home / 'selected-token'
        token_file.write_text(file_token + '\n', encoding='utf-8')
        token_file.chmod(0o600)
        profiles = []
        for ident, credential in (('alias', f'CF_API_TOKEN={alias}\n'),
                                  ('file', f'CF_TUNNEL_ACCEL_TOKEN_FILE={token_file}\n'),
                                  ('inherited', '')):
            env_file = self.home / f'{ident}.env'
            env_file.write_text(
                f'CF_TUNNEL_ACCEL_TUNNEL_ID={TUNNEL}\n'
                f'CF_TUNNEL_ACCEL_CONFIG={self.home / (ident + ".yml")}\n'
                f'CF_TUNNEL_ACCEL_SERVICE=cloudflared-{ident}.service\n' + credential,
                encoding='utf-8',
            )
            profiles.append({'id': ident, 'name': ident, 'env_file': str(env_file)})
        registry = self.home / 'profiles.json'
        registry.write_text(json.dumps({'profiles': profiles}), encoding='utf-8')

        with mock.patch.dict(os.environ, dict(self.env, CF_TUNNEL_ACCEL_API_TOKEN=parent), clear=True):
            loaded = load_profiles(CLI, registry)
            for module in loaded.values():
                self.addCleanup(sys.modules.pop, module.__name__, None)
            self.assertTrue(os.environ.get('CF_TUNNEL_ACCEL_API_TOKEN') == parent,
                            'the profile loader changed the parent credential')
            self.assertNotIn('CF_API_TOKEN', os.environ)
            for ident, expected in (('alias', alias), ('file', file_token), ('inherited', parent)):
                with self.subTest(profile=ident):
                    matched = []

                    def token_check(request, **kwargs):
                        # Only retain the boolean; failed tests must not print header contents.
                        matched.append(request.get_header('Authorization') == 'Bearer ' + expected)
                        return io.BytesIO(b'{"success": true}')

                    self.transport.side_effect = token_check
                    response = loaded[ident].api('GET', '/user/tokens/verify')
                    self.assertTrue(response.get('success'), 'fixture API did not respond successfully')
                    self.assertEqual(matched, [True], 'profile used the wrong credential source')


if __name__ == '__main__':
    unittest.main()
