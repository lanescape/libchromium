import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.scripts.common import REPO
import archive
import package
import publish

LINUX = {'target_os': 'linux', 'target_cpu': 'x64', 'sysroot': 'build/linux/debian_bullseye_amd64-sysroot'}
MAC = {'target_os': 'mac', 'target_cpu': 'arm64'}
TARGET = 'linux-amd64'


class Quick(unittest.TestCase):
    """CI's quick packages (specs/0003-ci-cd.md, CI) never reach a release."""

    def test_parts(self):
        self.assertEqual(package.parts_of(LINUX), ['lib', 'symbols', 'headers', 'toolchain', 'sysroot', 'notices'])
        self.assertEqual(package.parts_of(LINUX, quick=True), ['lib', 'headers', 'toolchain', 'sysroot', 'notices'])
        self.assertEqual(package.parts_of(MAC), ['lib', 'headers', 'toolchain', 'notices'])
        self.assertEqual(package.parts_of(MAC, quick=True), package.parts_of(MAC))

    def test_only_a_pull_request_s_version_is_quick(self):
        with tempfile.TemporaryDirectory() as tmp:
            for version in ('157.0.8103.39-1', '157.0.8103.39-12', '157.0.8103.39-pr', '157.0.8103.39-pr3-1'):
                with self.subTest(version=version), self.assertRaises(SystemExit):
                    package.package(TARGET, Path(tmp), Path(tmp), version, Path(tmp)/'out', quick=True)
            self.assertFalse((Path(tmp)/'out').exists())

    def test_a_release_is_at_the_release_level(self):
        self.assertEqual(archive.LEVEL, 19)
        self.assertLess(package.QUICK_LEVEL, archive.LEVEL)

    def test_publishing_refuses_a_quick_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            name = 'libchromium-1-linux-amd64-lib.tar.zst'
            (tmp/name).write_bytes(b'x')
            with self.assertRaises(SystemExit):
                publish.write_manifest(tmp, '1', 'c'*40, {'tag': '1.2.3.4', 'commit': '9'*40},
                                       {name: {'target': TARGET, 'part': 'lib', 'quick': True}}, 'owner/name')

    def test_publishing_needs_every_part_of_every_target(self):
        targets = json.loads((REPO/'targets.json').read_text())
        whole = {f'{target}-{part}': {'target': target, 'part': part}
                 for target, entry in targets.items() for part in package.parts_of(entry)}
        publish.check_parts(whole, targets)
        for name in whole:
            with self.subTest(missing=name), self.assertRaises(SystemExit):
                publish.check_parts({other: entry for other, entry in whole.items() if other != name}, targets)
        quick = {f'{target}-{part}': {'target': target, 'part': part, 'quick': True}
                 for target, entry in targets.items() for part in package.parts_of(entry, quick=True)}
        with self.assertRaises(SystemExit):
            publish.check_parts(quick, targets)

    def test_a_quick_package_strips_and_links_its_libraries_as_a_release_does(self):
        def commands(symbols):
            ran = []
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(package.subprocess, 'run', lambda command, check: ran.append(command)):
                tmp = Path(tmp)
                libraries = [tmp/'lib/libchromium.so', tmp/'lib/libffmpeg.so']
                package.strip_libraries(libraries, tmp/'toolchain', 'linux', tmp/symbols/'symbols')
                self.assertTrue((tmp/symbols/'symbols').is_dir())
                # The same commands but for where the debug information goes, whose name the link carries.
                return [[part.replace(str(tmp/symbols), '<symbols>').replace(str(tmp), '<tmp>') for part in command]
                        for command in ran]

        release = commands('stage/symbols')
        self.assertEqual(commands('stage/debug'), release)
        self.assertEqual(release[2], ['<tmp>/toolchain/bin/llvm-objcopy', '--add-gnu-debuglink=<symbols>/symbols/'
                                      'libchromium.so.debug', '<tmp>/lib/libchromium.so'])
        self.assertEqual(len(release), 6)


class Package(unittest.TestCase):
    """package() itself, quick and whole, with Chromium's tools and build replaced by a small tree."""

    def run_package(self, version, quick):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp/'checkout/src'
            out = src/'out'/TARGET
            (out/'libchromium').mkdir(parents=True)
            (out/'libchromium/resources.pak').write_bytes(b'pak')
            (out/'args.gn').write_text('is_debug = false\n')
            for name in ('libchromium.so', 'libffmpeg.so'):
                (out/name).write_bytes(name.encode())
            (src/LINUX['sysroot']/'usr').mkdir(parents=True)
            (src/'base').mkdir()
            (src/'base/a.h').write_text('// a\n')
            stripped, levels = [], {}

            def write(directory, asset, level=None):
                levels[Path(asset).name.split(f'{TARGET}-')[1].split('.')[0]] = level
                Path(asset).write_bytes(b'archive')

            with mock.patch('sdk.target_entry', return_value=LINUX), mock.patch('sdk.pin', return_value={}), \
                    mock.patch('sdk.summary'), \
                    mock.patch.object(package, 'target_variables', return_value={'solibs': ['./libchromium.so']}), \
                    mock.patch.object(package, 'compile_flags', return_value=([], [], [], [])), \
                    mock.patch.object(package, 'link_flags', return_value=([], [], [], [])), \
                    mock.patch.object(package, 'root_headers', return_value=['../../base/a.h']), \
                    mock.patch.object(package, 'linked_labels', return_value=set()), \
                    mock.patch.object(package, 'mojom_features', return_value=['is_posix', 'is_linux']), \
                    mock.patch.object(package, 'MOJOM_TOOLS', ()), \
                    mock.patch.object(package, 'MOJOM_LICENCES', ()), \
                    mock.patch.object(package, 'mojom_sources', return_value=[]), \
                    mock.patch.object(package, 'closure', return_value=({'../../base/a.h'}, [])), \
                    mock.patch.object(package, 'runtime_files', return_value=['libffmpeg.so']), \
                    mock.patch.object(package, 'strip_libraries',
                                      lambda libraries, toolchain, system, symbols: stripped.append(
                                          ([library.name for library in libraries], system, symbols.parent.name))), \
                    mock.patch.object(package, 'copy_toolchain', return_value={'version': '1', 'revision': ''}), \
                    mock.patch.object(package.licences, 'notices',
                                      lambda src, out, notices, version: notices.mkdir(parents=True)), \
                    mock.patch.object(package.archive, 'write', write):
                package.package(TARGET, tmp/'checkout', out, version, tmp/'assets', quick=quick)
            listed = json.loads((tmp/f'assets/assets-{TARGET}.json').read_text())
            return stripped, levels, listed

    def test_a_release_is_whole(self):
        stripped, levels, listed = self.run_package('157.0.8103.39-2', quick=False)
        self.assertEqual(stripped, [(['libchromium.so', 'libffmpeg.so'], 'linux', 'symbols')])
        self.assertEqual(levels, {part: 19 for part in package.parts_of(LINUX)})
        self.assertEqual([entry['part'] for entry in listed], package.parts_of(LINUX))
        self.assertFalse(any('quick' in entry for entry in listed))

    def test_a_quick_package_has_no_symbols_and_says_so(self):
        stripped, levels, listed = self.run_package('157.0.8103.39-pr1', quick=True)
        # Stripped and linked the same way, its debug information kept out of the parts.
        self.assertEqual(stripped, [(['libchromium.so', 'libffmpeg.so'], 'linux', 'debug')])
        self.assertEqual(levels, {part: package.QUICK_LEVEL for part in package.parts_of(LINUX, quick=True)})
        self.assertEqual([entry['part'] for entry in listed], package.parts_of(LINUX, quick=True))
        self.assertTrue(all(entry['quick'] is True for entry in listed))


class DebugSize(unittest.TestCase):
    """CI's only sign of how near a release's symbols part is to GitHub's 2 GiB limit."""

    def test_the_line_sums_every_debug_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            symbols = Path(tmp)/'symbols'
            (symbols/'nested').mkdir(parents=True)
            (symbols/'libchromium.so.debug').write_bytes(b'\0' * (3 << 20))
            (symbols/'nested'/'libffmpeg.so.debug').write_bytes(b'\0' * (1 << 20))
            self.assertEqual(package.debug_size_line('linux-amd64', symbols),
                             'linux-amd64: 4 MiB of debug information uncompressed, under the 2 GiB release asset limit')

    def test_a_size_at_the_limit_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            symbols = Path(tmp)/'symbols'
            symbols.mkdir()
            with open(symbols/'big.debug', 'wb') as f:
                f.truncate(2 << 30)  # sparse
            self.assertIn('at or over the 2 GiB', package.debug_size_line('linux-arm64', symbols))



if __name__ == '__main__':
    unittest.main()
