"""Notices and corresponding source for an SDK version (specs/0004-licences.md).

Notices ship inside the library part and as their own part. Corresponding source is every component whose licence
asks for it, found from Chromium's own metadata, as built: the checkout's files, with the SDK's patch applied. The
Linux sysroots are Debian's packages, so their source is Debian's source packages at the versions in them.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import archive

REPO = Path(__file__).resolve().parent.parent
# Licences that ask for the source of what's distributed, in Chromium's README.chromium License fields.
COPYLEFT = re.compile(r'\b(L?GPL|MPL|EPL|CDDL|CPL|OSL)\b|lgpl|mpl-|gpl-', re.IGNORECASE)
# Never inputs to the build: Blink's web tests are most of its directory.
UNBUILT = {'web_tests', 'perf_tests', 'manual_tests', '.git'}
TARGET = '//libchromium:libchromium'
# Chromium's official source tarballs, one for each release tag, with a .hashes file beside each.
CHROMIUM_TARBALLS = 'https://commondatastorage.googleapis.com/chromium-browser-official'
# Where this repository is published, which its releases and notices name.
REPOSITORY = 'lanescape/libchromium'
# The asset that holds the Debian source packages of every Linux target's sysroot.
SYSROOT_SOURCE = 'debian-bullseye-sysroots'

_DISCOVER = r'''
import argparse, json, sys
sys.path.insert(0, "tools/licenses")
import licenses
args = argparse.Namespace(extra_third_party_dirs=None, extra_allowed_dirs=None, exclude_dirs=None,
                          scan_root=".", gn_out_dir=sys.argv[1], gn_target=sys.argv[2], enable_warnings=True,
                          shipped_only=False, target_os=None)
# Diagnostics go to stderr, so stdout is only the answer.
real_stdout, sys.stdout = sys.stdout, sys.stderr
metadatas, had_errors = licenses._DiscoverMetadatas(args)
sys.stdout = real_stdout
print(json.dumps({"errors": bool(had_errors),
                  "components": [{"name": m["Name"], "license": m.get("License", ""), "dir": m.get("dir", ".")}
                                 for m in metadatas]}))
'''
GAP_MISSING = re.compile(r'^Error: missing third party metadata file or licenses\.py SPECIAL_CASES entry in (\S+)$')
GAP_ERRORS = re.compile(r'^Errors in (\S+)/[^/]+:$')
# Rust crates: Chromium's metadata sits in third_party/rust/<crate>/<epoch>, and the source it builds is vendored.
VENDORED = re.compile(r'//third_party/rust/chromium_crates_io/vendor/([^/"]+)/')


def components(src, out):
    """Every third-party component the SDK's targets link or ship: name, licence and directory under src."""
    gn_out = os.path.relpath(out, src)
    result = subprocess.run([sys.executable, '-c', _DISCOVER, gn_out, TARGET], cwd=src, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    answer = json.loads(result.stdout)
    if answer['errors']:
        # A component whose metadata Chromium can't read is left out of its credits. Only the reviewed gaps,
        # whose notices this repository carries instead, may be.
        unknown = unexplained(result.stderr, gaps())
        if unknown:
            sys.stderr.write(result.stderr)
            raise SystemExit("Chromium's licence metadata has gaps chromium/licence-gaps.json doesn't explain: " +
                             ', '.join(unknown))
    found = []
    for entry in answer['components']:
        directory = os.path.relpath(os.path.join(src, entry['dir']), src) if entry['dir'] else '.'
        entry['dir'] = directory
        entry['copyleft'] = bool(COPYLEFT.search(entry['license'])) and directory != '.'
        found.append(entry)
    return found


def gaps():
    entries = json.loads((REPO/'chromium/licence-gaps.json').read_text())
    return {directory: entry for directory, entry in entries.items() if directory != '_'}


def unexplained(diagnostics, known):
    """The diagnostics of licenses.py that the reviewed gaps don't explain. Every message, a line that doesn't
    start with a space, must name a reviewed gap; indented lines are a message's details."""
    unknown = []
    for line in diagnostics.splitlines():
        if not line.strip() or line[0].isspace():
            continue
        match = GAP_MISSING.match(line) or GAP_ERRORS.match(line)
        if not match or match.group(1) not in known:
            unknown.append(match.group(1) if match else line)
    return unknown


def notices(src, out, destination, version):
    """Chromium's licence, the credits for everything the build links, this repository's licence, the list of
    components with their licences, and where the source is that the release points to."""
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(src)/'LICENSE', destination/'LICENSE.chromium')
    shutil.copy2(REPO/'LICENSE', destination/'LICENSE.libchromium')
    subprocess.run([sys.executable, 'tools/licenses/licenses.py', 'credits', '--gn-target', TARGET,
                    '--gn-out-dir', os.path.relpath(out, src), str(destination/'CREDITS.html')],
                   cwd=src, check=True)
    listed = components(src, out)
    # The gaps' licences, whose texts Chromium's credits leave out.
    for directory, entry in sorted(gaps().items()):
        target = destination/'gaps'/slug(directory)
        for number, name in enumerate(entry['files']):
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(src)/name, target/f'{number+1}-{slug(name)}')
        listed.append({'name': directory, 'license': entry['license'] or '', 'dir': directory, 'copyleft': False,
                       'credited_in': f'gaps/{slug(directory)}' if entry['files'] else None})
    # Where each copyleft component's source is, and how many files it is, which its source asset must hold:
    # publishing checks it.
    for entry in listed:
        if entry['copyleft']:
            entry['source_dirs'] = source_dirs(src, entry['dir'])
            listing = source_listing(src, entry['source_dirs'])
            entry['source_files'] = sum(1 for _, kind, _, _ in listing if kind == 'file')
            entry['source_listing'] = archive.listing_digest(listing)
    (destination/'components.json').write_text(json.dumps(listed, indent=2, sort_keys=True)+'\n')
    (destination/'README').write_text(notices_readme(json.loads((REPO/'chromium/pin.json').read_text()), version))
    return listed


def notices_readme(pin, version):
    """The notices' README: what each file is, and where Chromium's source and the patch are, so the pointers
    travel with the binaries."""
    source = chromium_source(pin)
    return ('Notices for this libchromium version.\n\n'
            'LICENSE.chromium     Chromium\'s licence.\n'
            'CREDITS.html         The licences of every third-party component the SDK\'s library and files link\n'
            '                     or ship.\n'
            'LICENSE.libchromium  The licence of the scripts that built this version.\n'
            'components.json      Each component, its licence, and whether its corresponding source is published\n'
            '                     with this version (copyleft: true), as a source-<name> asset of the same release.\n'
            'gaps/                The licences of components Chromium\'s metadata leaves out of CREDITS.html.\n'
            'The mojom tools in the headers part\'s mojom/ directory, Jinja, MarkupSafe and PLY among them, carry\n'
            'their licences in their own directories rather than in CREDITS.html.\n\n'
            f'This version is Chromium {source["tag"]} ({source["commit"]}) with this repository\'s patch.\n'
            'Chromium\'s source for the tag is its official source tarball, with its checksums:\n'
            f'  {source["tarball"]}\n  {source["hashes"]}\n'
            'The patch, which the source-libchromium asset of the same release holds with the scripts that built\n'
            f'this version:\n  {patch_url(f"v{version}")}\n')


def patch_url(ref, repository=REPOSITORY):
    """This repository's patch to Chromium at a tag or commit."""
    return f'https://github.com/{repository}/tree/{ref}/chromium/patches'


def chromium_source(pin):
    """Where Chromium's own source at the pin is: its tag, commit, and Chromium's official source tarball for the
    tag with its checksums file, which the release points to rather than copies (specs/0004-licences.md)."""
    tarball = f'{CHROMIUM_TARBALLS}/chromium-{pin["tag"]}.tar.xz'
    return {'tag': pin['tag'], 'commit': pin['commit'], 'tarball': tarball, 'hashes': f'{tarball}.hashes'}


def toolchain_notice(src, destination):
    """The toolchain is LLVM's, under the Apache License 2.0 with LLVM Exceptions, which libc++ shares."""
    shutil.copy2(Path(src)/'third_party/libc++/src/LICENSE.TXT', destination/'LICENSE.TXT')


def source_dirs(src, directory):
    """The directories that hold a component's source as built: its own, and for a Rust crate, its vendored
    source, which its BUILD.gn names."""
    dirs = [directory]
    build = Path(src)/directory/'BUILD.gn'
    if directory.startswith('third_party/rust/') and build.is_file():
        for vendored in sorted(set(VENDORED.findall(build.read_text()))):
            path = f'third_party/rust/chromium_crates_io/vendor/{vendored}'
            if (Path(src)/path).is_dir() and path not in dirs:
                dirs.append(path)
    return dirs


def source_listing(src, dirs):
    """What a component's source asset holds below each of its directories, as source_archives() copies them,
    without what the build never reads and with links kept as links: (path under src, kind, size, link target)
    for each entry, as archive.scan() reports an archive's members."""
    entries = []

    def entry(path):
        relative = os.path.relpath(path, src).replace(os.sep, '/')
        if os.path.islink(path):
            return archive.Member(relative, 'symlink', 0, os.readlink(path))
        if os.path.isdir(path):
            return archive.Member(relative, 'dir')
        if os.path.isfile(path):
            return archive.Member(relative, 'file', os.path.getsize(path))
        return archive.Member(relative, 'other')

    for directory in dirs:
        for root, names, files in os.walk(Path(src)/directory):
            names[:] = [name for name in names if name not in UNBUILT]
            entries += [entry(os.path.join(root, name)) for name in names + files if name not in UNBUILT]
    return entries


def slug(directory):
    return re.sub(r'[^a-z0-9.]+', '-', directory.lower()).strip('-')


def source_archives(src, out, output, version):
    """Archives each copyleft component's source, as built, beside the parts: one asset for each set of
    directories, which components sharing a directory share. Returns the assets written."""
    groups = {}
    for component in components(src, out):
        if component['copyleft']:
            groups.setdefault(tuple(source_dirs(src, component['dir'])), []).append(component)
    written = []
    for dirs, members in sorted(groups.items()):
        with tempfile.TemporaryDirectory() as tmp:
            for directory in dirs:
                shutil.copytree(Path(src)/directory, Path(tmp)/directory, symlinks=True,
                                ignore=lambda _, names: [n for n in names if n in UNBUILT])
            asset = Path(output)/f'libchromium-{version}-source-{slug(dirs[0])}.tar.zst'
            archive.write(tmp, asset)
        written.append({'name': asset.name, 'part': 'source',
                        'component': ', '.join(sorted(m['name'] for m in members)),
                        'license': '; '.join(sorted({m['license'] for m in members})),
                        'dir': ', '.join(dirs), 'size': asset.stat().st_size})
    return written


# Debian's source packages for a sysroot.

def sysroot_packages(sysroot):
    """(source package, version) for every binary package in a Chromium sysroot."""
    return packages_of([control.read_text(errors='replace')
                        for control in sorted(Path(sysroot).glob('debian/*/DEBIAN/control'))], sysroot)


def packages_of(controls, sysroot):
    """(source package, version) for each binary package's control file of a sysroot."""
    found = set()
    for text in controls:
        fields = parse_control(text)
        version = fields['Version']
        source = fields.get('Source', fields['Package'])
        match = re.match(r'^(\S+)\s+\(([^)]+)\)$', source)
        if match:
            source, version = match.groups()
        found.add((source, version))
    if not found:
        # Its packages are what the asset must hold the source of, so none would make an empty one.
        raise SystemExit(f'{sysroot} has no Debian packages to find the source of')
    return sorted(found)


def parse_control(text):
    fields = {}
    key = None
    for line in text.splitlines():
        if line[:1] in (' ', '\t') and key:
            fields[key] += '\n'+line.strip()
        elif ':' in line:
            key, value = line.split(':', 1)
            fields[key] = value.strip()
    return fields


def pool_path(source, version):
    prefix = source[:4] if source.startswith('lib') else source[0]
    upstream = version.split(':', 1)[-1]
    return f'pool/main/{prefix}/{source}/{source}_{upstream}.dsc'


def snapshot_base(package_list):
    first = Path(package_list).read_text().split()[0]
    return first[:first.index('/pool/')+1]


def fetch(url, destination, sha=None):
    """Downloads a file into the cache, whole or not at all: a download a stopped machine cut short never
    reaches its name."""
    if destination.exists() and (sha is None or sha256(destination) == sha):
        return
    partial = destination.with_name(destination.name+'.partial')
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=300) as stream, open(partial, 'wb') as out:
                shutil.copyfileobj(stream, out)
                length = stream.headers.get('Content-Length')
            if length is not None and partial.stat().st_size != int(length):
                raise OSError(f'{url} ended early')
            break
        except OSError:
            if attempt == 4:
                raise
    if sha is not None and sha256(partial) != sha:
        raise SystemExit(f'{url} does not match its .dsc')
    if sha is None and destination.suffix == '.dsc' and not complete_dsc(partial.read_text(errors='replace')):
        raise SystemExit(f'{url} is not a whole, signed .dsc')
    os.replace(partial, destination)


def complete_dsc(text):
    """Whether a .dsc is whole: Debian signs every one, so a whole one ends with its signature."""
    return text.rstrip().endswith('-----END PGP SIGNATURE-----') and 'Checksums-Sha256:' in text


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def sysroot_sources(src, sysroots, cache, output, version, name):
    """Downloads the Debian source packages of every package in the sysroots, verified against their .dsc, and
    archives them with each package's copyright as one asset."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    packages = set()
    bases = set()
    for sysroot, package_list in sysroots:
        packages.update(sysroot_packages(Path(src)/sysroot))
        bases.add(snapshot_base(Path(src)/package_list))
    if len(bases) != 1:
        raise SystemExit(f'sysroots come from different snapshots: {bases}')
    base = bases.pop()
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)/'debian'
        stage.mkdir()
        listed = []
        for source, version_ in sorted(packages):
            dsc_path = pool_path(source, version_)
            directory = stage/source
            directory.mkdir(exist_ok=True)
            dsc = cache/Path(dsc_path).name
            if dsc.exists() and not complete_dsc(dsc.read_text(errors='replace')):
                dsc.unlink()
            fetch(base+dsc_path, dsc)
            files = checksums(dsc.read_text(errors='replace'))
            shutil.copy2(dsc, directory/dsc.name)
            for file_name, sha in files:
                cached = cache/file_name
                fetch(base+str(Path(dsc_path).parent/file_name), cached, sha)
                shutil.copy2(cached, directory/file_name)
            listed.append({'source': source, 'version': version_, 'dsc': base+dsc_path})
        (Path(tmp)/'packages.json').write_text(json.dumps(listed, indent=2)+'\n')
        asset = Path(output)/f'libchromium-{version}-source-{name}.tar.zst'
        # Debian's files are compressed already.
        archive.write(tmp, asset, level=3)
    return {'name': asset.name, 'part': 'source', 'component': name, 'license': 'Debian source packages',
            'size': asset.stat().st_size}


def checksums(dsc_text):
    fields = parse_control(dsc_text)
    entries = []
    for line in fields.get('Checksums-Sha256', '').splitlines():
        parts = line.split()
        if len(parts) == 3:
            entries.append((parts[2], parts[0]))
    if not entries:
        raise SystemExit('a .dsc without SHA-256 checksums')
    return entries


def sdk_source(output, version, commit='HEAD'):
    """This repository at the built commit: the settings, the patch and the scripts that built the version."""
    asset = Path(output)/f'libchromium-{version}-source-libchromium.tar.zst'
    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp)/'repo.tar'
        subprocess.run(['git', '-C', str(REPO), 'archive', '--format=tar', f'--prefix=libchromium/', '-o', str(tar),
                        commit], check=True)
        subprocess.run(['tar', '-xf', str(tar), '-C', tmp], check=True)
        tar.unlink()
        archive.write(tmp, asset)
    # MIT, but for the excerpt of Chromium's own code a test runs, under Chromium's licence beside it.
    return {'name': asset.name, 'part': 'source', 'component': 'libchromium',
            'license': 'MIT, and BSD-3-Clause for tests/scripts/fixtures (Chromium)',
            'size': asset.stat().st_size}
