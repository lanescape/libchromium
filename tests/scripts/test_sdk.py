import argparse
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests.scripts.common import REPO
import sdk


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL).stdout


class Checkout(unittest.TestCase):
    """A checkout of two repositories, src and src/v8, and this repository's patches for them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root/'repo'
        self.src = root/'checkout/src'
        for where in (self.src, self.src/'v8'):
            where.mkdir(parents=True)
            git(where, 'init', '-q')
            git(where, 'config', 'user.email', 't@example.com')
            git(where, 'config', 'user.name', 'T')
            git(where, 'config', 'commit.gpgsign', 'false')
        (self.src/'a.h').write_text('a\n')
        (self.src/'b.h').write_text('b\n')
        (self.src/'.gitignore').write_text('v8/\nlibchromium/\n')
        git(self.src, 'add', '.')
        git(self.src, 'commit', '-qm', 'pin')
        (self.src/'v8/c.h').write_text('c\n')
        git(self.src/'v8', 'add', '.')
        git(self.src/'v8', 'commit', '-qm', 'pin')
        (self.repo/'chromium/gn').mkdir(parents=True)
        (self.repo/'chromium/gn/BUILD.gn').write_text('# gn\n')
        self.saved = sdk.REPO, os.environ.get('LIBCHROMIUM_CHECKOUT')
        sdk.REPO = self.repo
        os.environ['LIBCHROMIUM_CHECKOUT'] = str(root/'checkout')

    def tearDown(self):
        sdk.REPO = self.saved[0]
        if self.saved[1] is None:
            os.environ.pop('LIBCHROMIUM_CHECKOUT', None)
        else:
            os.environ['LIBCHROMIUM_CHECKOUT'] = self.saved[1]
        self.tmp.cleanup()

    def write_patches(self, src_text, v8_text='C\n'):
        """Patches made the way export_patch.py makes them: edit, diff, restore."""
        patches = self.repo/'chromium/patches'
        shutil.rmtree(patches, ignore_errors=True)
        patches.mkdir(parents=True)
        for where, name, text, out in ((self.src, 'a.h', src_text, patches/'src.patch'),
                                       (self.src/'v8', 'c.h', v8_text, patches/'v8.patch')):
            original = (where/name).read_text()
            (where/name).write_text(text)
            out.write_text(git(where, 'diff'))
            (where/name).write_text(original)

    def patch(self):
        sdk.step_patch('linux-amd64', None)


class Patch(Checkout):
    """step_patch through the states a kept checkout can be in."""

    def test_applies_once(self):
        self.write_patches('A\n')
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'A\n')
        self.assertEqual((self.src/'v8/c.h').read_text(), 'C\n')
        before = (self.src/'a.h').stat().st_mtime_ns
        self.patch()
        self.assertEqual((self.src/'a.h').stat().st_mtime_ns, before)
        self.assertEqual((self.src/'libchromium/BUILD.gn').read_text(), '# gn\n')

    def test_resumes_between_repositories(self):
        self.write_patches('A\n')
        # A machine stopped after src's patch and its record, before v8's.
        git(self.src, 'apply', str(self.repo/'chromium/patches/src.patch'))
        sdk.record('', self.repo/'chromium/patches/src.patch')
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'A\n')
        self.assertEqual((self.src/'v8/c.h').read_text(), 'C\n')

    def test_a_patch_applied_without_a_record(self):
        self.write_patches('A\n')
        git(self.src, 'apply', str(self.repo/'chromium/patches/src.patch'))
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'A\n')

    def test_a_half_applied_patch(self):
        self.write_patches('A\n')
        (self.src/'a.h').write_text('garbled\n')
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'A\n')

    def test_a_changed_patch_replaces_the_old(self):
        self.write_patches('A\n')
        self.patch()
        self.write_patches('AA\n')
        v8_time = (self.src/'v8/c.h').stat().st_mtime_ns
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'AA\n')
        self.assertEqual((self.src/'v8/c.h').read_text(), 'C\n')
        # An unchanged file keeps its time, so nothing that includes it rebuilds.
        self.assertEqual((self.src/'v8/c.h').stat().st_mtime_ns, v8_time)

    def test_stopped_mid_apply_then_the_patch_drops_a_file(self):
        # A patch of a.h and b.h, recorded, then a machine stopped after changing a.h only.
        self.write_patches('A\n')
        patch = self.repo/'chromium/patches/src.patch'
        (self.src/'b.h').write_text('B\n')
        two = git(self.src, 'diff', '--', 'b.h')
        (self.src/'b.h').write_text('b\n')
        patch.write_text(patch.read_text()+two)
        sdk.record('', patch)
        (self.src/'a.h').write_text('A\n')
        # The next push's patch changes b.h alone.
        (self.src/'a.h').write_text('a\n')
        (self.src/'b.h').write_text('BB\n')
        patch.write_text(git(self.src, 'diff', '--', 'b.h'))
        (self.src/'b.h').write_text('b\n')
        (self.src/'a.h').write_text('A\n')
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'a\n')
        self.assertEqual((self.src/'b.h').read_text(), 'BB\n')

    def test_a_dropped_patch_is_reverted(self):
        self.write_patches('A\n')
        self.patch()
        (self.repo/'chromium/patches/v8.patch').unlink()
        self.patch()
        self.assertEqual((self.src/'v8/c.h').read_text(), 'c\n')
        sdk.unpatch(self.src)
        self.assertEqual((self.src/'a.h').read_text(), 'a\n')


class Reconcile(Checkout):
    """step_reconcile on a build directory restored apart from its checkout, as CI's caches are."""

    def setUp(self):
        super().setUp()
        shutil.copy(REPO/'targets.json', self.repo/'targets.json')
        self.checkout = self.src.parent
        self.synced({'pin': {'tag': '1.0.0.0', 'commit': 'a'*40}, 'gclient': 'g'})
        self.write_patches('A\n')
        self.patch()
        self.old = 1_000_000_000_000_000_000
        for path in (self.src/'a.h', self.src/'b.h', self.src/'v8/c.h', self.src/'libchromium/BUILD.gn'):
            os.utime(path, ns=(self.old, self.old))

    def synced(self, record):
        (self.checkout/'.libchromium-checkout.json').write_text(json.dumps(record)+'\n')

    def build(self, target='linux-amd64', record=None):
        """A build directory built from the checkout as it is now, or with the record given."""
        out = sdk.out_dir(target)
        out.mkdir(parents=True, exist_ok=True)
        (out/'obj.o').write_text('')
        (out/'build.ninja').write_text('')
        (out/sdk.BUILT_FROM).write_text(json.dumps(record if record is not None else sdk.built_from(target)))
        return out

    def reconcile(self, target='linux-amd64'):
        sdk.step_reconcile(target, None)

    def touched(self):
        return sorted(path.relative_to(self.src).as_posix()
                      for path in (self.src/'a.h', self.src/'b.h', self.src/'v8/c.h', self.src/'libchromium/BUILD.gn')
                      if path.stat().st_mtime_ns != self.old)

    def test_the_same_checkout_is_left_alone(self):
        out = self.build()
        self.reconcile()
        self.assertTrue((out/'obj.o').exists())
        self.assertEqual(self.touched(), [])

    def test_no_build_directory(self):
        self.reconcile()
        self.assertFalse(sdk.out_dir('linux-amd64').exists())

    def test_another_patch_rebuilds_the_files_either_patch_changes(self):
        # Built with src's patch changing b.h; the checkout's changes a.h. v8's patch is the same.
        self.write_patches('a\n')
        (self.src/'b.h').write_text('B\n')
        (self.repo/'chromium/patches/src.patch').write_text(git(self.src, 'diff', '--', 'b.h'))
        (self.src/'b.h').write_text('b\n')
        self.patch()
        out = self.build()
        self.assertEqual(sdk.checkout_state()['patches']['']['files'], ['b.h'])
        # src's new patch changes a.h alone.
        sdk.unpatch(self.src)
        (self.src/'a.h').write_text('A\n')
        (self.repo/'chromium/patches/src.patch').write_text(git(self.src, 'diff', '--', 'a.h'))
        (self.src/'a.h').write_text('a\n')
        self.patch()
        self.assertEqual(sdk.checkout_state()['patches']['']['files'], ['a.h'])
        self.assertEqual((self.src/'b.h').read_text(), 'b\n')
        for path in (self.src/'a.h', self.src/'b.h', self.src/'v8/c.h'):
            os.utime(path, ns=(self.old, self.old))
        self.reconcile()
        self.assertTrue((out/'obj.o').exists())
        self.assertEqual(self.touched(), ['a.h', 'b.h'])

    def test_another_gn_target_rebuilds_it(self):
        out = self.build()
        (self.src/'libchromium/BUILD.gn').write_text('# other\n')
        os.utime(self.src/'libchromium/BUILD.gn', ns=(self.old, self.old))
        self.reconcile()
        self.assertTrue((out/'obj.o').exists())
        self.assertEqual(self.touched(), ['libchromium/BUILD.gn'])

    def test_another_pin_builds_afresh(self):
        out = self.build()
        self.synced({'pin': {'tag': '2.0.0.0', 'commit': 'b'*40}, 'gclient': 'g'})
        self.reconcile()
        self.assertFalse(out.exists())
        self.assertEqual(self.touched(), [])

    def test_a_checkout_that_is_not_whole_builds_afresh(self):
        out = self.build()
        (self.checkout/'.libchromium-checkout.json').unlink()
        self.reconcile()
        self.assertFalse(out.exists())

    def test_a_build_directory_without_a_record_builds_afresh(self):
        out = self.build()
        (out/sdk.BUILT_FROM).unlink()
        self.reconcile()
        self.assertFalse(out.exists())

    def test_a_record_naming_files_outside_the_checkout_builds_afresh(self):
        record = sdk.built_from('linux-amd64')
        record['checkout']['patches']['']['files'] = ['../../outside']
        record['checkout']['patches']['']['sha256'] = 'other'
        outside = self.src.parent.parent/'outside'
        outside.write_text('')
        os.utime(outside, ns=(self.old, self.old))
        out = self.build(record=record)
        self.reconcile()
        self.assertFalse(out.exists())
        self.assertEqual(outside.stat().st_mtime_ns, self.old)

    def install_sdk(self, sha256=None):
        """An Apple SDK installed whole in the checkout, as its marker and Contents say."""
        (self.src/sdk.HERMETIC_XCODE/'Contents').mkdir(parents=True, exist_ok=True)
        sdk.apple_sdk_marker().write_text(sha256 or sdk.target_entry('macos-arm64')['mac_sdk']['sha256'])

    def test_macos_built_with_another_apple_sdk_builds_afresh(self):
        marker = sdk.apple_sdk_marker()
        pinned = sdk.target_entry('macos-arm64')['mac_sdk']['sha256']
        for installed, kept in (('0'*64, False), (pinned, True)):
            with self.subTest(installed=installed):
                self.install_sdk(installed)
                out = self.build('macos-arm64')
                # A restored checkout never holds the Apple SDK: mac-sdk installs the pinned one after.
                marker.unlink()
                self.reconcile('macos-arm64')
                self.assertEqual(out.exists(), kept)

    def configure(self):
        """Whether configure ran GN."""
        if not (self.repo/'chromium/args.gn').exists():
            (self.repo/'chromium/args.gn').write_text('is_debug = false\n')
        ran = []
        with unittest.mock.patch.object(sdk, 'run', lambda *command, **kwargs: ran.append(command)):
            sdk.step_configure('linux-amd64', None)
        self.assertIn(ran, ([], [('gn', 'gen', sdk.out_dir('linux-amd64'))]))
        return bool(ran)

    def test_configure_records_what_the_build_directory_is_built_from(self):
        self.assertTrue(self.configure())
        out = sdk.out_dir('linux-amd64')
        self.assertEqual(json.loads((out/sdk.BUILT_FROM).read_text()),
                         {**sdk.built_from('linux-amd64'), 'generation': 2})
        self.reconcile()
        self.assertEqual(self.touched(), [])
        (out/'build.ninja').write_text('')
        self.assertFalse(self.configure())

    def test_configure_generates_again_for_another_patch_of_a_toolchain_script(self):
        # GN reads such a script's time into the commands that run it, such as macOS's TOOL_VERSION, only when it
        # generates. Here the build directory and its checkout match when reconcile runs, and then patch changes the
        # patch, as when a run restores a checkout that isn't for its key.
        driver = self.src/'build/toolchain/apple/linker_driver.py'
        driver.parent.mkdir(parents=True)
        driver.write_text('toc = b""\n')
        git(self.src, 'add', '.')
        git(self.src, 'commit', '-qm', 'driver')
        out = sdk.out_dir('linux-amd64')

        def patched(c_h, driver_text):
            # reconcile finds the build directory built from the checkout as restored, and then patch changes it.
            self.reconcile()
            # src's patch, made from the whole repository, changes a.h and the driver.
            driver_now = driver.read_text()
            driver.write_text(driver_text)
            self.write_patches('A\n', c_h)
            driver.write_text(driver_now)
            self.patch()
            self.assertEqual(driver.read_text(), driver_text)
            return self.configure()
        patched('C\n', 'toc = []\n')
        (out/'build.ninja').write_text('')
        args = (out/'args.gn').stat().st_mtime_ns
        # A patch is compared whole: only another patch of a repository whose patch changes no toolchain script
        # leaves GN alone.
        for c_h, driver_text, generates in (('D\n', 'toc = []\n', False), ('D\n', 'toc = list()\n', True)):
            with self.subTest(v8=c_h, driver=driver_text):
                self.assertEqual(patched(c_h, driver_text), generates)
                self.assertEqual(json.loads((out/sdk.BUILT_FROM).read_text()),
                                 {**sdk.built_from('linux-amd64'), 'generation': 2})
                (out/'build.ninja').write_text('')
        self.assertEqual((out/'args.gn').stat().st_mtime_ns, args)

    def configure_macos(self):
        """What configure ran for macOS, with the pinned SDK's marker installed."""
        if not sdk.apple_sdk_marker().exists():
            self.install_sdk()
        ran = []
        with unittest.mock.patch.object(sdk, 'run', lambda *command, **kwargs: ran.append(command)), \
                unittest.mock.patch.object(sdk, 'args_gn', return_value='target_os = "mac"\n'):
            sdk.step_configure('macos-arm64', None)
        return ran

    def test_configure_refuses_a_build_directory_built_with_another_apple_sdk(self):
        out = sdk.out_dir('macos-arm64')
        self.assertEqual(self.configure_macos(), [('gn', 'gen', out)])
        (out/'build.ninja').write_text('')
        self.assertEqual(self.configure_macos(), [])
        record = json.loads((out/sdk.BUILT_FROM).read_text())
        for built_with in ('0'*64, None):
            with self.subTest(built_with=built_with):
                record['apple_sdk'] = built_with
                (out/sdk.BUILT_FROM).write_text(json.dumps(record))
                with self.assertRaisesRegex(SystemExit, 'another Apple SDK'):
                    self.configure_macos()

    def test_configure_refuses_macos_without_the_pinned_sdk(self):
        self.install_sdk('0'*64)
        for state in ('another', 'a marker alone', 'none'):
            with self.subTest(state=state):
                if state == 'a marker alone':
                    self.install_sdk()
                    shutil.rmtree(self.src/sdk.HERMETIC_XCODE)
                elif state == 'none':
                    sdk.apple_sdk_marker().unlink()
                with self.assertRaisesRegex(SystemExit, 'not installed'):
                    sdk.step_configure('macos-arm64', None)

    def test_configure_generates_again_for_a_record_an_earlier_configure_wrote(self):
        # It wrote the record before GN generated, so GN may never have generated for it.
        self.assertTrue(self.configure())
        out = sdk.out_dir('linux-amd64')
        (out/'build.ninja').write_text('')
        record = json.loads((out/sdk.BUILT_FROM).read_text())
        del record['generation']
        (out/sdk.BUILT_FROM).write_text(json.dumps(record))
        self.assertTrue(self.configure())
        self.assertFalse(self.configure())

    def test_configure_keeps_its_own_record_of_an_unknown_checkout(self):
        # A record naming no checkout still names the SDK, and generates again.
        out = sdk.out_dir('macos-arm64')
        (self.checkout/'.libchromium-checkout.json').unlink()
        self.assertEqual(self.configure_macos(), [('gn', 'gen', out)])
        (out/'build.ninja').write_text('')
        self.assertEqual(self.configure_macos(), [('gn', 'gen', out)])

    def test_configure_refuses_a_macos_build_directory_without_a_record(self):
        out = sdk.out_dir('macos-arm64')
        out.mkdir(parents=True)
        # One that GN was first generating, and stopped, generates again.
        (out/'.libchromium-generating').touch()
        (out/'build.ninja').write_text('')
        self.assertEqual(self.configure_macos(), [('gn', 'gen', out)])
        (out/'build.ninja').write_text('')
        self.assertEqual(self.configure_macos(), [])
        (out/sdk.BUILT_FROM).unlink()
        with self.assertRaisesRegex(SystemExit, 'another Apple SDK'):
            self.configure_macos()
        (out/'build.ninja').unlink()
        (out/'obj.o').write_text('')
        with self.assertRaisesRegex(SystemExit, 'another Apple SDK'):
            self.configure_macos()

    def test_configure_generates_again_without_a_record(self):
        self.assertTrue(self.configure())
        out = sdk.out_dir('linux-amd64')
        (out/'build.ninja').write_text('')
        (out/sdk.BUILT_FROM).unlink()
        self.assertTrue(self.configure())

    def test_a_failed_generation_keeps_the_earlier_record(self):
        self.assertTrue(self.configure())
        out = sdk.out_dir('linux-amd64')
        record = json.loads((out/sdk.BUILT_FROM).read_text())
        record['checkout']['patches']['']['files'] = ['build/toolchain/apple/linker_driver.py']
        record['checkout']['patches']['']['sha256'] = 'other'
        (out/sdk.BUILT_FROM).write_text(json.dumps(record))

        def fails(*command, **kwargs):
            raise SystemExit('gn failed')
        with unittest.mock.patch.object(sdk, 'run', fails), self.assertRaises(SystemExit):
            sdk.step_configure('linux-amd64', None)
        self.assertEqual(json.loads((out/sdk.BUILT_FROM).read_text()), record)

    def test_a_stopped_generation_for_other_arguments_happens_again(self):
        self.assertTrue(self.configure())
        out = sdk.out_dir('linux-amd64')
        (out/'build.ninja').write_text('')
        (self.repo/'chromium/args.gn').write_text('is_debug = true\n')

        # GN writes build.ninja before the rest of what it generates.
        def stops(*command, **kwargs):
            (out/'build.ninja').write_text('new')
            raise KeyboardInterrupt
        with unittest.mock.patch.object(sdk, 'run', stops), self.assertRaises(KeyboardInterrupt):
            sdk.step_configure('linux-amd64', None)
        ran = []
        with unittest.mock.patch.object(sdk, 'run', lambda *command, **kwargs: ran.append(command)):
            sdk.step_configure('linux-amd64', None)
        self.assertEqual(ran, [('gn', 'gen', out)])


class AppleSdk(unittest.TestCase):
    """step_mac_sdk, from an archive like the pinned one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.destination = self.root/'xcode'
        self.entry = unittest.mock.patch.object(sdk, 'target_entry')
        self.entry.start()
        self.archive()

    def tearDown(self):
        self.entry.stop()
        self.tmp.cleanup()

    def archive(self, directories=True, prefix='', padding=0):
        """An SDK with a header, and a link to its directory as the Apple SDK has it: the link the oldest member,
        the header the newest, and both older than anything made now."""
        made = self.root/'made'
        shutil.rmtree(made, ignore_errors=True)
        (made/'Developer/include/libxml').mkdir(parents=True)
        (made/'Developer/include/libxml/tree.h').write_text('tree\n')
        (made/'Developer/include/libxml2').mkdir()
        (made/'Developer/include/libxml2/libxml').symlink_to('../libxml')
        tar_path = self.root/'sdk.tar'
        with tarfile.open(tar_path, 'w', format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(made.rglob('*')):
                if path.is_dir() and not path.is_symlink() and not directories:
                    continue
                member = tar.gettarinfo(path, path.relative_to(made).as_posix())
                if member.issym():
                    member.name = prefix+member.name
                member.mtime = 1_500_000_000 if member.issym() else 1_700_000_000 if member.isfile() else 1_600_000_000
                if member.isfile():
                    with open(path, 'rb') as data:
                        tar.addfile(member, data)
                else:
                    tar.addfile(member)
        with open(tar_path, 'ab') as tar:
            tar.write(bytes(padding))
        self.path = tar_path.with_suffix('.tar.zst')
        self.path.unlink(missing_ok=True)
        subprocess.run(['zstd', '-q', '--rm', str(tar_path)], check=True)
        sdk.target_entry.return_value = {'mac_sdk': {'sha256': sdk.sha256(self.path), 'sdk': '26.2', 'xcode': '26.2'}}

    def install(self):
        sdk.step_mac_sdk('macos-arm64', argparse.Namespace(archive=str(self.path), destination=str(self.destination)))
        return {path.relative_to(self.destination).as_posix(): path.lstat().st_mtime_ns
                for path in [self.destination, *self.destination.rglob('*')]}

    def marker(self):
        return self.root/'.xcode.libchromium-apple-sdk'

    def again(self):
        self.marker().unlink()
        return self.install()

    def test_every_install_has_the_archives_times(self):
        first = self.install()
        self.assertTrue((self.destination/'Contents/Developer/include/libxml2/libxml/tree.h').is_file())
        self.assertEqual(first['Contents/Developer/include/libxml2/libxml'], 1_500_000_000*10**9)
        self.assertEqual(first['Contents/Developer/include/libxml'], 1_600_000_000*10**9)
        self.assertEqual(max(first.values()), 1_700_000_000*10**9)
        for made in ('.', 'Contents'):
            self.assertEqual(first[made], 1_700_000_000*10**9)
        self.assertEqual(self.marker().read_text(), sdk.target_entry('macos-arm64')['mac_sdk']['sha256'])
        self.assertEqual(oct(self.destination.stat().st_mode & 0o777), oct(0o777 & ~self.umask()))
        self.assertEqual(self.again(), first)

    def test_padding_after_the_tars_end_is_read(self):
        # More than a pipe holds, which zstd must write before it exits.
        self.archive(padding=4 << 20)
        self.assertEqual(self.install()['Contents/Developer/include/libxml2/libxml'], 1_500_000_000*10**9)

    def test_directories_the_archive_does_not_list_have_its_newest_time(self):
        self.archive(directories=False)
        first = self.install()
        self.assertEqual(first['Contents/Developer/include/libxml'], 1_700_000_000*10**9)
        self.assertEqual(first['Contents/Developer/include/libxml2/libxml'], 1_500_000_000*10**9)
        self.assertEqual(self.again(), first)

    def test_a_link_is_timed_by_the_name_it_is_extracted_as(self):
        # tarfile's data filter strips a leading slash.
        self.archive(prefix='/')
        self.assertEqual(self.install()['Contents/Developer/include/libxml2/libxml'], 1_500_000_000*10**9)

    def test_a_listed_directory_named_through_a_link_keeps_its_time(self):
        made = self.root/'made'
        shutil.rmtree(made, ignore_errors=True)
        (made/'Real/Sub').mkdir(parents=True)
        (made/'Real/Sub/a.h').write_text('a\n')
        tar_path = self.root/'sdk.tar'
        with tarfile.open(tar_path, 'w', format=tarfile.PAX_FORMAT) as tar:
            for name, kind, mtime in (('Real', tarfile.DIRTYPE, 1_600_000_000),
                                      ('Alias', tarfile.SYMTYPE, 1_500_000_000),
                                      ('Alias/Sub', tarfile.DIRTYPE, 1_550_000_000),
                                      ('Real/Sub/a.h', tarfile.REGTYPE, 1_700_000_000)):
                member = tarfile.TarInfo(name)
                member.type, member.mtime, member.mode = kind, mtime, 0o755
                if kind == tarfile.SYMTYPE:
                    member.linkname = 'Real'
                if kind == tarfile.REGTYPE:
                    member.size = 2
                    with open(made/name, 'rb') as data:
                        tar.addfile(member, data)
                else:
                    tar.addfile(member)
        self.path = tar_path.with_suffix('.tar.zst')
        self.path.unlink(missing_ok=True)
        subprocess.run(['zstd', '-q', '--rm', str(tar_path)], check=True)
        sdk.target_entry.return_value['mac_sdk']['sha256'] = sdk.sha256(self.path)
        times = self.install()
        self.assertEqual(times['Contents/Real/Sub'], 1_550_000_000*10**9)
        self.assertEqual(times['Contents/Alias'], 1_500_000_000*10**9)

    def test_a_listed_root_keeps_its_time(self):
        made = self.root/'made'
        shutil.rmtree(made, ignore_errors=True)
        made.mkdir()
        (made/'a.h').write_text('a\n')
        tar_path = self.root/'sdk.tar'
        with tarfile.open(tar_path, 'w', format=tarfile.PAX_FORMAT) as tar:
            for name in ('.', 'a.h'):
                member = tar.gettarinfo(made/name, name)
                member.mtime = 1_600_000_000 if name == '.' else 1_700_000_000
                with open(made/name, 'rb') if member.isfile() else open(os.devnull, 'rb') as data:
                    tar.addfile(member, data if member.isfile() else None)
        self.path = tar_path.with_suffix('.tar.zst')
        self.path.unlink(missing_ok=True)
        subprocess.run(['zstd', '-q', '--rm', str(tar_path)], check=True)
        sdk.target_entry.return_value['mac_sdk']['sha256'] = sdk.sha256(self.path)
        times = self.install()
        self.assertEqual(times['Contents'], 1_600_000_000*10**9)
        self.assertEqual(times['.'], 1_700_000_000*10**9)

    def test_a_stopped_replacement_leaves_no_marker_on_what_is_left(self):
        self.install()
        rmtree = shutil.rmtree

        # Stopped part-way through removing the installed SDK.
        def stops(path, *args, **kwargs):
            if Path(path) == self.destination:
                rmtree(Path(path)/'Contents')
                raise KeyboardInterrupt
            return rmtree(path, *args, **kwargs)
        self.marker().write_text('another')
        with unittest.mock.patch.object(sdk.shutil, 'rmtree', stops), self.assertRaises(KeyboardInterrupt):
            self.install()
        self.assertFalse(self.marker().exists())
        self.install()
        self.assertTrue((self.destination/'Contents/Developer/include/libxml2/libxml/tree.h').is_file())

    def test_a_stopped_install_is_never_taken_for_a_whole_one(self):
        utime = os.utime

        # Stopped as the last time is set, before the marker is written.
        def stops(path, *args, **kwargs):
            if Path(path) == self.destination:
                raise KeyboardInterrupt
            return utime(path, *args, **kwargs)
        with unittest.mock.patch.object(sdk.os, 'utime', stops), self.assertRaises(KeyboardInterrupt):
            self.install()
        self.assertFalse(self.marker().exists())
        self.assertEqual(self.install()['.'], 1_700_000_000*10**9)

    def test_an_sdk_that_is_gone_is_installed_again(self):
        self.install()
        shutil.rmtree(self.destination)
        self.install()
        self.assertTrue((self.destination/'Contents/Developer/include/libxml2/libxml/tree.h').is_file())

    @staticmethod
    def umask():
        mask = os.umask(0)
        os.umask(mask)
        return mask


class Environment(unittest.TestCase):
    """What Chromium's tools run with."""

    def test_public_storage_is_read_without_signing_in(self):
        environment = sdk.env()
        self.assertEqual(environment['BOTO_CONFIG'], os.devnull)
        self.assertEqual(environment['SWARMING_HEADLESS'], '1')

    def test_the_callers_target_never_reaches_chromiums_tools(self):
        # build/rust/gni_impl/run_bindgen.py exits if it sees TARGET.
        saved = os.environ.get('TARGET')
        os.environ['TARGET'] = 'linux-amd64'
        try:
            self.assertNotIn('TARGET', sdk.env())
        finally:
            if saved is None:
                os.environ.pop('TARGET', None)
            else:
                os.environ['TARGET'] = saved


class Explain(unittest.TestCase):
    """Why Siso ran what it ran: counted in the job's summary and kept whole for the run's artifact, whether the build
    passed or failed, and only ever this build's record."""

    RECORD = (
        'output obj/base/a.o older than most recent input ../../third_party/llvm-build/Release+Asserts/bin/clang++: '
        'out:2026-11-17T22:20:00Z in:+1h\n'
        'output obj/base/b.o older than most recent input ../../third_party/llvm-build/Release+Asserts/bin/clang++: '
        'out:2026-11-17T22:20:00Z in:+1h\n'
        'output obj/v8/c.o older than most recent input ../../build/linux/debian_bullseye_arm64-sysroot/usr/include/'
        'stdio.h: out:2026-11-17T22:20:00Z in:+1h\n'
        'deps for gen/d.h dirty: input out/linux-arm64/gen/d.mojom-module: dirty\n'
        'output obj/e.o older than most recent input ../../weird dir/with: colon/e.h: out:2026-11-17T22:20:00Z '
        'in:+1h\n'
        'something Siso says another way about ../../secret/f.cc\n'
        '\n'
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out = root/'checkout/src/out/linux-arm64'
        self.out.mkdir(parents=True)
        self.temp = root/'runner-temp'
        self.temp.mkdir()
        self.step_summary = root/'summary.md'
        self.saved = {name: os.environ.get(name) for name in ('LIBCHROMIUM_CHECKOUT', 'RUNNER_TEMP',
                                                             'GITHUB_STEP_SUMMARY')}
        os.environ.update(LIBCHROMIUM_CHECKOUT=str(root/'checkout'), RUNNER_TEMP=str(self.temp),
                          GITHUB_STEP_SUMMARY=str(self.step_summary))
        self.run = sdk.run

    def tearDown(self):
        sdk.run = self.run
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def build(self, record=None, fails=False):
        self.error = subprocess.CalledProcessError(1, ['autoninja'])
        def autoninja(*command, **kwargs):
            self.assertEqual(command[0], 'autoninja')
            self.assertNotIn('-explain_log=', command)
            self.assertFalse((self.out/'siso_explain').exists(), 'the build started with the last build\'s record')
            if record is not None:
                (self.out/'siso_explain').write_text(record)
            if fails:
                raise self.error
            return 0
        sdk.run = autoninja
        args = sdk.argparse.Namespace(jobs=None)
        if fails:
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                sdk.step_build('linux-arm64', args)
            self.assertIs(raised.exception, self.error)
        else:
            sdk.step_build('linux-arm64', args)
        return self.step_summary.read_text() if self.step_summary.exists() else ''

    def kept(self):
        return self.temp/'siso-explain-linux-arm64/siso_explain-linux-arm64.txt'

    def test_the_reasons_are_counted_by_where_the_input_is(self):
        written = self.build(self.RECORD)
        self.assertEqual(written.splitlines(), [
            '- Siso explained 6 entries for linux-arm64:',
            '- 2: an input is newer, under third_party/llvm-build/Release+Asserts/bin',
            '- 1: a dependency ran, under out/linux-arm64/gen',
            '- 1: an input is newer, under build/linux/debian_bullseye_arm64-sysroot/usr',
            '- 1: an input is newer, under weird dir/with: colon',
            '- 1: another reason (the record has it)',
        ])
        self.assertEqual(self.kept().read_text(), self.RECORD)

    def test_a_failed_build_is_explained_and_still_fails(self):
        written = self.build(self.RECORD, fails=True)
        self.assertIn('- Siso explained 6 entries for linux-arm64:', written)
        self.assertTrue(self.kept().is_file())

    def test_the_cached_record_is_never_reported(self):
        (self.out/'siso_explain').write_text(self.RECORD)
        written = self.build()
        self.assertEqual(written, '- Siso explained nothing for linux-arm64\n')
        self.assertFalse(self.kept().exists())

    def test_only_so_many_reasons_are_counted(self):
        record = ''.join(f'deps for x dirty: input d{n}/x.h: dirty\n' for n in (1, 2, 3, 1, 3))
        counted, sdk.REASONS_COUNTED = sdk.REASONS_COUNTED, 2
        try:
            written = self.build(record)
        finally:
            sdk.REASONS_COUNTED = counted
        self.assertEqual(written.splitlines(), [
            '- Siso explained 5 entries for linux-arm64:', '- 2: a dependency ran, under d1',
            '- 1: a dependency ran, under d2', '- 2: reasons past the first 2 kinds, not counted (the record has them)'])

    def test_only_fixed_words_and_directories_reach_the_summary(self):
        self.assertNotIn('secret', self.build(self.RECORD))

    def test_a_report_that_fails_never_fails_the_build(self):
        def unreadable(target):
            raise PermissionError(13, 'Permission denied')
        explain, sdk.explain = sdk.explain, unreadable
        try:
            self.build(self.RECORD)
        finally:
            sdk.explain = explain

    def test_a_report_that_fails_never_replaces_the_builds_own_error(self):
        def unwritable(*args, **kwargs):
            raise UnicodeEncodeError('ascii', 'é', 0, 1, 'ordinal not in range(128)')
        summary, sdk.summary = sdk.summary, unwritable
        try:
            with unittest.mock.patch('builtins.print', unwritable):
                self.build(self.RECORD, fails=True)
        finally:
            sdk.summary = summary

    def test_the_record_is_kept_even_if_the_summary_cannot_be_written(self):
        def unwritable(line):
            raise OSError(28, 'No space left on device')
        with unittest.mock.patch.object(sdk, 'summary', unwritable):
            self.build(self.RECORD)
        self.assertEqual(self.kept().read_text(), self.RECORD)

    def test_an_earlier_builds_kept_record_never_stands_for_this_one(self):
        self.kept().parent.mkdir(parents=True)
        self.kept().write_text('an earlier build\'s')
        self.build()
        self.assertFalse(self.kept().exists())

    def test_a_copy_cut_short_is_never_kept(self):
        def short(source, destination):
            Path(destination).write_text('part of the rec')
            raise OSError(28, 'No space left on device')
        with unittest.mock.patch('shutil.copyfile', short):
            self.build(self.RECORD)
        self.assertEqual(list((self.temp/'siso-explain-linux-arm64').iterdir()), [])

    def test_a_last_record_that_cannot_be_removed_still_builds_unexplained(self):
        (self.out/'siso_explain').mkdir()
        built = []
        def autoninja(*command, **kwargs):
            built.append(command)
            return 0
        sdk.run = autoninja
        sdk.step_build('linux-arm64', sdk.argparse.Namespace(jobs=None))
        # With no record at all, which Siso could fail to start for, rotating the old one.
        self.assertEqual([command[:2] for command in built], [('autoninja', '-explain_log=')])
        self.assertEqual(self.step_summary.read_text(),
                         "- Siso's last record for linux-arm64 could not be removed, so this build is not explained\n")
        self.assertFalse(self.kept().exists())


class DepotTools(unittest.TestCase):
    """depot_tools is cloned when missing, and its bootstrap always finished."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tools = Path(self.tmp.name)/'depot_tools'
        self.ran = []
        self.saved = os.environ.get('DEPOT_TOOLS'), sdk.run
        os.environ['DEPOT_TOOLS'] = str(self.tools)

        self.broken = None
        self.where = []

        def run(*command, cwd=None, check=True, **kwargs):
            self.ran.append([str(part) for part in command])
            self.where.append(cwd)
            if command[:2] == ('git', 'clone'):
                Path(command[-1]).mkdir(parents=True)
                (Path(command[-1])/'gclient').write_text('')
            if str(command[0]) == self.broken:
                if check:
                    raise subprocess.CalledProcessError(1, command)
                return 1
            return 0
        sdk.run = run

    def tearDown(self):
        tools, sdk.run = self.saved
        if tools is None:
            os.environ.pop('DEPOT_TOOLS', None)
        else:
            os.environ['DEPOT_TOOLS'] = tools
        self.tmp.cleanup()

    def test_a_fresh_clone_is_bootstrapped(self):
        sdk.ensure_depot_tools()
        self.assertEqual([command[0] for command in self.ran],
                         ['git', str(self.tools/'ensure_bootstrap'), str(self.tools/'python-bin/python3'),
                          str(self.tools/'vpython3')])

    def test_a_kept_clone_is_bootstrapped_again(self):
        self.tools.mkdir()
        (self.tools/'gclient').write_text('')
        (self.tools/'python3_bin_reldir.txt').write_text('bin\n')
        sdk.ensure_depot_tools()
        self.assertEqual(self.ran, [[str(self.tools/'ensure_bootstrap')],
                                    [str(self.tools/'python-bin/python3'), '-c', 'pass'],
                                    [str(self.tools/'vpython3'), '-c', 'pass']])
        # vpython3 takes depot_tools' own environment, not one around wherever the step runs.
        self.assertEqual(self.where[-1], self.tools)

    def test_a_bootstrap_that_left_no_python_fails(self):
        # ensure_bootstrap exits 0 even when it fetched nothing.
        for tool in ('python-bin/python3', 'vpython3'):
            with self.subTest(tool=tool):
                self.ran, self.broken = [], str(self.tools/tool)
                with self.assertRaises(subprocess.CalledProcessError):
                    sdk.ensure_depot_tools()

    def test_the_checkout_bootstraps_before_anything_else(self):
        calls = []
        saved = sdk.ensure_depot_tools, sdk.wanted_checkout, sdk.checkout_root
        sdk.ensure_depot_tools = lambda: calls.append('bootstrap')

        def wanted():
            calls.append('wanted')
            raise SystemExit('stop')
        sdk.wanted_checkout = wanted
        sdk.checkout_root = lambda: Path(self.tmp.name)/'checkout'
        try:
            with self.assertRaises(SystemExit):
                sdk.step_checkout('linux-amd64', None)
        finally:
            sdk.ensure_depot_tools, sdk.wanted_checkout, sdk.checkout_root = saved
        self.assertEqual(calls, ['bootstrap', 'wanted'])


class Sync(unittest.TestCase):
    """A move to another pin deletes what the new pin dropped, keeping unchanged files' times unless gclient can't
    move it in place, even after a stop; a sync at the same pin, with no move unfinished, isn't a move."""

    OLD, NEW = 'a'*40, 'b'*40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)/'checkout'
        (self.root/'src/.git').mkdir(parents=True)
        self.head = self.OLD
        self.ran = []
        # gclient commands that fail, each as many times as it's listed.
        self.failing = []
        self.saved = sdk.run, sdk.ensure_depot_tools, sdk.wanted_checkout, sdk.checkout_root, sdk.unpatch, \
            os.environ.get('GITHUB_STEP_SUMMARY')
        self.wanted = {'pin': {'tag': '1.2.3.4', 'commit': self.NEW}, 'gclient': 'g'}
        self.summary = Path(self.tmp.name)/'summary'
        os.environ['GITHUB_STEP_SUMMARY'] = str(self.summary)

        def run(*command, cwd=None, check=True, capture=False, quiet=False):
            command = [str(part) for part in command]
            self.ran.append(command)
            if command[:1] == ['gclient'] and command[1] in self.failing:
                self.failing.remove(command[1])
                if check:
                    raise subprocess.CalledProcessError(1, command)
                return 1
            if command[-2:] == ['rev-parse', 'HEAD']:
                return self.head+'\n'
            if command[-2:] == ['rev-parse', 'FETCH_HEAD']:
                return self.NEW+'\n'
            if 'checkout' in command and '--detach' in command:
                self.head = command[-1]
            return '' if capture else 0
        sdk.run = run
        sdk.ensure_depot_tools = lambda: None
        sdk.wanted_checkout = lambda: self.wanted
        sdk.checkout_root = lambda: self.root
        sdk.unpatch = lambda src: self.ran.append(['unpatch'])

    def tearDown(self):
        sdk.run, sdk.ensure_depot_tools, sdk.wanted_checkout, sdk.checkout_root, sdk.unpatch, summary = self.saved
        if summary is None:
            os.environ.pop('GITHUB_STEP_SUMMARY', None)
        else:
            os.environ['GITHUB_STEP_SUMMARY'] = summary
        self.tmp.cleanup()

    def syncs(self):
        return [command[2:] for command in self.ran if command[:2] == ['gclient', 'sync']]

    def checkout(self):
        sdk.step_checkout('linux-amd64', None)

    IN_PLACE = ['--nohooks', '--no-history', '--reset', '-D', '-r', 'src@'+NEW]
    FORCED = ['--nohooks', '--no-history', '--force', '--reset', '-D', '-r', 'src@'+NEW]

    def test_a_move_deletes_what_the_new_pin_dropped_in_place(self):
        self.checkout()
        # Not --force, which with -D would clone again every dependency the move changes.
        self.assertEqual(self.syncs(), [self.IN_PLACE])
        self.assertFalse(sdk.moving_record().exists())
        self.assertEqual(json.loads(sdk.checkout_record().read_text()), self.wanted)
        self.assertFalse(self.summary.exists())

    def test_a_move_gclient_cant_make_in_place_is_made_again_with_force(self):
        self.failing = ['sync']
        self.checkout()
        self.assertEqual(self.syncs(), [self.IN_PLACE, self.FORCED])
        self.assertIn('`--force`', self.summary.read_text())
        self.assertFalse(sdk.moving_record().exists())
        self.assertEqual(json.loads(sdk.checkout_record().read_text()), self.wanted)

    def test_a_summary_that_cant_be_written_doesnt_stop_the_retry(self):
        self.summary.mkdir()
        self.failing = ['sync']
        self.checkout()
        self.assertEqual(self.syncs(), [self.IN_PLACE, self.FORCED])
        self.assertEqual(json.loads(sdk.checkout_record().read_text()), self.wanted)

    def test_a_move_is_marked_before_src_leaves_its_pin(self):
        def unpatch(src):
            self.assertTrue(sdk.moving_record().exists())
            raise SystemExit('stopped')
        sdk.unpatch = unpatch
        with self.assertRaises(SystemExit):
            self.checkout()
        self.assertEqual(self.head, self.OLD)

    def test_a_move_stopped_after_src_reached_the_pin_still_syncs_as_a_move(self):
        for failing in (['sync', 'sync'], ['runhooks']):
            with self.subTest(failing=failing):
                self.head, self.ran, self.failing = self.OLD, [], list(failing)
                sdk.moving_record().unlink(missing_ok=True)
                with self.assertRaises(subprocess.CalledProcessError):
                    self.checkout()
                self.assertEqual(self.head, self.NEW)
                self.assertTrue(sdk.moving_record().exists())
                self.assertFalse(sdk.checkout_record().exists())
                self.ran = []
                self.checkout()
                self.assertNotIn(['unpatch'], self.ran)
                self.assertEqual(self.syncs(), [self.IN_PLACE])
                self.assertFalse(sdk.moving_record().exists())
                sdk.checkout_record().unlink()

    def test_a_sync_at_the_same_pin_is_not_a_move(self):
        # Another .gclient at the pin.
        self.head = self.NEW
        self.checkout()
        self.assertEqual(self.syncs(), [['--nohooks', '--no-history', '--force', '-r', 'src@'+self.NEW]])
        self.assertNotIn(['unpatch'], self.ran)
        self.assertFalse(sdk.moving_record().exists())

    def test_a_sync_while_a_move_for_another_pin_is_unfinished_is_a_move(self):
        # A move was marked, perhaps after another one left src here part-way, then the pin came back to src's.
        self.head = self.NEW
        sdk.moving_record().touch()
        self.checkout()
        self.assertEqual(self.syncs(), [self.IN_PLACE])
        self.assertNotIn(['unpatch'], self.ran)
        self.assertFalse(sdk.moving_record().exists())

    def test_a_whole_checkout_at_the_pin_is_left_alone(self):
        self.head = self.NEW
        sdk.checkout_record().write_text(json.dumps(self.wanted)+'\n')
        (self.root/'.gclient').write_text((REPO/'chromium/gclient').read_text())
        self.checkout()
        self.assertFalse(any(command[:1] == ['gclient'] for command in self.ran))


if __name__ == '__main__':
    unittest.main()

