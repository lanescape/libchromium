"""Packages a built target as the parts of an SDK version (specs/0001-sdk.md, Parts; specs/0002-build.md,
Packaging). sdk.py package calls package(); the flag and header logic is here so tests can reach it."""
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import archive
import licences

REPO = Path(__file__).resolve().parent.parent
SCHEMA = 1
LIMIT = 2*1024**3
# CI's packages go only to its next job, are kept a day and are never published: a quicker zstd level, and no
# symbols part (specs/0003-ci-cd.md, CI). Only a pull request's version, which is never published, may have one.
QUICK_LEVEL = 3
QUICK_VERSION = re.compile(r'-pr[0-9]+$')

# Root headers: the headers of every target linked into the library whose directory is one of these. Their
# closure under the SDK's flags is what ships (headers()).
HEADER_ROOTS = (
    '//base', '//build', '//cc', '//components/embedder_support', '//components/input',
    '//components/keyed_service', '//components/viz/common', '//components/viz/host', '//content/public', '//gin',
    '//gpu', '//ipc', '//media', '//mojo/public', '//net', '//sandbox', '//skia', '//storage/browser',
    '//storage/common', '//third_party/blink/public', '//ui', '//url', '//v8/include',
)
# Shipped whole: Chromium's libc++, libc++abi and libc++'s configuration for Chromium.
LIBCXX_TREES = ('third_party/libc++/src/include', 'third_party/libc++abi/src/include', 'buildtools/third_party/libc++')
SERVICE_ROOT = re.compile(r'^//services/[^/:]+/public(/|:|$)')
# Chromium's mojom bindings generator and the libraries it imports, as Chromium has them, so that an embedder
# generates the C++ bindings of mojoms of its own that import Chromium's (specs/0001-sdk.md, Mojom).
MOJOM_TOOLS = ('mojo/public/tools/mojom', 'mojo/public/tools/bindings', 'third_party/jinja2', 'third_party/markupsafe',
               'third_party/ply', 'build/action_helpers.py', 'build/gn_helpers.py', 'build/zip_helpers.py')
MOJOM_TOOLS_SKIP = ('*_unittest.py', '__pycache__', '*.pyc')
# Generated sources whose closure joins the headers: Mojo's base types', which include what any generated
# bindings' sources include beyond their headers, such as mojo_buildflags.h.
# The headers the generator writes for a mojom's default variant beside its .mojom.h. An embedder's generated
# bindings include an imported mojom's, so each importable mojom ships them all (mojom_companions()).
MOJOM_COMPANIONS = ('-forward.h', '-import-headers.h', '-shared.h', '-shared-internal.h', '-data-view.h',
                    '-params-data.h', '-send-validation.h', '-features.h', '-shared-message-ids.h')
# The licences of the libraries the mojom tools import, which ship beside them rather than in CREDITS.html
# (specs/0004-licences.md): packaging stops without them.
MOJOM_LICENCES = ('third_party/jinja2/LICENSE.rst', 'third_party/markupsafe/LICENSE', 'third_party/ply/LICENSE')
MOJOM_SOURCES = ('gen/mojo/public/mojom/base/*.mojom.cc', 'gen/mojo/public/mojom/base/*.mojom-shared.cc')

# Compile flags that are about Chromium's own build rather than what its headers need: diagnostics, warnings,
# optimisation, debug information, profiles, LTO and Chromium's style plugins. An embedder picks its own.
DROP_EXACT = {'-c', '-MMD', '-MD', '-Werror', '-w', '-fcolor-diagnostics', '-fno-color-diagnostics',
              '-fwhole-program-vtables', '-fsplit-lto-unit', '-fno-split-lto-unit', '-nostdinc++x'}
DROP_PREFIX = ('-W', '-O', '-g', '-flto', '-fprofile-', '-fdebug-', '-ffile-compilation-dir', '-fdebug-prefix-map',
               '-fmacro-prefix-map', '-ffile-prefix-map', '-fcrash-diagnostics', '-fdiagnostics-',
               '-fsanitize-ignorelist', '-fsanitize-blacklist', '-fcoverage', '--warning-suppression-mappings',
               '-fno-crash-diagnostics', '-fmessage-length', '-ftime-trace', '-fbasic-block-sections',
               '-fdata-sections', '-ffunction-sections', '-funique-section-names', '-fno-unique-section-names',
               '-fexperimental-relative-c++-abi-vtables=0')
DROP_WITH_VALUE = {'-o', '-MF', '-MT', '-MQ', '-x'}
# Chromium's explicit Clang modules need prebuilt module files that stay in its build; an embedder includes the
# same headers as text.
DROP_MODULES = ('-fmodules', '-fno-implicit-module-maps', '-fno-implicit-modules', '-fmodule-', '-fbuiltin-module-map',
                '-fno-modules', '-fmodule-name', '-fmodules-')
XCLANG_DROP = ('-add-plugin', '-load', '-plugin-arg-', '-debug-info-kind', '-fuse-ctor-homing',
               '-fdebug-compilation-dir', '-fno-pch-timestamp', '-fmodules-local-submodule-visibility',
               '-fmodules-disable-diagnostic-validation', '-fmodule-file-home-is-cwd', '-fmodules-cache-path',
               '-fmodule-')
INCLUDE_FLAGS = ('-isystem', '-idirafter', '-iquote')
# Link flags that only tune or diagnose Chromium's own links, or choose where the binary finds its libraries,
# which is the embedder's call.
LINK_DROP_PREFIX = ('-flto', '-Wl,--thinlto', '-Wl,--lto', '-Wl,-mllvm', '-Wl,-plugin-opt', '-Wl,--icf',
                    '-Wl,--color-diagnostics', '--color-diagnostics', '-fprofile-', '-Wl,--gdb-index', '-Werror',
                    '-Wl,-rpath', '-Wl,--no-call-graph-profile-sort', '-Wl,-cache_path_lto', '-Wl,--thinlto-cache',
                    '-Wl,-prune_after_lto', '-Wl,-no_warn', '-Wl,--fatal-warnings', '-Wl,-fatal_warnings',
                    '-Wl,--warn-', '-Wl,--lto-O', '-Wl,-cache_policy', '-fwhole-program-vtables',
                    '-Wl,--build-id', '-Wl,--undefined-version', '-Wl,-mcpu', '-Wl,--save-temps')


class PathLeak(Exception):
    """A flag would carry a build machine's path into the SDK."""


def has_path(arg):
    """Whether a flag names a path in the build tree or an absolute one, anywhere in it."""
    return '../' in arg or arg.startswith(('gen/', 'obj/', './')) or re.search(r'(^|[=,:])/', arg) is not None


def rewrite_path(path, mapping):
    """An in-tree path, relative to the build directory, as {sdk}/..., or None if it isn't one the SDK ships."""
    normal = os.path.normpath(path)
    for prefix, replacement in mapping:
        if normal == prefix:
            return replacement
        if normal.startswith(prefix+'/'):
            return replacement+normal[len(prefix):]
    return None


def path_mapping(target_entry):
    """Prefixes of paths in the build's commands, relative to the build directory, longest first."""
    pairs = [('gen', '{sdk}/gen'), ('../..', '{sdk}/include')]
    if target_entry.get('sysroot'):
        pairs.append((f'../../{target_entry["sysroot"]}', '{sdk}/sysroot'))
    pairs.append(('../../build/mac_files/xcode_binaries/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs',
                  '{apple_sdk}/..'))
    pairs.append(('../../third_party/llvm-build/Release+Asserts', '{sdk}/toolchain'))
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def mapped(path, mapping):
    result = rewrite_path(path, mapping)
    if result is None:
        raise PathLeak(path)
    if result.startswith('{apple_sdk}/../'):
        # Any SDK version: the embedder's own.
        result = '{apple_sdk}'
    return result


def compile_flags(argv, mapping):
    """The flags an embedder compiles with, from one of Chromium's compile commands, with their paths made
    relative to the SDK. Returns (flags, defines, include_dirs, system_include_dirs)."""
    flags, defines, includes, system_includes = [], [], [], []
    args = list(argv[1:])
    i = 0
    drop_next_xclang = False
    while i < len(args):
        arg = args[i]
        nxt = args[i+1] if i+1 < len(args) else None
        if arg == '-Xclang':
            if drop_next_xclang:
                drop_next_xclang = False
            elif nxt.startswith(XCLANG_DROP):
                drop_next_xclang = nxt in ('-add-plugin', '-load') or nxt.startswith('-plugin-arg-')
            elif has_path(nxt):
                raise PathLeak(nxt)
            else:
                flags += [arg, nxt]
            i += 2
            continue
        if arg == '-mllvm':
            i += 2
            continue
        if arg in DROP_WITH_VALUE:
            i += 2
            continue
        if arg in DROP_EXACT or arg.startswith(DROP_PREFIX + DROP_MODULES) or \
                arg.endswith(('.cc', '.c', '.mm', '.m', '.cpp')):
            i += 1
            continue
        if arg.startswith('-D'):
            defines.append(arg[2:])
        elif arg.startswith('-I'):
            value = arg[2:] or nxt
            includes.append(mapped(value, mapping))
            i += 0 if arg[2:] else 1
        elif arg.startswith(INCLUDE_FLAGS):
            flag = next(flag for flag in INCLUDE_FLAGS if arg.startswith(flag))
            value = arg[len(flag):] or nxt
            (system_includes if flag != '-iquote' else includes).append(mapped(value, mapping))
            i += 0 if arg[len(flag):] else 1
        elif arg.startswith('--sysroot='):
            flags.append('--sysroot='+mapped(arg[len('--sysroot='):], mapping))
        elif arg == '-isysroot':
            flags += ['-isysroot', mapped(nxt, mapping)]
            i += 1
        elif has_path(arg):
            raise PathLeak(arg)
        else:
            flags.append(arg)
        i += 1
    return flags, defines, includes, system_includes


def link_flags(variables, mapping):
    """The flags and inputs an embedder links with, from GN's variables for the probe's link. Returns (flags,
    libraries, system_libs, frameworks): libraries are the SDK's files, system_libs names for -l."""
    flags, libraries, system_libs, frameworks = [], [], [], []
    args = variables.get('ldflags', []) + variables.get('libs', []) + variables.get('frameworks', [])
    i = 0
    while i < len(args):
        arg = args[i]
        nxt = args[i+1] if i+1 < len(args) else None
        if arg.startswith(LINK_DROP_PREFIX) or re.fullmatch(r'-Wl,-O\d', arg) or arg.startswith(('-g', '-Wcrl,')):
            i += 1
            continue
        if arg in ('-framework', '-weak_framework'):
            frameworks.append(nxt)
            i += 2
            continue
        if arg.startswith('-l'):
            system_libs.append(arg[2:])
        elif arg.endswith('.a') and not arg.startswith('-'):
            libraries.append(mapped(arg, mapping))
        elif arg.startswith('--sysroot='):
            flags.append('--sysroot='+mapped(arg[len('--sysroot='):], mapping))
        elif arg == '-isysroot':
            flags += ['-isysroot', mapped(nxt, mapping)]
            i += 1
        elif arg.startswith('-L'):
            flags.append('-L'+mapped(arg[2:], mapping))
        elif arg.startswith('-Wl,-rpath-link='):
            flags.append('-Wl,-rpath-link='+mapped(arg[len('-Wl,-rpath-link='):], mapping))
        elif has_path(arg):
            raise PathLeak(arg)
        else:
            flags.append(arg)
        i += 1
    for solib in variables.get('solibs', []):
        if not re.fullmatch(r'\./lib[^/]+\.(so|dylib)', solib):
            raise PathLeak(solib)
        libraries.append('{sdk}/lib/'+solib[2:])
    return flags, libraries, system_libs, frameworks


def unescape(value):
    """A ninja variable's value as the shell words GN wrote."""
    return shlex.split(value.replace('$ ', ' ').replace('$:', ':').replace('$$', '$'))


def target_variables(ninja_file):
    """GN's variables for a target: its compile settings, and those of its link (indented under the link)."""
    variables = {}
    for line in Path(ninja_file).read_text().splitlines():
        match = re.match(r'^\s*(defines|include_dirs|cflags|cflags_c|cflags_cc|cflags_objc|cflags_objcc|ldflags|libs|'
                         r'frameworks|solibs) = ?(.*)$', line)
        if match:
            variables[match.group(1)] = unescape(match.group(2))
    return variables


def compile_argv(variables, language):
    """The compile command's settings for a language, as Chromium's compile rule orders them."""
    specific = {'c++': 'cflags_cc', 'objective-c++': 'cflags_objcc', 'c': 'cflags_c'}[language]
    return (['clang++'] + variables.get('defines', []) + variables.get('include_dirs', []) +
            variables.get('cflags', []) + variables.get(specific, []))


def gn_json(src, out, *args):
    import sdk
    return json.loads(subprocess.check_output([str(sdk.depot_tools()/'gn'), 'desc', '--format=json', str(out), *args],
                                              cwd=src, text=True, env=sdk.env()))


def linked_labels(src, out):
    """Every target the library is built from, in the target's toolchain."""
    return {label for label in gn_json(src, out, '//libchromium:chromium', 'deps', '--all')['//libchromium:chromium']['deps']
            if '(' not in label}


def root_headers(src, out, linked):
    """The headers of the library's public targets, as paths relative to the build directory."""
    described = {}
    for field in ('sources', 'public'):
        for label, fields in gn_json(src, out, '//*', field).items():
            described.setdefault(label, {}).update(fields)
    roots = set()
    for label, fields in described.items():
        if label not in linked:
            continue
        directory = label.split(':')[0]
        if not (any(directory == root or directory.startswith(root+'/') for root in HEADER_ROOTS)
                or SERVICE_ROOT.match(directory)):
            continue
        listed = [value for key in ('sources', 'public') for value in fields.get(key, [])
                  if isinstance(fields.get(key), list)]
        for path in listed:
            if path.endswith('.h') and not re.search(r'(_win|_android|_ios|_fuchsia|_chromeos)\.h$', path):
                roots.add(path)
    for header in (Path(src)/'v8/include').glob('**/*.h'):
        roots.add('//'+header.relative_to(src).as_posix())
    out_label = '//'+Path(out).relative_to(src).as_posix()+'/'
    return sorted('gen/'+path[len(out_label)+4:] if path.startswith(out_label+'gen/')
                  else '../../'+path[2:] for path in roots)


def mojom_sources(out):
    """MOJOM_SOURCES, relative to the build directory. None stops the packaging: Chromium has moved them."""
    found = sorted(path.relative_to(out).as_posix() for pattern in MOJOM_SOURCES for path in Path(out).glob(pattern))
    if not found:
        raise SystemExit(f'no generated sources match {", ".join(MOJOM_SOURCES)}: has Chromium moved Mojo\'s base types?')
    return found


def mojom_companions(shipped, out):
    """The companion headers (MOJOM_COMPANIONS) of every mojom whose .mojom.h ships, that the build has and that
    don't ship yet, as roots relative to the build directory."""
    roots = []
    for relative in sorted(shipped):
        match = re.fullmatch(r'gen/(.+\.mojom)\.h', relative)
        if not match:
            continue
        for suffix in MOJOM_COMPANIONS:
            companion = f'gen/{match.group(1)}{suffix}'
            if companion not in shipped and (Path(out)/companion).is_file():
                roots.append(companion)
    return roots


def absent_companions(shipped, out):
    """The mojoms whose .mojom.h ships but one of whose companions the build doesn't have, which can't be
    imported: an embedder's generated bindings would include the missing header."""
    absent = set()
    for relative in shipped:
        match = re.fullmatch(r'gen/(.+\.mojom)\.h', relative)
        if match and any(f'gen/{match.group(1)}{suffix}' not in shipped and
                         not (Path(out)/f'gen/{match.group(1)}{suffix}').is_file() for suffix in MOJOM_COMPANIONS):
            absent.add(match.group(1))
    return absent


def companion_mojom(root):
    """The mojom a companion header root (mojom_companions()) belongs to, as its path under gen/."""
    for suffix in MOJOM_COMPANIONS:
        if root.endswith(suffix):
            return root.removeprefix('gen/').removesuffix(suffix)
    raise ValueError(f'{root} is no mojom\'s companion header')


def mojom_files(headers, src, out, incomplete=()):
    """The .mojom source and parsed module of every mojom whose C++ bindings ship, keyed by where each goes in the
    headers part: a source from the checkout under include/, one the build generated under gen/, as in Chromium,
    and every module under gen/. A mojom whose bindings ship without its source or module stops the packaging,
    since an embedder's mojom that imports it couldn't be parsed. One whose companion headers don't all ship
    (incomplete) isn't importable, so its source and module stay out: an import of it fails at once, by name."""
    files, missing = {}, []
    for relative in sorted(headers):
        match = re.fullmatch(r'gen/(.+\.mojom)\.h', relative)
        if not match or match.group(1) in incomplete:
            continue
        name = match.group(1)
        module = Path(out)/'gen'/(name+'-module')
        if (Path(src)/name).is_file():
            source, destination = Path(src)/name, f'include/{name}'
        else:
            source, destination = Path(out)/'gen'/name, f'gen/{name}'
        if not module.is_file() or not source.is_file():
            missing.append(name)
            continue
        files[f'gen/{name}-module'] = module
        files[destination] = source
    if missing:
        raise SystemExit(f'{len(missing)} mojoms whose bindings ship have no source or parsed module, such as '
                         f'{", ".join(missing[:5])}')
    return files


def mapping_headers(mappings, src, out):
    """The headers the type mappings name, as roots relative to the build directory: an embedder's generated
    bindings include them, whether or not any of Chromium's public headers do. A header that is in neither the
    checkout nor the build stops the packaging."""
    roots, missing = set(), set()
    for mapping in mappings['c++'].values():
        for header in mapping.get('public_headers', []) + mapping.get('traits_headers', []):
            if (Path(src)/header).is_file():
                roots.add('../../'+header)
            elif (Path(out)/'gen'/header).is_file():
                roots.add('gen/'+header)
            else:
                missing.add(header)
    if missing:
        raise SystemExit(f'the type mappings name {len(missing)} headers that are neither in the checkout nor in '
                         f'the build, such as {", ".join(sorted(missing)[:5])}')
    return sorted(roots)


def unshipped_mapping_headers(roots, headers):
    """The mappings' header roots that aren't among the headers that ship, as the part holds them."""
    shipped = set(headers)
    return sorted(root for root in roots if rewrite_path(root, [('gen', 'gen'), ('../..', 'include')]) not in shipped)


def type_mappings(linked, out):
    """The C++ type mappings of every mojom target the library is built from, merged into one file for an
    embedder's generator. Blink's variant maps the same types to Blink's own, so it's left out: an embedder's
    bindings are the default variant."""
    merged = {}
    for label in sorted(linked):
        directory, _, name = label.partition(':')
        if not name.endswith('__type_mappings') or name.removesuffix('__type_mappings').endswith('_blink'):
            continue
        path = Path(out)/'gen'/directory[2:]/name
        for typename, mapping in json.loads(path.read_text()).get('c++', {}).items():
            if merged.setdefault(typename, mapping) != mapping:
                raise SystemExit(f'{typename} is mapped to C++ two ways, the second by {label}')
    return {'c++': merged}


def module_namespaces(modules, src):
    """Every mojom namespace the parsed modules reach: their own, and those of every module any kind they hold
    belongs to, through fields, parameters and imported kinds, read with Chromium's own mojom package from the
    checkout. An embedder's bindings for a type that imports any of these can need those types' mappings."""
    tools = str(Path(src)/'mojo/public/tools/mojom')
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from mojom.generate import module as mojom_module
    namespaces = set()
    for path in modules:
        with open(path, 'rb') as stream:
            loaded = mojom_module.Module.Load(stream)
        # By identity, within one module's graph, which stays alive while it's walked: ids are reused once freed.
        pending, seen = [loaded], set()
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            if isinstance(item, (list, tuple, set)):
                pending.extend(item)
            elif isinstance(item, dict):
                pending.extend(item.values())
            elif type(item).__module__ == mojom_module.__name__:
                if isinstance(item, mojom_module.Module):
                    namespaces.add(item.mojom_namespace)
                pending.extend(vars(item).values())
    return namespaces


def drop_unusable_mappings(mappings, missing, namespaces):
    """Drops the type mappings that name a header that doesn't ship (missing, as roots), if no importable mojom can
    reach their type: no shipped module reaches the type's namespace (module_namespaces()), so no embedder's
    bindings can use the type, and nothing includes the header. One whose namespace a shipped module reaches stops
    the packaging, whichever target declared the mapping, and so does any, if a reachable module has no namespace.
    Returns the dropped types."""
    dropped = []
    for typename, mapping in list(mappings['c++'].items()):
        if not mapping_roots(mapping) & set(missing):
            continue
        if '' in namespaces or any(typename.startswith(namespace+'.') for namespace in namespaces):
            raise SystemExit(f'{typename} is mapped to a header that doesn\'t ship, yet a shipped mojom module '
                             'declares its namespace')
        del mappings['c++'][typename]
        dropped.append(typename)
    return dropped


def mapping_roots(mapping):
    """A mapping's headers, as mapping_headers() names them as roots."""
    return {prefix+header for header in mapping.get('public_headers', []) + mapping.get('traits_headers', [])
            for prefix in ('../../', 'gen/')}


def mojom_features(src, out):
    """The features the build parsed Chromium's mojoms with, which an embedder's must be parsed with too."""
    label = '//mojo/public/mojom/base:base__parser_action'
    args = gn_json(src, out, label, 'args')[label]['args']
    features = [args[i+1] for i, arg in enumerate(args[:-1]) if arg == '--enable-feature']
    if not features:
        raise SystemExit(f'{label} parses with no --enable-feature: has Chromium changed how mojoms are parsed?')
    return features


def closure(variables, out, roots, language, jobs):
    """Every file the roots include, relative to the build directory, and the roots that don't preprocess alone
    with the SDK's flags, such as another platform's."""
    identity = [(prefix, prefix) for prefix, _ in path_mapping({'sysroot': None})] + [('/', '/')]
    flags, defines, includes, system_includes = compile_flags(compile_argv(variables, language), identity)
    command = (['../../third_party/llvm-build/Release+Asserts/bin/clang++', '-x', language] + flags +
               ['-D'+define for define in defines] + ['-I'+path for path in includes] +
               [part for path in system_includes for part in ('-isystem', path)] + ['-M', '-w'])

    def one(root):
        result = subprocess.run(command+[root], cwd=out, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        if result.returncode:
            # Some of Chromium's headers use its build flags without including them, as its sources always have.
            result = subprocess.run(command+['-include', '../../build/build_config.h', root], cwd=out, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if result.returncode:
            return root, None
        text = result.stdout.replace('\\\n', ' ')
        return root, text.split(':', 1)[1].split()

    found, failed = set(), []
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        for root, deps in pool.map(one, roots):
            if deps is None:
                failed.append(root)
            else:
                found.update(os.path.normpath(dep) for dep in deps)
    return found, failed


def parts_of(entry, quick=False):
    """The parts of a target's package, in the order they're made. A quick package, CI's, has no symbols."""
    names = ['lib']
    if entry['target_os'] == 'linux' and not quick:
        names.append('symbols')
    names += ['headers', 'toolchain']
    if entry.get('sysroot'):
        names.append('sysroot')
    return names + ['notices']


# What the library loads at runtime that ships beside it, among GN's runtime dependencies of it: FFmpeg, ANGLE's
# EGL and GLES libraries, SwiftShader and its Vulkan driver manifest, and the Vulkan loader. Not ANGLE's mock
# driver or Vulkan's validation layers, which are for testing, nor what the build itself runs.
RUNTIME = re.compile(r'^\./(lib(ffmpeg|EGL|GLESv2|vk_swiftshader)\.(so|dylib)|libvulkan\.(so\.1|dylib)|'
                     r'vk_swiftshader_icd\.json)$')


def runtime_files(src, out):
    """The library's runtime dependencies that ship in lib/, by their names in the build directory."""
    import sdk
    listed = subprocess.check_output([str(sdk.depot_tools()/'gn'), 'desc', str(out), '//libchromium:chromium',
                                      'runtime_deps'], cwd=src, text=True, env=sdk.env())
    names = shipped_runtime(listed)
    if not any(name.startswith('libffmpeg') for name in names):
        raise SystemExit('the library no longer lists FFmpeg among its runtime dependencies')
    return names


def shipped_runtime(listing):
    """The names, in the build directory, of what ships from gn desc's runtime_deps listing, which writes a
    linked output as ./<name> and a generated file as <name>."""
    return sorted({name.removeprefix('./') for name in listing.split()
                   if RUNTIME.match(name if name.startswith('./') else './'+name)})


def strip(toolchain, path, system):
    if system == 'mac':
        # Without local symbols. llvm-strip signs ad hoc again what it strips.
        subprocess.run([str(toolchain/'bin/llvm-strip'), '-x', str(path)], check=True)
    else:
        subprocess.run([str(toolchain/'bin/llvm-strip'), '--strip-all', str(path)], check=True)


def strip_libraries(libraries, toolchain, system, symbols):
    """Strips the libraries. On Linux, each one's debug information is kept first, in the directory `symbols`, and
    the stripped library names it by `.gnu_debuglink`, its file name and checksum, wherever that directory is."""
    if system != 'linux':
        for library in libraries:
            strip(toolchain, library, system)
        return
    symbols.mkdir(parents=True)
    for library in libraries:
        debug = symbols/(library.name+'.debug')
        subprocess.run([str(toolchain/'bin/llvm-objcopy'), '--only-keep-debug', str(library), str(debug)],
                       check=True)
        strip(toolchain, library, system)
        subprocess.run([str(toolchain/'bin/llvm-objcopy'), f'--add-gnu-debuglink={debug}', str(library)],
                       check=True)


def debug_size_line(target, symbols):
    """How much debug information a release's symbols part would hold, uncompressed."""
    size = sum(path.stat().st_size for path in symbols.rglob('*') if path.is_file())
    limit = 2 << 30
    verdict = 'under' if size < limit else 'at or over'
    return f'{target}: {size >> 20} MiB of debug information uncompressed, {verdict} the 2 GiB release asset limit'


def package(target, checkout, out, version, output, quick=False):
    from sdk import target_entry, pin, summary  # noqa: avoids a cycle at import
    if quick and not QUICK_VERSION.search(version):
        raise SystemExit(f'{version} is not a pull request\'s version, <tag>-pr<number>: only those, which are never '
                         'published, may be packaged quickly')
    entry = target_entry(target)
    system = entry['target_os']
    src = checkout/'src'
    out = Path(out)
    toolchain_src = src/'third_party/llvm-build/Release+Asserts'
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    stage = output/'stage'/target
    if stage.exists():
        shutil.rmtree(stage)
    mapping = path_mapping(entry)
    suffix = '.dylib' if system == 'mac' else '.so'

    # Flags, from GN's settings for the probe's compiles and link.
    variables = target_variables(out/'obj/libchromium/embedder_probe.ninja')
    flags_cc, defines, includes, system_includes = compile_flags(compile_argv(variables, 'c++'), mapping)
    languages = {'c++': flags_cc}
    if system == 'mac':
        languages['objective-c++'] = compile_flags(compile_argv(variables, 'objective-c++'), mapping)[0]
    ldflags, libraries, system_libs, frameworks = link_flags(variables, mapping)

    # Headers: the closure of the public targets' headers under those flags, and of Mojo's base types' generated
    # sources, whose own includes any embedder's generated bindings need.
    linked = linked_labels(src, out)
    mappings = type_mappings(linked, out)
    mapped = mapping_headers(mappings, src, out)
    sources = set(mojom_sources(out))
    roots = sorted(set(root_headers(src, out, linked)) | sources | set(mapped))
    files, failed = closure(variables, out, roots, 'objective-c++' if system == 'mac' else 'c++',
                            os.cpu_count() or 8)
    headers = {}
    for path in sorted(files):
        if path in sources:
            continue
        rewritten = rewrite_path(path, [('gen', 'gen'), ('../..', 'include')])
        if rewritten and not path.startswith(('../../third_party/llvm-build/', f'../../{entry.get("sysroot", "-")}/',
                                              '../../build/mac_files/')):
            headers[rewritten] = out/path
    summary(f'{target}: {len(headers)} headers from {len(roots)-len(failed)} of {len(roots)} root headers')

    # The companion headers of every mojom whose bindings ship, and what they include, until no new mojom's
    # bindings join. A mojom with a companion that doesn't preprocess isn't importable (mojom_files()).
    incomplete, tried = set(), set()
    while companions := [root for root in mojom_companions(headers, out)
                         if root not in tried and companion_mojom(root) not in incomplete]:
        tried.update(companions)
        files, unpreprocessed = closure(variables, out, companions, 'objective-c++' if system == 'mac' else 'c++',
                                        os.cpu_count() or 8)
        incomplete |= {companion_mojom(root) for root in unpreprocessed}
        for path in sorted(files):
            rewritten = rewrite_path(path, [('gen', 'gen'), ('../..', 'include')])
            if rewritten and not path.startswith(('../../third_party/llvm-build/',
                                                  f'../../{entry.get("sysroot", "-")}/', '../../build/mac_files/')):
                headers[rewritten] = out/path
    incomplete |= absent_companions(headers, out)
    summary(f'{target}: {len(headers)} headers with mojoms\' companions; {len(incomplete)} mojoms not importable')

    parts = {}

    def part(name):
        directory = stage/name
        directory.mkdir(parents=True, exist_ok=True)
        parts[name] = directory
        return directory

    # The library, its runtime files, its settings and notices.
    lib = part('lib')
    (lib/'lib').mkdir()
    runtime_libraries = []
    # What the probe links, and what the library loads at runtime, from GN's own list (runtime_files()).
    names = [solib[2:] for solib in variables.get('solibs', [])]
    names += [name for name in runtime_files(src, out) if name not in names]
    for name in names:
        shutil.copy2(out/name, lib/'lib'/name)
        runtime_libraries.append(f'lib/{name}')
    resources = lib/'resources'
    # The packs, without the lists of what's in them that the build keeps.
    shutil.copytree(out/'libchromium', resources, ignore=shutil.ignore_patterns('*.info'))
    for name in ('icudtl.dat', 'v8_context_snapshot.bin', 'snapshot_blob.bin'):
        if (out/name).exists():
            shutil.copy2(out/name, resources/name)
    shutil.copy2(out/'args.gn', lib/'args.gn')

    # Debug information, then the stripped libraries.
    binaries = [lib/relative for relative in runtime_libraries if not relative.endswith('.json')]
    symbols = None
    if system == 'linux':
        # A quick package keeps no symbols part, but its libraries are stripped and linked to their debug
        # information just as a release's are, so CI tests the same library.
        symbols = (stage/'debug' if quick else part('symbols'))/'symbols'
    strip_libraries(binaries, toolchain_src, system, symbols)
    if quick and symbols is not None:
        # CI makes no symbols part, so this is the only sign of how near a release's is to the 2 GiB limit: the
        # part holds this, compressed, so a size under the limit here keeps it under.
        print(debug_size_line(target, symbols), flush=True)

    # libc++'s whole tree, whatever Chromium's headers happen to include: an embedder may use any of it.
    for tree in LIBCXX_TREES:
        for path in (src/tree).rglob('*'):
            if path.is_file():
                headers[f'include/{path.relative_to(src).as_posix()}'] = path
    # A mapping whose header doesn't ship, because it doesn't preprocess with the SDK's flags, would make an
    # embedder's generated bindings include a file the SDK lacks.
    # Mojom: what an embedder needs to generate its own mojoms' bindings, beside the headers they compile against.
    importable = mojom_files(headers, src, out, incomplete)
    if unshipped := unshipped_mapping_headers(mapped, headers):
        namespaces = module_namespaces([path for name, path in importable.items() if name.endswith('-module')], src)
        dropped = drop_unusable_mappings(mappings, unshipped, namespaces)
        summary(f'{target}: dropped {len(dropped)} type mappings whose headers don\'t ship, in namespaces no '
                f'importable mojom reaches: {", ".join(dropped)}')
    headers.update(importable)
    headers_dir = part('headers')
    for relative, source in headers.items():
        destination = headers_dir/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    for tool in MOJOM_TOOLS:
        source, destination = src/tool, headers_dir/'mojom'/tool
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*MOJOM_TOOLS_SKIP))
        else:
            shutil.copy2(source, destination)
    (headers_dir/'mojom').mkdir(exist_ok=True)
    if missing := [name for name in MOJOM_LICENCES if not (headers_dir/'mojom'/name).is_file()]:
        raise SystemExit(f'the mojom tools ship without their licences: {", ".join(missing)}')
    (headers_dir/'mojom/type_mappings.json').write_text(json.dumps(mappings, indent=1, sort_keys=True)+'\n')
    features = mojom_features(src, out)

    toolchain = part('toolchain')/'toolchain'
    clang_version = copy_toolchain(toolchain_src, toolchain, entry)

    if entry.get('sysroot'):
        shutil.copytree(src/entry['sysroot'], part('sysroot')/'sysroot', symlinks=True)

    notices = part('notices')/'notices'
    licences.notices(src, out, notices, version)
    shutil.copytree(notices, lib/'notices')

    description = {
        'schema': SCHEMA,
        'version': version,
        'target': target,
        'chromium': pin(),
        'system': system,
        'cpu': entry['target_cpu'],
        'clang': clang_version,
        'toolchain': {
            'cc': '{sdk}/toolchain/bin/clang', 'cxx': '{sdk}/toolchain/bin/clang++',
            'ar': '{sdk}/toolchain/bin/llvm-ar', 'nm': '{sdk}/toolchain/bin/llvm-nm',
            'strip': '{sdk}/toolchain/bin/llvm-strip', 'objcopy': '{sdk}/toolchain/bin/llvm-objcopy',
            'linker': '{sdk}/toolchain/bin/'+('ld64.lld' if system == 'mac' else 'ld.lld'),
        },
        'sysroot': '{sdk}/sysroot' if entry.get('sysroot') else None,
        'defines': defines,
        'include_dirs': includes,
        'system_include_dirs': system_includes,
        'compile': languages,
        'link': {'flags': ldflags, 'libraries': libraries, 'system_libs': system_libs, 'frameworks': frameworks},
        'mojom': {
            'parser': '{sdk}/mojom/mojo/public/tools/mojom/mojom_parser.py',
            'generator': '{sdk}/mojom/mojo/public/tools/bindings/mojom_bindings_generator.py',
            'imports': ['{sdk}/include', '{sdk}/gen'],
            'modules': '{sdk}/gen',
            'type_mappings': '{sdk}/mojom/type_mappings.json',
            'features': features,
        },
        'runtime': {'libraries': runtime_libraries,
                    'resources': sorted(p.relative_to(lib).as_posix() for p in resources.rglob('*') if p.is_file())},
        'unpreprocessed_root_headers': len(failed),
    }
    if system == 'mac':
        description['apple_sdk'] = {'built_with': entry['mac_sdk']['sdk'], 'xcode': entry['mac_sdk']['xcode']}
    (lib/'sdk.json').write_text(json.dumps(description, indent=2)+'\n')

    if list(parts) != parts_of(entry, quick):
        raise SystemExit(f'{target} made the parts {", ".join(parts)}, not {", ".join(parts_of(entry, quick))}')
    written = []
    for name, directory in parts.items():
        asset = output/f'libchromium-{version}-{target}-{name}.tar.zst'
        archive.write(directory, asset, level=QUICK_LEVEL if quick else archive.LEVEL)
        size = asset.stat().st_size
        listed = {'name': asset.name, 'target': target, 'part': name, 'size': size}
        if quick:
            listed['quick'] = True  # publish.py refuses it.
        written.append(listed)
        summary(f'{asset.name}: {size/1024**2:.0f} MiB')
        if size >= LIMIT:
            raise SystemExit(f'{asset.name} is {size} bytes, at or over the 2 GiB limit for a release asset')
    (output/f'assets-{target}.json').write_text(json.dumps(written, indent=2)+'\n')
    shutil.rmtree(output/'stage')


TOOLS = ('clang', 'clang++', 'lld', 'ld.lld', 'ld64.lld', 'llvm-ar', 'llvm-nm', 'llvm-objcopy', 'llvm-objdump',
         'llvm-readelf', 'llvm-readobj', 'llvm-strip', 'llvm-symbolizer', 'llvm-cxxfilt', 'llvm-dwarfdump',
         'llvm-install-name-tool', 'llvm-otool')


def copy_toolchain(source, destination, entry):
    """Chromium's clang, its own headers and the target's runtime libraries; it runs on Linux x86-64."""
    (destination/'bin').mkdir(parents=True)
    for tool in TOOLS:
        path = source/'bin'/tool
        if path.is_symlink():
            os.symlink(os.readlink(path), destination/'bin'/tool)
        elif path.exists():
            shutil.copy2(path, destination/'bin'/tool)
    versions = sorted((source/'lib/clang').iterdir())
    resource = versions[-1]
    target_dir = 'darwin' if entry['target_os'] == 'mac' else entry['clang_target']
    (destination/'lib/clang'/resource.name/'lib').mkdir(parents=True)
    shutil.copytree(resource/'include', destination/'lib/clang'/resource.name/'include')
    shutil.copytree(resource/'lib'/target_dir, destination/'lib/clang'/resource.name/'lib'/target_dir)
    for name in ('cr_build_revision',):
        if (source/name).exists():
            shutil.copy2(source/name, destination/name)
    revision = (source/'cr_build_revision').read_text().strip() if (source/'cr_build_revision').exists() else ''
    licences.toolchain_notice(source.parent.parent.parent, destination)
    return {'version': resource.name, 'revision': revision}
