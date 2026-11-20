"""Text preservation and ambiguity controls for public cloudflared configurations."""
import unittest
import cfa_ingress as ingress


class IngressContract(unittest.TestCase):
    CONFIG = '''tunnel: "11111111-1111-1111-1111-111111111111" # own tunnel
originRequest:
  connectTimeout: 10s
ingress:
- hostname: 'app.example.org' # keep this comment
  service: https://127.0.0.1:443

  originRequest:
    originServerName: backend.example.org
    httpHostHeader: backend.example.org
    noTLSVerify: true # explicit self-signed
- service: http_status:404
metrics: 127.0.0.1:59999
'''

    def test_indentless_and_quotes_parse(self):
        self.assertEqual(ingress.get_tunnel_id(self.CONFIG), '11111111-1111-1111-1111-111111111111')
        entry = ingress.parse_entries(self.CONFIG)[0]
        self.assertEqual(entry['hostname'], 'app.example.org')
        self.assertTrue(entry['no_tls_verify'])
        self.assertEqual(entry['origin_request']['originServerName'], 'backend.example.org')

    def test_tls_change_preserves_other_origin_parameters_and_comments(self):
        changed = ingress.upsert(self.CONFIG, 'app.example.org', 'https://127.0.0.1:443', False)
        self.assertIn('originServerName: backend.example.org', changed)
        self.assertIn('httpHostHeader: backend.example.org', changed)
        self.assertIn('noTLSVerify: false # explicit self-signed', changed)
        self.assertIn("hostname: 'app.example.org' # keep this comment", changed)
        self.assertIn('metrics: 127.0.0.1:59999', changed)
        self.assertEqual(ingress.upsert(changed, 'app.example.org', 'https://127.0.0.1:443', False), changed)

    def test_insert_before_catchall_and_keep_top_level_footer(self):
        changed = ingress.upsert(self.CONFIG, 'new.example.org', 'http://[::1]:8080/api')
        entries = ingress.parse_entries(changed)
        self.assertEqual(entries[1]['service'], 'http://[::1]:8080/api')
        self.assertLess(changed.index('new.example.org'), changed.index('- service: http_status:404'))
        self.assertTrue(changed.endswith('metrics: 127.0.0.1:59999\n'))

    def test_remove_does_not_leave_service_after_blank_lines(self):
        changed = ingress.remove(self.CONFIG, 'app.example.org')
        self.assertNotIn('https://127.0.0.1', changed)
        self.assertNotIn('httpHostHeader:', changed)
        self.assertEqual(ingress.parse_entries(changed), [])
        self.assertIn('metrics:', changed)

    def test_service_before_hostname_and_two_space_indent(self):
        text = 'ingress:\n  - service: http://127.0.0.1:80\n    hostname: app.example.org\n  - service: http_status:404\n'
        changed = ingress.upsert(text, 'app.example.org', 'http://127.0.0.1:81')
        self.assertEqual(ingress.parse_entries(changed)[0]['service'], 'http://127.0.0.1:81')
        self.assertEqual(ingress.parse_entries(ingress.remove(changed, 'app.example.org')), [])

    def test_global_tls_flag_is_overridden_per_rule(self):
        text = self.CONFIG.replace('connectTimeout: 10s', 'noTLSVerify: true').replace('    noTLSVerify: true # explicit self-signed\n', '')
        self.assertTrue(ingress.parse_entries(text)[0]['no_tls_verify'])
        changed = ingress.upsert(text, 'app.example.org', 'https://127.0.0.1:443', False)
        self.assertFalse(ingress.parse_entries(changed)[0]['no_tls_verify'])
        self.assertIn('  noTLSVerify: true', changed)

    def test_ambiguous_or_complex_documents_refuse_writes(self):
        docs = [
            'ingress: [{hostname: app.example.org, service: http://127.0.0.1:80}]\n',
            'ingress:\n  - hostname: &foo app.example.org\n    service: http://127.0.0.1:80\n  - service: http_status:404\n',
            'ingress:\n  - hostname: app.example.org\n    path: /api\n    service: http://127.0.0.1:80\n  - service: http_status:404\n',
            'ingress:\n  - service: http_status:404\n  - hostname: app.example.org\n    service: http://127.0.0.1:80\n',
            'ingress:\n  - hostname: app.example.org\n    service: |\n      http://localhost:80\n  - service: http_status:404\n',
        ]
        for text in docs:
            with self.subTest(text=text), self.assertRaises(ValueError):
                ingress.upsert(text, 'app.example.org', 'http://127.0.0.1:81')
            self.assertIsInstance(text, str)  # caller's original remains immutable


if __name__ == '__main__':
    unittest.main()
