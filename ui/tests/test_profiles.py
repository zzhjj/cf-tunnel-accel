"""Offline startup profile tests, including the real extensionless CLI."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("arc_profiles", ROOT / "ui/profiles.py")
profiles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profiles)
CLI = ROOT / "cf-tunnel-accel"


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Never read the operator's installed default env or token file.
        self.env = patch.dict(os.environ, {
            "PATH": os.environ.get("PATH", ""),
            "CF_TUNNEL_ACCEL_ENV_FILE": str(self.root / "default.env"),
            "CF_TUNNEL_ACCEL_TOKEN_FILE": str(self.root / "absent-token"),
            "CF_TUNNEL_ACCEL_TUNNEL_ID": "inherited-tunnel",
            "CF_TUNNEL_ACCEL_CONFIG": "/inherited/config.yml",
            "CF_TUNNEL_ACCEL_SERVICE": "inherited.service",
            "CF_TUNNEL_ACCEL_AUX_ZONE": "inherited.example",
            "UNCHANGED": "original",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.modules_before = set(sys.modules)
        self.addCleanup(self.clean_modules)

    def clean_modules(self):
        for name in set(sys.modules) - self.modules_before:
            if name.startswith("_cf_tunnel_profile_"):
                sys.modules.pop(name, None)

    def entry(self, ident, extra=""):
        env_file = self.root / (ident + ".env")
        env_file.write_text(
            f"CF_TUNNEL_ACCEL_TUNNEL_ID={ident}-tunnel\n"
            f"CF_TUNNEL_ACCEL_CONFIG={self.root / (ident + '.yml')}\n"
            f"CF_TUNNEL_ACCEL_SERVICE={ident}.service\n" + extra,
            encoding="utf-8",
        )
        return {"id": ident, "name": ident.title(), "env_file": str(env_file)}

    def registry(self, entries):
        path = self.root / "profiles.json"
        path.write_text(json.dumps({"profiles": entries}), encoding="utf-8")
        return path

    def load(self, entries, cli=CLI):
        before = dict(os.environ)
        try:
            return profiles.load_profiles(cli, self.registry(entries))
        finally:
            self.assertEqual(dict(os.environ), before)

    def test_real_cli_independent_globals_precedence_and_metadata(self):
        a = self.entry("a", "CF_TUNNEL_ACCEL_AUX_ZONE='a.example'\nSIDE_EFFECT=a\n")
        b = self.entry("b", 'CF_TUNNEL_ACCEL_AUX_ZONE="b.example"\nSIDE_EFFECT=b\n')
        loaded = self.load([a, b])
        self.assertEqual(list(loaded), ["a", "b"])
        first, second = loaded.values()
        self.assertIsNot(first, second)
        self.assertNotEqual(first.__name__, second.__name__)
        for ident, module in loaded.items():
            self.assertEqual(module.TUNNEL_ID, ident + "-tunnel")
            self.assertEqual(module.CONFIG_PATH, self.root / (ident + ".yml"))
            self.assertEqual(module.SERVICE_NAME, ident + ".service")
            self.assertEqual(module.AUX_ZONE_NAME, ident + ".example")
            self.assertEqual(module.ENV_FILE, self.root / (ident + ".env"))
        first.CDN_CANDIDATES.append("only-in-a.example")
        self.assertNotIn("only-in-a.example", second.CDN_CANDIDATES)
        self.assertEqual(profiles.profile_metadata(first), a)
        metadata = profiles.profile_metadata(first)
        metadata["name"] = "changed"
        self.assertEqual(profiles.profile_metadata(first), a)

    def test_runtime_helpers_use_the_profile_snapshot_not_live_environment(self):
        loaded = self.load([self.entry('a'), self.entry('b')])
        for ident, module in loaded.items():
            self.assertEqual(module.env_str('CF_TUNNEL_ACCEL_TUNNEL_ID', ''), ident + '-tunnel')
        with patch.dict(os.environ, {'CF_TUNNEL_ACCEL_TUNNEL_ID': 'live-change'}):
            self.assertEqual(loaded['a'].env_str('CF_TUNNEL_ACCEL_TUNNEL_ID', ''), 'a-tunnel')
            self.assertEqual(loaded['b'].env_str('CF_TUNNEL_ACCEL_TUNNEL_ID', ''), 'b-tunnel')

    def test_default_mode_keeps_cli_precedence_and_restores_file_side_effects(self):
        (self.root / "default.env").write_text(
            "CF_TUNNEL_ACCEL_TUNNEL_ID=file-tunnel\n"
            "CF_TUNNEL_ACCEL_PREFERRED_TARGET=file.example\n"
            "NON_CF_SIDE_EFFECT=added\n", encoding="utf-8")
        before = dict(os.environ)
        loaded = profiles.load_profiles(CLI, None)
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(list(loaded), ["default"])
        self.assertEqual(loaded["default"].TUNNEL_ID, "inherited-tunnel")
        self.assertEqual(loaded["default"].PREFERRED_TARGET, "file.example")
        self.assertEqual(profiles.profile_metadata(loaded["default"]),
                         {"id": "default", "name": "Default", "env_file": None})

    def test_restore_arbitrary_import_changes_and_between_profiles(self):
        stub = self.root / "cli"
        stub.write_text(
            "import os\n"
            "SEEN = dict(os.environ)\n"
            "os.environ.clear()\n"
            "os.environ['IMPORT_EFFECT'] = 'changed'\n", encoding="utf-8")
        entries = [self.entry("a", "NON_CF=a\nUNCHANGED=replaced\n"),
                   self.entry("b", "NON_CF=b\n")]
        loaded = self.load(entries, stub)
        self.assertEqual(loaded["a"].SEEN["NON_CF"], "a")
        self.assertEqual(loaded["b"].SEEN["NON_CF"], "b")
        self.assertEqual(loaded["a"].SEEN["UNCHANGED"], "original")
        self.assertNotIn("IMPORT_EFFECT", loaded["b"].SEEN)

    def test_failure_restores_environment_and_removes_partial_modules(self):
        stub = self.root / "cli"
        stub.write_text(
            "import os\n"
            "os.environ.pop('UNCHANGED', None)\n"
            "os.environ['IMPORT_EFFECT'] = 'changed'\n"
            "if os.environ['CF_TUNNEL_ACCEL_TUNNEL_ID'] == 'b-tunnel':\n"
            "    raise RuntimeError('import failed')\n", encoding="utf-8")
        before = set(sys.modules)
        with self.assertRaisesRegex(RuntimeError, "import failed"):
            self.load([self.entry("a"), self.entry("b")], stub)
        self.assertFalse(any(n.startswith("_cf_tunnel_profile_")
                             for n in set(sys.modules) - before))

    def test_standard_cloudflare_token_alias_overrides_inherited_credentials(self):
        with patch.dict(os.environ, {'CF_API_TOKEN': 'parent-fixture'}):
            loaded = self.load([self.entry('a', 'CLOUDFLARE_API_TOKEN=profile-fixture\n')])
        self.assertEqual(loaded['a'].CF_API_TOKEN, 'profile-fixture')


    def test_reject_invalid_registry_schema(self):
        valid = self.entry("a")
        cases = [None, [], {}, {"profiles": []}, {"profiles": {}},
                 {"profiles": [valid], "extra": True},
                 {"profiles": [{**valid, "extra": 1}]},
                 {"profiles": [{"id": "a", "env_file": valid["env_file"]}]},
                 {"profiles": [valid, valid]},
                 {"profiles": [{**valid, "id": "../a"}]},
                 {"profiles": [{**valid, "id": 1}]},
                 {"profiles": [{**valid, "name": " "}]},
                 {"profiles": [{**valid, "env_file": "relative.env"}]},
                 {"profiles": [None]}]
        path = self.root / "bad.json"
        for data in cases:
            with self.subTest(data=data):
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    profiles.load_profiles(CLI, path)

    def test_explicit_missing_or_malformed_files_never_fall_back(self):
        with self.assertRaises(FileNotFoundError):
            profiles.load_profiles(CLI, self.root / "missing.json")
        bad = self.root / "bad.json"
        bad.write_text("{", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            profiles.load_profiles(CLI, bad)
        entry = self.entry("a")
        Path(entry["env_file"]).unlink()
        with self.assertRaises(FileNotFoundError):
            self.load([entry])

    def test_required_settings_must_be_explicit_not_inherited_or_cli_defaults(self):
        for key in ("TUNNEL_ID", "CONFIG", "SERVICE"):
            for value in (None, "", "   "):
                with self.subTest(key=key, value=value):
                    entry = self.entry("a")
                    path = Path(entry["env_file"])
                    lines = [line for line in path.read_text().splitlines()
                             if not line.startswith("CF_TUNNEL_ACCEL_" + key + "=")]
                    if value is not None:
                        lines.append("CF_TUNNEL_ACCEL_" + key + "=" + value)
                    path.write_text("\n".join(lines), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "tunnel/config/service"):
                        self.load([entry])

    def test_duplicate_configs_and_services_including_aliases(self):
        for extra in (
            f"CF_TUNNEL_ACCEL_CONFIG={self.root}/a.yml",
            "CF_TUNNEL_ACCEL_SERVICE=a.service",
            "CF_TUNNEL_ACCEL_SERVICE=a",
        ):
            with self.subTest(extra=extra):
                a, b = self.entry("a"), self.entry("b")
                path = Path(b["env_file"])
                key = extra.partition("=")[0]
                lines = [line for line in path.read_text().splitlines()
                         if not line.startswith(key + "=")]
                path.write_text("\n".join(lines + [extra]), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "distinct"):
                    self.load([a, b])
        a, b = self.entry("a"), self.entry("b")
        (self.root / "b.yml").symlink_to(self.root / "a.yml")
        with self.assertRaisesRegex(ValueError, "distinct"):
            self.load([a, b])

    def test_env_parser_matches_cli_first_value_and_pins_selected_file(self):
        entry = self.entry('a', '# comment\n'
                           'CF_TUNNEL_ACCEL_TUNNEL_ID=second\n'
                           'CF_TUNNEL_ACCEL_ENV_FILE=/not/selected\n')
        module = self.load([entry])["a"]
        self.assertEqual(module.TUNNEL_ID, "a-tunnel")
        self.assertEqual(module.ENV_FILE, Path(entry["env_file"]))


if __name__ == "__main__":
    unittest.main()
