import tempfile
import unittest
from pathlib import Path
from session_search.cpu import core_groups, read_cores, save_cores

class CpuTests(unittest.TestCase):
    def test_smt_and_multiple_sockets_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for cpu, socket, core in [(0,0,0),(1,0,1),(2,0,0),(3,0,1),(4,1,0),(5,1,0)]:
                topology=root/f'cpu{cpu}'/'topology'
                topology.mkdir(parents=True)
                (topology/'physical_package_id').write_text(str(socket))
                (topology/'core_id').write_text(str(core))
            self.assertEqual(core_groups(root), [[0,2],[1,3],[4,5]])
            self.assertEqual(core_groups(root, {2,3,5}), [[2],[3],[5]])
            (root/'cpu5'/'online').write_text('0')
            self.assertEqual(core_groups(root)[2], [4])
            path=root/'config'/'cpu.json'
            self.assertIsNone(read_cores(path))
            save_cores(2,path)
            self.assertEqual(read_cores(path),2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
