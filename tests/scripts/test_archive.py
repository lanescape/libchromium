import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.scripts.common import REPO  # noqa: F401
import archive


class Deterministic(unittest.TestCase):
    def test_same_files_same_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name in ('a', 'b'):
                root = tmp/name
                (root/'lib').mkdir(parents=True)
                (root/'lib/x.so').write_bytes(b'\x7fELF')
                (root/'lib/x.so').chmod(0o755)
                (root/'z.txt').write_text('z')
                (root/'link').symlink_to('z.txt')
            (tmp/'b/z.txt').touch()  # a different time
            archive.write(tmp/'a', tmp/'a.tar.zst')
            archive.write(tmp/'b', tmp/'b.tar.zst')
            self.assertEqual((tmp/'a.tar.zst').read_bytes(), (tmp/'b.tar.zst').read_bytes())
            listing = subprocess.check_output(['tar', '--zstd', '-tvf', str(tmp/'a.tar.zst')], text=True)
            self.assertIn('lib/x.so', listing)
            self.assertIn('link -> z.txt', listing)

    def test_reads_one_file_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp/'part/notices').mkdir(parents=True)
            (tmp/'part/notices/components.json').write_text('[]')
            (tmp/'part/other').write_text('x')
            archive.write(tmp/'part', tmp/'part.tar.zst')
            self.assertEqual(archive.read(tmp/'part.tar.zst', 'notices/components.json'), b'[]')
            self.assertIsNone(archive.read(tmp/'part.tar.zst', 'components.json'))
            self.assertIsNone(archive.read(tmp/'part.tar.zst', 'notices'))

    def test_a_tar_cut_short_between_members_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp/'part').mkdir()
            (tmp/'part/a').write_bytes(b'a'*700)
            (tmp/'part/b').write_bytes(b'\0'*3000)  # zeros, which mustn't pass for the end of the tar
            archive.write(tmp/'part', tmp/'whole.tar.zst')
            self.assertEqual(archive.scan(tmp/'whole.tar.zst')[0], [('a', 'file', 700, ''), ('b', 'file', 3000, '')])
            tar = subprocess.run(['zstd', '-dc', str(tmp/'whole.tar.zst')], check=True, stdout=subprocess.PIPE).stdout
            # Each member is a 512-byte header and its data in 512-byte blocks: after a's data; after b's; and with
            # the two zero blocks that end a tar, but none of the padding after them.
            for cut, whole in ((512+1024, False), (512+1024+512+3072, False), (512+1024+512+3072+1024, True)):
                with self.subTest(cut=cut):
                    subprocess.run(['zstd', '-q', '-f', '-o', str(tmp/'cut.tar.zst')], input=tar[:cut], check=True)
                    if whole:
                        archive.scan(tmp/'cut.tar.zst')
                    else:
                        with self.assertRaises(tarfile.ReadError):
                            archive.scan(tmp/'cut.tar.zst')
            (tmp/'cut.tar.zst').write_bytes((tmp/'whole.tar.zst').read_bytes()[:20])
            with self.assertRaises(RuntimeError):
                archive.scan(tmp/'cut.tar.zst')

    def test_members_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name in ('b/2', 'a/1', 'c'):
                (tmp/name).parent.mkdir(parents=True, exist_ok=True)
                (tmp/name).write_text('x')
            self.assertEqual(list(archive.members(tmp)), ['c', 'a', 'a/1', 'b', 'b/2'])


if __name__ == '__main__':
    unittest.main()
