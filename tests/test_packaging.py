import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb unavailable')
    def test_deb_contains_allowlisted_code_and_no_install_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / 'package.deb'
            subprocess.run([str(ROOT / 'scripts/build-deb.sh'), str(output)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            extracted = pathlib.Path(directory) / 'extracted'
            control = pathlib.Path(directory) / 'control'
            subprocess.run(['dpkg-deb', '-x', str(output), str(extracted)], check=True)
            subprocess.run(['dpkg-deb', '-e', str(output), str(control)], check=True)
            self.assertEqual({p.name for p in control.iterdir()}, {'control'})
            package = extracted / 'usr/lib/session-search/session_search'
            self.assertEqual({p.name for p in package.iterdir()},
                             {name + '.py' for name in
                              ('__init__', 'reader', 'store', 'embedding', 'indexer', 'server', 'tui', 'tui_backend', 'cpu', 'charts', 'search_history')})
            self.assertTrue((extracted / 'usr/bin/session-search').stat().st_mode & 0o111)
            unit = (extracted / 'usr/lib/systemd/user/codex-session-index.service').read_text()
            for setting in ('CPUQuota=\n', 'MemoryHigh=4G', 'MemoryMax=5G',
                            'MemorySwapMax=0'):
                self.assertIn(setting, unit)
            self.assertFalse((extracted / 'home').exists())

    def test_launcher_dispatches_without_automatic_dependency_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = pathlib.Path(directory) / 'python'
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            fake.chmod(0o755)
            import os
            env = dict(os.environ, SESSION_SEARCH_PYTHON=str(fake))
            for command, module in [('mcp', 'server'), ('index', 'indexer'), ('tui', 'tui')]:
                result = subprocess.run([str(ROOT / 'scripts/session-search'), command],
                                        env=env, check=True, capture_output=True, text=True)
                self.assertEqual(result.stdout.splitlines(), ['-m', 'session_search.' + module])
            result = subprocess.run([str(ROOT / 'scripts/session-search'), 'unknown'],
                                    env=env, capture_output=True)
            self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
