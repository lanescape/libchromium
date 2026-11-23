import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.scripts.common import REPO  # noqa: F401
import archive
import licences
import package
import publish
import sdk

PIN = {'tag': '157.0.8103.39', 'commit': '9'*40}
REPOSITORY = 'owner/name'


def assets(directory, sizes):
    for name, data in sizes.items():
        (directory/name).write_bytes(data)


# Each target's notices list one copyleft component and one that isn't; its source is archived by its directory.
COMPONENTS = [{'name': 'FFmpeg', 'dir': 'third_party/ffmpeg', 'license': 'LGPL 2.1', 'copyleft': True,
               'source_dirs': ['third_party/ffmpeg'], 'source_files': 1,
               'source_listing': archive.listing_digest([archive.Member('third_party/ffmpeg/a.c', 'file', 0)])},
              {'name': 'zlib', 'dir': 'third_party/zlib', 'license': 'Zlib', 'copyleft': False}]
SOURCES = {'libchromium-1-source-third-party-ffmpeg.tar.zst': {'part': 'source', 'component': 'FFmpeg',
                                                               'license': 'LGPL 2.1', 'dir': 'third_party/ffmpeg'},
           'libchromium-1-source-libchromium.tar.zst': {'part': 'source', 'component': 'libchromium'},
           'libchromium-1-source-debian-bullseye-sysroots.tar.zst': {'part': 'source',
                                                                     'component': 'debian-bullseye-sysroots'}}


def complete(quick=False, without=None):
    """What CD's jobs say about every part of every target and every source, but `without`."""
    described = {f'libchromium-1-{target}-{part}.tar.zst': {'target': target, 'part': part,
                                                            **({'quick': True} if quick else {})}
                 for target, entry in publish.targets().items() for part in package.parts_of(entry)}
    described.update({name: dict(entry) for name, entry in SOURCES.items()})
    return {name: entry for name, entry in described.items() if name != without}


# What this repository tracks at the release's commit, as its source asset holds it: each file's text.
TRACKED = {'libchromium/LICENSE': 'MIT', 'libchromium/chromium/patches/v8.patch': 'diff',
           'libchromium/scripts/sdk.py': ''}
CONTROL = 'Package: libc6\nSource: glibc\nVersion: 2.31-13+deb11u6\n'
ORIG = 'glibc source'
DSC = ('Format: 3.0 (quilt)\nSource: glibc\nChecksums-Sha256:\n '+publish.hashlib.sha256(ORIG.encode()).hexdigest() +
       f' {len(ORIG)} glibc_2.31.orig.tar.xz\n-----BEGIN PGP SIGNATURE-----\n\nx\n-----END PGP SIGNATURE-----\n')


def members(name, entry, components=COMPONENTS):
    """What an asset holds, as its job makes it: each member's path, and a file's text, None for a directory, or
    ('->', target) for a link."""
    if entry.get('part') == 'notices':
        return {'notices': None, 'notices/components.json': json.dumps(components)}
    if entry.get('component') == 'libchromium':
        return {'libchromium': None, **TRACKED}
    if entry.get('component') == licences.SYSROOT_SOURCE:
        return {'packages.json': json.dumps([{'source': 'glibc', 'version': '2.31-13+deb11u6',
                                              'dsc': 'https://snapshot.invalid/pool/main/g/glibc/glibc_2.31-13.dsc'}]),
                'debian/glibc': None, 'debian/glibc/glibc_2.31-13.dsc': DSC,
                'debian/glibc/glibc_2.31.orig.tar.xz': ORIG}
    if entry.get('part') == 'source':
        return {f'{directory}/a.c': '' for directory in entry['dir'].split(', ')}
    if entry.get('part') == 'sysroot':
        return {'sysroot': None, 'sysroot/debian/libc6/DEBIAN/control': CONTROL}
    return {'x': name}


def write_assets(directory, described, held=members):
    """Each asset as a stand-in archive: what it holds, as JSON, which the stand-in archive.scan() reads."""
    assets(directory, {name: json.dumps(held(name, entry)).encode() for name, entry in described.items()})


def member(name, held):
    """The stand-in archive's member, as archive.scan() reports one."""
    if held is None:
        return archive.Member(name, 'dir')
    if isinstance(held, list):
        return archive.Member(name, 'symlink', 0, held[1])
    return archive.Member(name, 'file', len(held.encode()))


class StandInArchives(unittest.TestCase):
    """An asset here is a stand-in archive, the JSON of what it holds, so these tests need no zstd."""

    def setUp(self):
        def scan(path, wanted=(), digested=()):
            held = json.loads(Path(path).read_bytes())
            if not isinstance(held, dict):
                raise RuntimeError('not an archive')
            wants = wanted if callable(wanted) else wanted.__contains__
            digests = digested if callable(digested) else digested.__contains__
            return ([member(name, text) for name, text in held.items()],
                    {name: text.encode() for name, text in held.items() if isinstance(text, str) and wants(name)},
                    {name: publish.hashlib.sha256(text.encode()).hexdigest() for name, text in held.items()
                     if isinstance(text, str) and digests(name)})

        tracked = {name: ('file', len(text.encode())) for name, text in TRACKED.items()}
        for patcher in (mock.patch.object(publish.archive, 'scan', scan),
                        mock.patch.object(publish, 'tracked', lambda commit: dict(tracked))):
            patcher.start()
            self.addCleanup(patcher.stop)


class Manifest(unittest.TestCase):
    def test_lists_every_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            names = ['libchromium-1-linux-amd64-lib.tar.zst', 'libchromium-1-source-libchromium.tar.zst']
            assets(tmp, {name: name.encode() for name in names})
            described = {names[0]: {'target': 'linux-amd64', 'part': 'lib'}, names[1]: {'part': 'source'}}
            manifest = publish.write_manifest(tmp, '1', 'c'*40, PIN, described, REPOSITORY)
            self.assertEqual([a['name'] for a in manifest['assets']], sorted(names))
            self.assertEqual(manifest['tag'], 'v1')
            sums = (tmp/'SHA256SUMS').read_text().splitlines()
            self.assertEqual(len(sums), 3)
            self.assertTrue(sums[-1].endswith('  manifest.json'))
            publish.check_local(tmp, manifest)
            (tmp/names[0]).write_bytes(b'changed')
            with self.assertRaises(SystemExit):
                publish.check_local(tmp, manifest)

    def test_points_to_chromium_and_the_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            assets(tmp, {'libchromium-1-x.tar.zst': b'x'})
            manifest = publish.write_manifest(tmp, '157.0.8103.39-2', 'c'*40, PIN,
                                              {'libchromium-1-x.tar.zst': {'target': 'linux-amd64'}}, REPOSITORY)
            base = 'https://commondatastorage.googleapis.com/chromium-browser-official/chromium-157.0.8103.39.tar.xz'
            self.assertEqual(manifest['source'], {
                'chromium': {'tag': '157.0.8103.39', 'commit': '9'*40, 'tarball': base, 'hashes': base+'.hashes'},
                'patch': {'tag': 'v157.0.8103.39-2', 'commit': 'c'*40,
                          'url': 'https://github.com/owner/name/tree/v157.0.8103.39-2/chromium/patches',
                          'commit_url': f'https://github.com/owner/name/tree/{"c"*40}/chromium/patches'}})
            self.assertEqual(json.loads((tmp/'manifest.json').read_text())['source'], manifest['source'])
            notes = publish.body(manifest)
            for text in (base, base+'.hashes', '157.0.8103.39', manifest['source']['patch']['url'], 'c'*40):
                self.assertIn(text, notes)

    def test_undescribed_or_missing_assets_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            assets(tmp, {'libchromium-1-x.tar.zst': b'x'})
            with self.assertRaises(SystemExit):
                publish.write_manifest(tmp, '1', 'c'*40, PIN, {}, REPOSITORY)
            with self.assertRaises(SystemExit):
                publish.write_manifest(tmp, '1', 'c'*40, PIN, {'libchromium-1-x.tar.zst': {},
                                                               'libchromium-1-y.tar.zst': {}}, REPOSITORY)

    def test_two_jobs_may_describe_one_asset_the_same_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            entry = {'name': 'libchromium-1-source-x.tar.zst', 'part': 'source', 'size': 3, 'dir': 'x'}
            (tmp/'assets-a.json').write_text(json.dumps([entry]))
            (tmp/'assets-b.json').write_text(json.dumps([dict(entry, size=3)]))
            described = publish.merge_descriptions([tmp/'assets-a.json', tmp/'assets-b.json'])
            self.assertEqual(described, {'libchromium-1-source-x.tar.zst': {'part': 'source', 'dir': 'x'}})
            (tmp/'assets-c.json').write_text(json.dumps([dict(entry, dir='y')]))
            with self.assertRaises(SystemExit):
                publish.merge_descriptions([tmp/'assets-a.json', tmp/'assets-c.json'])

    def test_size_limit(self):
        self.assertEqual(publish.too_large({'a': publish.LIMIT-1, 'b': publish.LIMIT, 'c': publish.LIMIT+1}),
                         ['b', 'c'])

    def test_gather_refuses_two_different_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for job, data in (('one', b'a'), ('two', b'a')):
                (tmp/job).mkdir()
                assets(tmp/job, {'libchromium-1-source-x.tar.zst': data})
                (tmp/job/'assets-sources.json').write_text('[]')
            publish.gather(tmp/'out', [tmp/'one', tmp/'two'])
            self.assertTrue((tmp/'out/libchromium-1-source-x.tar.zst').exists())
            (tmp/'three').mkdir()
            assets(tmp/'three', {'libchromium-1-source-x.tar.zst': b'b'})
            with self.assertRaises(SystemExit):
                publish.gather(tmp/'out2', [tmp/'one', tmp/'three'])


class IsPublic(unittest.TestCase):
    """is_public() asks with HEAD and no credentials, and retries only what may pass on a second try."""

    def answers(self, *outcomes):
        requests = []
        outcomes = list(outcomes)

        class Response:
            def __init__(self, status):
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def urlopen(request, timeout=None):
            requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return Response(outcome)

        saved = publish.urllib.request.urlopen, publish.time.sleep
        publish.urllib.request.urlopen, publish.time.sleep = urlopen, lambda _: None
        self.addCleanup(setattr, publish.time, 'sleep', saved[1])
        self.addCleanup(setattr, publish.urllib.request, 'urlopen', saved[0])
        return requests

    @staticmethod
    def http_error(code):
        return publish.urllib.error.HTTPError('https://example.invalid/x', code, 'x', {}, None)

    def test_head_without_credentials(self):
        requests = self.answers(200)
        self.assertTrue(publish.is_public('https://example.invalid/x'))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_method(), 'HEAD')
        self.assertFalse(any(name.lower() == 'authorization' for name, _ in requests[0].header_items()))

    def test_missing_is_not_retried(self):
        requests = self.answers(self.http_error(404))
        self.assertFalse(publish.is_public('https://example.invalid/x'))
        self.assertEqual(len(requests), 1)

    def test_server_and_network_failures_are_retried(self):
        requests = self.answers(self.http_error(503), OSError('reset'), 200)
        self.assertTrue(publish.is_public('https://example.invalid/x'))
        self.assertEqual(len(requests), 3)

    def test_gives_up_after_three_tries(self):
        requests = self.answers(self.http_error(503), OSError('reset'), self.http_error(500))
        self.assertFalse(publish.is_public('https://example.invalid/x'))
        self.assertEqual(len(requests), 3)


class FakeGitHub:
    """Records what release() asks of GitHub."""

    def __init__(self, tags=(), releases=()):
        self.tags = set(tags)
        self.existing = list(releases)
        self.calls = []
        self.uploaded = {}
        self.published = False

    def tag_exists(self, tag):
        return tag in self.tags

    def releases(self):
        return self.existing

    def api(self, method, path, body=None, expect=(200,)):
        self.calls.append((method, path))
        if method == 'POST' and path == '/releases':
            return 201, {'id': 7}
        if method == 'GET' and path.startswith('/releases/7/assets'):
            return 200, [{'id': i, 'name': name, 'size': len(data), 'digest': 'sha256:'+publish.hashlib.sha256(data).hexdigest()}
                         for i, (name, data) in enumerate(sorted(self.uploaded.items()))]
        if method == 'PATCH':
            self.published = True
            self.tags.add(body['tag_name'])
            return 200, {'html_url': 'https://example.invalid/release'}
        if method == 'GET' and path.startswith('/git/ref/tags/'):
            return 200, {'object': {'sha': 'c'*40}}
        return 204, None

    def upload(self, release, path):
        self.uploaded[path.name] = path.read_bytes()

    def asset_sha256(self, asset):
        return asset['digest'][len('sha256:'):]


class Sources(StandInArchives):
    def test_every_source_there_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            described = complete()
            write_assets(tmp, described)
            publish.check_release(tmp, '1', 'c'*40, described, publish.targets())

    def test_each_missing_source_fails(self):
        for name in SOURCES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                described = complete(without=name)
                write_assets(tmp, described)
                with self.assertRaises(SystemExit) as raised:
                    publish.check_release(tmp, '1', 'c'*40, described, publish.targets())
                self.assertIn('corresponding source missing', str(raised.exception))

    def test_a_copyleft_component_needs_its_own_directory_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            described = complete()
            described['libchromium-1-source-third-party-ffmpeg.tar.zst']['dir'] = 'third_party/other'
            write_assets(tmp, described)
            with self.assertRaises(SystemExit) as raised:
                publish.check_release(tmp, '1', 'c'*40, described, publish.targets())
            self.assertIn('FFmpeg (third_party/ffmpeg)', str(raised.exception))

    def test_a_source_by_another_name_doesn_t_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            described = complete(without='libchromium-1-source-libchromium.tar.zst')
            described['libchromium-1-source-other.tar.zst'] = {'part': 'source', 'component': 'libchromium'}
            write_assets(tmp, described)
            with self.assertRaises(SystemExit) as raised:
                publish.check_release(tmp, '1', 'c'*40, described, publish.targets())
            self.assertIn('libchromium-1-source-libchromium.tar.zst', str(raised.exception))

    def test_notices_without_a_list_of_components_fail(self):
        for listed in ({}, [], [{}], [{'name': 'x', 'dir': 'x', 'copyleft': 'yes'}],
                       [{'name': 'x', 'dir': 'third_party/x', 'copyleft': True}],
                       [dict(COMPONENTS[0], source_dirs=['third_party/y'])],
                       [{key: value for key, value in COMPONENTS[0].items() if key != 'source_files'}],
                       [dict(COMPONENTS[0], source_files=0)],
                       [dict(COMPONENTS[0], source_listing='x')],
                       [{'name': '', 'dir': 'third_party/x', 'copyleft': True}],
                       [{'name': 'x', 'dir': ' ', 'copyleft': True}], 'x'):
            with self.subTest(listed=listed), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'notices.tar.zst'
                path.write_text(json.dumps({'notices/components.json': json.dumps(listed)}))
                with self.assertRaises(SystemExit):
                    publish.components_of(path)

    def test_a_source_that_doesn_t_hold_what_it_s_for_fails(self):
        repository = members('', {'component': 'libchromium'})
        debian = 'libchromium-1-source-debian-bullseye-sysroots.tar.zst'
        sysroots = members('', {'component': licences.SYSROOT_SOURCE})
        cases = [('libchromium-1-source-libchromium.tar.zst', {'libchromium/README.md': ''}),
                 ('libchromium-1-source-libchromium.tar.zst',
                  {name: text for name, text in repository.items() if not name.endswith('v8.patch')}),
                 ('libchromium-1-source-libchromium.tar.zst', dict(repository, **{'libchromium/LICENSE': 'MI'})),
                 ('libchromium-1-source-libchromium.tar.zst', dict(repository, **{'libchromium/LICENSE': None})),
                 (debian, dict(sysroots, **{'packages.json': '[]'})),
                 (debian, dict(sysroots, **{'packages.json': '[null]'})),
                 (debian, {name: text for name, text in sysroots.items() if not name.endswith('.orig.tar.xz')}),
                 # A file the .dsc names, cut short.
                 (debian, dict(sysroots, **{'debian/glibc/glibc_2.31.orig.tar.xz': ORIG[:-1]})),
                 (debian, {name: text for name, text in sysroots.items() if not name.endswith('.dsc')}),
                 (debian, dict(sysroots, **{'packages.json': sysroots['packages.json'].replace('2.31-13.dsc',
                                                                                              '2.31-14.dsc')})),
                 # A package the sysroots have, left out of both packages.json and the archive.
                 (debian, {'packages.json': json.dumps([{'source': 'zlib', 'version': '1', 'dsc': 'x/zlib_1.dsc'}]),
                           'debian/zlib/zlib_1.dsc': DSC.replace('glibc_2.31.orig.tar.xz', 'zlib_1.tar.xz'),
                           'debian/zlib/zlib_1.tar.xz': ORIG}),
                 # A .dsc cut short after its checksums.
                 (debian, dict(sysroots, **{'debian/glibc/glibc_2.31-13.dsc': DSC.split('-----BEGIN')[0]})),
                 # A file of the component's left out, cut short, or a link in its place.
                 ('libchromium-1-source-third-party-ffmpeg.tar.zst', {'third_party/ffmpeg/b.c': '',
                                                                      'third_party/ffmpeg/c.c': ''}),
                 ('libchromium-1-source-third-party-ffmpeg.tar.zst', {'third_party/ffmpeg/a.c': 'more'}),
                 ('libchromium-1-source-third-party-ffmpeg.tar.zst', {'third_party/ffmpeg/a.c': ['->', 'b.c']}),
                 ('libchromium-1-source-third-party-ffmpeg.tar.zst', {'third_party/ffmpeg': None,
                                                                      'third_party/ffmpeg/sub': None,
                                                                      'third_party/other/a.c': ''})]
        for broken, held in cases:
            with self.subTest(broken=broken, held=held), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                described = complete()
                write_assets(tmp, described, lambda name, entry: held if name == broken else members(name, entry))
                with self.assertRaises(SystemExit) as raised:
                    publish.check_release(tmp, '1', 'c'*40, described, publish.targets())
                self.assertIn(f'corresponding source not whole: {broken}', str(raised.exception))

    def test_a_crate_s_vendored_source_is_needed_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            described = complete()
            crate = 'libchromium-1-source-third-party-ffmpeg.tar.zst'
            components = [dict(COMPONENTS[0], source_dirs=['third_party/ffmpeg', 'third_party/vendor/x'],
                               source_files=2, source_listing=archive.listing_digest([
                                   archive.Member('third_party/ffmpeg/a.c', 'file', 0),
                                   archive.Member('third_party/vendor/x/a.c', 'file', 0)])), COMPONENTS[1]]
            # The sources job left the vendored source out of both the archive and its listing.
            write_assets(tmp, described, lambda name, entry: members(name, entry, components))
            with self.assertRaises(SystemExit) as raised:
                publish.check_release(tmp, '1', 'c'*40, described, publish.targets())
            self.assertIn('third_party/vendor/x/', str(raised.exception))

    def test_targets_that_list_a_shared_source_differently_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            described = complete()
            other = [dict(COMPONENTS[0], source_files=2, source_listing='0'*64), COMPONENTS[1]]
            write_assets(tmp, described, lambda name, entry: members(name, entry, other)
                         if entry.get('target') == 'macos-arm64' else members(name, entry))
            with self.assertRaises(SystemExit) as raised:
                publish.check_release(tmp, '1', 'c'*40, described, publish.targets())
            self.assertIn('list different files for libchromium-1-source-third-party-ffmpeg.tar.zst',
                          str(raised.exception))

    def test_a_source_that_isn_t_an_archive_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            described = complete()
            write_assets(tmp, described)
            (tmp/'libchromium-1-source-third-party-ffmpeg.tar.zst').write_text('[]')
            with self.assertRaises(SystemExit) as raised:
                publish.check_release(tmp, '1', 'c'*40, described, publish.targets())
            self.assertIn('not a whole .tar.zst archive', str(raised.exception))

    def test_no_sysroot_needs_no_debian_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            targets = {name: entry for name, entry in publish.targets().items() if not entry.get('sysroot')}
            described = {name: entry for name, entry in complete(
                without='libchromium-1-source-debian-bullseye-sysroots.tar.zst').items()
                if entry.get('target') in (None, *targets)}
            write_assets(tmp, described)
            publish.check_release(tmp, '1', 'c'*40, described, targets)

    def test_manifest_refuses_a_version_without_a_source(self):
        # As CD runs it: the jobs' listings beside their assets, with one source and its listing left out.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            described = complete(without='libchromium-1-source-third-party-ffmpeg.tar.zst')
            write_assets(tmp, described)
            (tmp/'assets-all.json').write_text(json.dumps([dict(entry, name=name, size=1)
                                                           for name, entry in described.items()]))
            argv = ['publish.py', 'manifest', str(tmp), '--version', '1', '--commit', 'c'*40,
                    '--repository', REPOSITORY]
            with mock.patch.object(sys, 'argv', argv), self.assertRaises(SystemExit) as raised:
                publish.main()
            self.assertIn('FFmpeg', str(raised.exception))
            self.assertFalse((tmp/'manifest.json').exists())


class Components(unittest.TestCase):
    def test_reads_the_notices_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp/'part/notices').mkdir(parents=True)
            (tmp/'part/notices/components.json').write_text(json.dumps(COMPONENTS))
            archive.write(tmp/'part', tmp/'notices.tar.zst')
            self.assertEqual(publish.components_of(tmp/'notices.tar.zst'), COMPONENTS)
            (tmp/'part/notices/components.json').unlink()
            archive.write(tmp/'part', tmp/'empty.tar.zst')
            with self.assertRaises(SystemExit):
                publish.components_of(tmp/'empty.tar.zst')


class FromTheJobs(unittest.TestCase):
    """CD's publish job as it runs: each target's job output, its source made by `sdk.py sources` itself, gathered
    and checked by `manifest`. Only Chromium's licence metadata and Debian's downloads are stood in for."""

    FFMPEG = {'name': 'FFmpeg', 'dir': 'third_party/ffmpeg', 'license': 'LGPL 2.1', 'copyleft': True}
    ZLIB = {'name': 'zlib', 'dir': 'third_party/zlib', 'license': 'Zlib', 'copyleft': False}
    # A Rust crate, whose source asset holds its vendored source too; only linux-arm64 links it here.
    CRATE = {'name': 'symphonia-core', 'dir': 'third_party/rust/symphonia_core/v0_5', 'license': 'MPL 2.0',
             'copyleft': True}
    VENDORED = 'third_party/rust/chromium_crates_io/vendor/symphonia-core-0.5.4'
    SNAPSHOT = 'https://snapshot.debian.org/archive/debian/20250129T203412Z/'

    def components(self, target):
        return [dict(self.FFMPEG), dict(self.ZLIB)] + ([dict(self.CRATE)] if target == 'linux-arm64' else [])

    def checkout(self, tmp):
        src = tmp/'checkout/src'
        for path, text in {'LICENSE': 'Chromium', 'third_party/ffmpeg/a.c': '', 'third_party/zlib/z.c': '',
                           f'{self.CRATE["dir"]}/BUILD.gn': f'sources = ["//{self.VENDORED}/src/lib.rs"]',
                           f'{self.VENDORED}/src/lib.rs': ''}.items():
            (src/path).parent.mkdir(parents=True, exist_ok=True)
            (src/path).write_text(text)
        for target, entry in publish.targets().items():
            if entry.get('sysroot'):
                control = src/entry['sysroot']/'debian/libc6/DEBIAN/control'
                control.parent.mkdir(parents=True)
                control.write_text('Package: libc6\nSource: glibc\nVersion: 2.31-13+deb11u6\n')
                listing = (src/'build/linux/sysroot_scripts/generated_package_lists' /
                           f'bullseye.{ {"x64": "amd64", "arm64": "arm64"}[entry["target_cpu"]] }')
                listing.parent.mkdir(parents=True, exist_ok=True)
                listing.write_text(self.SNAPSHOT+'pool/main/g/glibc/libc6_2.31-13+deb11u6_amd64.deb\n')

    @staticmethod
    def fetch(url, destination, sha=None):
        if url.endswith('.dsc'):
            destination.write_text('Format: 3.0 (quilt)\nSource: glibc\nChecksums-Sha256:\n'
                                   f' {publish.hashlib.sha256(b"orig").hexdigest()} 4 glibc_2.31.orig.tar.xz\n'
                                   '-----BEGIN PGP SIGNATURE-----\n\nx\n-----END PGP SIGNATURE-----\n')
        else:
            destination.write_bytes(b'orig')

    def notices(self, tmp, target, version):
        """licences.notices() itself, with Chromium's credits tool and the reviewed gaps stood in for."""
        def credits(command, **_):
            Path(command[-1]).write_text('<html></html>')

        with mock.patch.object(licences, 'components', lambda src, out: self.components(target)), \
                mock.patch.object(licences, 'gaps', dict), mock.patch.object(licences.subprocess, 'run', credits):
            licences.notices(tmp/'checkout/src', tmp/'out', tmp/'stage'/target/'notices', version)

    def jobs(self, tmp, version='1'):
        self.checkout(tmp)
        environment = mock.patch.dict('os.environ', {'LIBCHROMIUM_CHECKOUT': str(tmp/'checkout')})
        environment.start()
        self.addCleanup(environment.stop)
        for target, entry in publish.targets().items():
            job = tmp/'jobs'/f'sdk-{target}'
            job.mkdir(parents=True)
            parts = []
            for part in package.parts_of(entry):
                name = f'libchromium-{version}-{target}-{part}.tar.zst'
                if part == 'notices':
                    # As package.py makes it: the notices directory, in the part's own.
                    self.notices(tmp, target, version)
                    archive.write(tmp/'stage'/target, job/name)
                elif part == 'sysroot':
                    # As package.py makes it: the whole sysroot.
                    shutil.copytree(tmp/'checkout/src'/entry['sysroot'], tmp/'stage'/f'{target}-sysroot/sysroot',
                                    symlinks=True)
                    archive.write(tmp/'stage'/f'{target}-sysroot', job/name)
                else:
                    (job/name).write_bytes(name.encode())
                parts.append({'name': name, 'target': target, 'part': part, 'size': (job/name).stat().st_size})
            (job/f'assets-{target}.json').write_text(json.dumps(parts))
            arguments = mock.Mock(output=str(job), version=version, cache=str(tmp/'cache'),
                                  sysroots=target == 'linux-amd64')
            with mock.patch.object(licences, 'components', lambda src, out, target=target: self.components(target)), \
                    mock.patch.object(licences, 'fetch', self.fetch), \
                    mock.patch.dict('os.environ', {'GITHUB_STEP_SUMMARY': ''}), mock.patch('builtins.print'):
                sdk.step_sources(target, arguments)

    def manifest(self, tmp):
        release = tmp/'release'
        publish.gather(release, sorted((tmp/'jobs').iterdir()))
        # The commit sdk.py sources archives, HEAD, as CD's are the same.
        commit = subprocess.run(['git', '-C', str(publish.REPO), 'rev-parse', 'HEAD'], check=True, text=True,
                                stdout=subprocess.PIPE).stdout.strip()
        argv = ['publish.py', 'manifest', str(release), '--version', '1', '--commit', commit,
                '--repository', REPOSITORY]
        with mock.patch.object(sys, 'argv', argv), mock.patch('builtins.print'):
            publish.main()
        return json.loads((release/'manifest.json').read_text())

    def test_every_source_there_makes_a_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.jobs(tmp)
            names = {entry['name'] for entry in self.manifest(tmp)['assets']}
            for source in ('third-party-rust-symphonia-core-v0-5', 'third-party-ffmpeg', 'libchromium',
                           'debian-bullseye-sysroots'):
                self.assertIn(f'libchromium-1-source-{source}.tar.zst', names)
            self.assertNotIn('libchromium-1-source-third-party-zlib.tar.zst', names)
            crate, _, _ = archive.scan(tmp/'release/libchromium-1-source-third-party-rust-symphonia-core-v0-5.tar.zst')
            self.assertIn((f'{self.VENDORED}/src/lib.rs', 'file', 0, ''), crate)

    def test_one_target_s_missing_source_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.jobs(tmp)
            job = tmp/'jobs/sdk-linux-arm64'
            name = 'libchromium-1-source-third-party-rust-symphonia-core-v0-5.tar.zst'
            (job/name).unlink()
            listing = job/'assets-sources-linux-arm64.json'
            listing.write_text(json.dumps([entry for entry in json.loads(listing.read_text())
                                           if entry['name'] != name]))
            with self.assertRaises(SystemExit) as raised:
                self.manifest(tmp)
            self.assertIn(f"{name}, for linux-arm64's symphonia-core", str(raised.exception))
            self.assertFalse((tmp/'release/manifest.json').exists())

    def test_a_file_left_out_of_a_source_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp/'checkout/src/third_party/ffmpeg/b.c').parent.mkdir(parents=True)
            (tmp/'checkout/src/third_party/ffmpeg/b.c').write_text('')
            original = licences.source_archives

            def leaving_out(src, out, output, version):
                (Path(src)/'third_party/ffmpeg/b.c').unlink(missing_ok=True)
                try:
                    return original(src, out, output, version)
                finally:
                    (Path(src)/'third_party/ffmpeg/b.c').write_text('')

            with mock.patch.object(licences, 'source_archives', leaving_out):
                self.jobs(tmp)
            with self.assertRaises(SystemExit) as raised:
                self.manifest(tmp)
            self.assertIn('libchromium-1-source-third-party-ffmpeg.tar.zst: its files in third_party/ffmpeg/ aren\'t '
                          'those its notices list, 1 of 2 files', str(raised.exception))

    def test_a_source_cut_short_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.jobs(tmp)
            path = tmp/'jobs/sdk-linux-amd64/libchromium-1-source-libchromium.tar.zst'
            path.write_bytes(path.read_bytes()[:len(path.read_bytes())//2])
            with self.assertRaises(SystemExit) as raised:
                self.manifest(tmp)
            self.assertIn('libchromium-1-source-libchromium.tar.zst is not a whole', str(raised.exception))


class Requests(unittest.TestCase):
    """GitHub's refusals and dropped connections, as request() and upload() see them."""

    SECRET = 'https://uploads.example.invalid/x?X-Signature=secret'

    def answering(self, *outcomes):
        outcomes = list(outcomes)
        requests = []

        class Response:
            def __init__(self, status, text):
                self.status, self.text = status, text

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, size=-1):
                text, self.text = self.text, b''
                return text

        def urlopen(request, timeout=None):
            requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return Response(*outcome)

        for name, value in (('urlopen', urlopen),):
            patcher = mock.patch.object(publish.urllib.request, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeping = mock.patch.object(publish.time, 'sleep', lambda _: None)
        sleeping.start()
        self.addCleanup(sleeping.stop)
        return requests

    def refusal(self, code, body):
        import io
        return publish.urllib.error.HTTPError(self.SECRET, code, 'x', {}, io.BytesIO(body))

    def test_a_refusal_says_only_its_status(self):
        for body in (json.dumps({'message': f'Validation Failed, see {self.SECRET} with token',
                                 'upload_url': self.SECRET}).encode(), f'<html>{self.SECRET}</html>'.encode()):
            with self.subTest(body=body):
                self.answering(self.refusal(422, body))
                with self.assertRaises(SystemExit) as raised:
                    publish.GitHub('owner/name', 'token').request('POST', self.SECRET, b'x', expect=(201,))
                self.assertEqual(str(raised.exception), 'GitHub answered 422 to POST /x')

    def test_a_refusal_cut_short_is_a_dropped_connection(self):
        class Truncated(publish.urllib.error.HTTPError):
            def read(self, *_):
                raise publish.http.client.IncompleteRead(b'<html>')

        self.answering(Truncated(self.SECRET, 502, 'x', {}, None))
        with self.assertRaises(SystemExit) as raised:
            publish.GitHub('owner/name', 'token').request('GET', self.SECRET)
        self.assertEqual(str(raised.exception), 'GET /x failed: IncompleteRead')

    def test_a_request_keeps_the_token_from_a_redirect(self):
        requests = self.answering((200, b'{}'))
        publish.GitHub('owner/name', 'token').api('GET', '/releases/1')
        self.assertEqual(requests[0].unredirected_hdrs, {'Authorization': 'Bearer token'})
        self.assertNotIn('Authorization', requests[0].headers)

    def test_a_redirect_to_no_address_says_only_its_kind(self):
        self.answering(ValueError(f'unknown url type: {self.SECRET}'))
        with self.assertRaises(SystemExit) as raised:
            publish.GitHub('owner/name', 'token').request('GET', self.SECRET)
        self.assertEqual(str(raised.exception), 'GET /x failed: ValueError')

    def test_an_asset_s_download_keeps_the_token_from_the_redirect(self):
        requests = self.answering((200, b'data'))
        digest = publish.GitHub('owner/name', 'token').asset_sha256({'id': 5})
        self.assertEqual(digest, publish.hashlib.sha256(b'data').hexdigest())
        self.assertEqual(requests[0].unredirected_hdrs, {'Authorization': 'Bearer token'})
        self.assertNotIn('Authorization', requests[0].headers)

    def test_an_asset_s_failed_download_says_only_its_status_or_kind(self):
        for outcome, said in ((self.refusal(404, self.SECRET.encode()), 'GitHub answered 404 to GET '),
                              (ConnectionResetError(self.SECRET), 'GET ')):
            with self.subTest(said=said):
                self.answering(outcome)
                with self.assertRaises(SystemExit) as raised:
                    publish.GitHub('owner/name', 'token').asset_sha256({'id': 5})
                self.assertTrue(str(raised.exception).startswith(said+'/repos/owner/name/releases/assets/5'))
                self.assertNotIn('secret', str(raised.exception))

    def test_a_dropped_connection_says_only_its_kind(self):
        for error in (ConnectionResetError(f'reset by {self.SECRET}'), TimeoutError('timed out'),
                      publish.urllib.error.URLError(self.SECRET), publish.http.client.IncompleteRead(b'x')):
            with self.subTest(error=type(error).__name__):
                self.answering(error)
                with self.assertRaises(SystemExit) as raised:
                    publish.GitHub('owner/name', 'token').request('GET', self.SECRET)
                self.assertEqual(str(raised.exception), f'GET /x failed: {type(error).__name__}')

    def test_an_upload_is_tried_again_after_a_dropped_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'libchromium-1-x.tar.zst'
            path.write_bytes(b'data')
            requests = self.answering(ConnectionResetError('reset'),
                                      (200, json.dumps([{'id': 5, 'name': path.name}]).encode()),
                                      (204, b''),
                                      (201, json.dumps({'id': 6, 'name': path.name}).encode()))
            self.assertEqual(publish.GitHub('owner/name', 'token').upload(7, path), {'id': 6, 'name': path.name})
            self.assertEqual([request.get_method() for request in requests], ['POST', 'GET', 'DELETE', 'POST'])
            self.assertTrue(requests[2].full_url.endswith('/releases/assets/5'))

    def test_an_upload_is_tried_again_after_a_refusal_cut_short(self):
        class Truncated(publish.urllib.error.HTTPError):
            def read(self, *_):
                raise publish.http.client.IncompleteRead(b'')

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'libchromium-1-x.tar.zst'
            path.write_bytes(b'data')
            self.answering(Truncated(self.SECRET, 502, 'x', {}, None), (200, b'[]'),
                           (201, json.dumps({'id': 6}).encode()))
            self.assertEqual(publish.GitHub('owner/name', 'token').upload(7, path), {'id': 6})

    def test_an_upload_is_tried_again_after_an_answer_cut_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'libchromium-1-x.tar.zst'
            path.write_bytes(b'data')
            for cut in (b'', b'{"id": 6, "na'):
                with self.subTest(cut=cut):
                    requests = self.answering((201, cut), (200, b'[]'), (201, json.dumps({'id': 6}).encode()))
                    self.assertEqual(publish.GitHub('owner/name', 'token').upload(7, path), {'id': 6})
                    self.assertEqual([request.get_method() for request in requests], ['POST', 'GET', 'POST'])

    def test_an_upload_is_tried_again_after_an_empty_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'libchromium-1-x.tar.zst'
            path.write_bytes(b'data')
            requests = self.answering(TimeoutError(), (200, b''), (201, json.dumps({'id': 6}).encode()))
            self.assertEqual(publish.GitHub('owner/name', 'token').upload(7, path), {'id': 6})
            self.assertEqual([request.get_method() for request in requests], ['POST', 'GET', 'POST'])

    def test_an_upload_gives_up_after_three_tries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'libchromium-1-x.tar.zst'
            path.write_bytes(b'data')
            requests = self.answering(TimeoutError(), (200, b'[]'), TimeoutError(),
                                      ConnectionResetError(), TimeoutError())
            with self.assertRaises(SystemExit) as raised:
                publish.GitHub('owner/name', 'token').upload(7, path)
            self.assertEqual(str(raised.exception), 'POST /repos/owner/name/releases/7/assets failed: TimeoutError')
            # The second clean-up's listing dropped too, and the third try went ahead all the same.
            self.assertEqual([request.get_method() for request in requests], ['POST', 'GET', 'POST', 'GET', 'POST'])


class Release(StandInArchives):
    def setUp(self):
        super().setUp()
        # Chromium's tarball and checksums, which the release points to, are published.
        self.checked = []
        self.saved = publish.is_public
        publish.is_public = lambda url: self.checked.append(url) or True

    def tearDown(self):
        publish.is_public = self.saved

    def prepared(self, tmp, quick=False, without=None):
        """Every part of every target and every source, as CD's jobs make them, but `without`."""
        described = complete(quick, without)
        write_assets(tmp, described)
        manifest = publish.write_manifest(tmp, '1', 'c'*40, PIN, {name: {key: value for key, value in entry.items()
                                                                         if key != 'quick'}
                                                                  for name, entry in described.items()}, REPOSITORY)
        if quick:
            # A manifest made some other way than `manifest`, which refuses a quick part.
            for entry in manifest['assets']:
                entry['quick'] = True
            (tmp/'manifest.json').write_text(publish.json.dumps(manifest))
        return sorted(described)

    def test_refuses_a_quick_or_partial_release_before_asking_github(self):
        for case in ({'quick': True}, {'without': 'libchromium-1-linux-arm64-symbols.tar.zst'},
                     {'without': 'libchromium-1-source-libchromium.tar.zst'},
                     {'without': 'libchromium-1-source-third-party-ffmpeg.tar.zst'}):
            with self.subTest(**case), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                self.prepared(tmp, **case)
                github = FakeGitHub()
                # check_local refuses the quick manifest's changed bytes first; this tests the check behind it.
                with mock.patch.object(publish, 'check_local'), self.assertRaises(SystemExit):
                    publish.release(tmp, github)
                self.assertEqual(github.calls, [])
                self.assertFalse(github.published)

    def test_publishes_after_uploading_and_verifying(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            names = self.prepared(tmp)
            github = FakeGitHub()
            publish.release(tmp, github)
            self.assertTrue(github.published)
            self.assertEqual(sorted(github.uploaded), sorted(names + ['SHA256SUMS', 'manifest.json']))
            self.assertEqual(github.calls[-2][0], 'PATCH')
            self.assertEqual([url.rsplit('/', 1)[1] for url in self.checked],
                             ['chromium-157.0.8103.39.tar.xz', 'chromium-157.0.8103.39.tar.xz.hashes'])

    def test_waits_for_chromium_s_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.prepared(tmp)
            publish.is_public = lambda url: not url.endswith('.hashes')
            github = FakeGitHub()
            with self.assertRaises(SystemExit):
                publish.release(tmp, github)
            self.assertEqual(github.calls, [])
            self.assertFalse(github.published)

    def test_an_existing_tag_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.prepared(tmp)
            github = FakeGitHub(tags=['v1'])
            with self.assertRaises(SystemExit):
                publish.release(tmp, github)
            self.assertEqual(github.calls, [])

    def test_a_published_release_fails_and_a_draft_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.prepared(tmp)
            github = FakeGitHub(releases=[{'id': 3, 'tag_name': 'v1', 'name': '1', 'draft': False}])
            with self.assertRaises(SystemExit):
                publish.release(tmp, github)
            github = FakeGitHub(releases=[{'id': 3, 'tag_name': 'v1', 'name': '1', 'draft': True}])
            publish.release(tmp, github)
            self.assertEqual(github.calls[0], ('DELETE', '/releases/3'))
            self.assertTrue(github.published)

    def test_a_corrupted_upload_stays_a_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.prepared(tmp)
            github = FakeGitHub()
            original = github.upload
            github.upload = lambda release, path: (original(release, path),
                                                   github.uploaded.__setitem__(path.name, b'bad')
                                                   if path.name.endswith('lib.tar.zst') else None)
            with self.assertRaises(SystemExit):
                publish.release(tmp, github)
            self.assertFalse(github.published)


if __name__ == '__main__':
    unittest.main()
