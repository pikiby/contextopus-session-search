import json
import tempfile
import unittest
from pathlib import Path
from session_search.settings import read_selection, save_selection


class SelectionTests(unittest.TestCase):
    def test_defaults_and_independent_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings/sessions.json'
            self.assertEqual(read_selection(path), {'active': True, 'archived': False})
            save_selection('archived', True, path)
            save_selection('active', False, path)
            self.assertEqual(read_selection(path), {'active': False, 'archived': True})
            save_selection('archived', False, path)
            self.assertEqual(read_selection(path), {'active': False, 'archived': False})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_invalid_selection_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sessions.json'
            save_selection('active', True, path)
            before = path.read_bytes()
            for state, value in [('all', True), ('active', 1), ('archived', 'false')]:
                with self.assertRaises(ValueError):
                    save_selection(state, value, path)
                self.assertEqual(path.read_bytes(), before)
            path.write_text(json.dumps({'active': 1, 'archived': False}))
            with self.assertRaises(ValueError):
                read_selection(path)
