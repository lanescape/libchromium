#!/usr/bin/env python3
"""Writes chromium/patches/ from a clean checkout at the pin: export_patch.py <checkout src>.

The SDK's changes to Chromium (specs/0002-build.md, The patch). They add the build argument
`libchromium_exports`, which makes a non-component build's shared libraries export what a component build
exports between its components, and turns off what can't hold across that boundary:

- Every export header that keys its `visibility("default")` annotation on `COMPONENT_BUILD` (PartitionAlloc's on
  its own IS_COMPONENT_BUILD flag) also takes `LIBCHROMIUM_EXPORTS`, which the argument defines for every target. One rule covers them all, so moving the
  pin only needs this script run again.
- `PRESERVE_MOST` in base and V8 stays off, as in a component build: a lazily bound call to an exported function
  would clobber the registers it promises to keep.
- Components are source sets in the target's toolchain, as each is linked whole into its library in a component
  build, so the library keeps every export, whether anything in it calls the export or not.
- Skia, skcms, Abseil, BoringSSL and Perfetto, which choose their exports in GN rather than in a header, export
  as in a component build, and so do V8's platform and base libraries. V8's own API has its own argument,
  `v8_expose_public_symbols` (chromium/args.gn).
- V8's thread-locals use a model a shared library can hold, as in a component build, rather than the
  executable-only local-exec. (Blink's heap has an argument of its own for this, set in chromium/args.gn.)
- On macOS the allocator shim's operator new and delete stay visible, as in a component build, matching shared
  libc++'s declarations.
- macOS's shared libc++ gets an @rpath install name, as every library in a component build does.
- Executables and shared libraries find the shared libraries beside them, as in a component build, since libc++
  and FFmpeg are shared libraries here too, and the build runs executables of its own, such as protoc.
- A macOS build without V8's context snapshot generates: Chrome's own bundle, which the SDK never builds, lists
  V8's snapshot without depending on what makes it.
- Every mojom target's C++, its generated code and the traits sources it compiles itself, but for Blink's
  variant, is exported, which a component build compiles hidden into each component that uses it, so that an
  embedder's own mojoms can import Chromium's. Traits a separate target compiles stay hidden.
- Two targets the library's export list names but which have no export macros are compiled with default
  visibility: //components/embedder_support:user_agent, Chrome's user agent, made a source set, and //ui/events/blink.
- Whole-program vtable optimisation stays off: it assumes the link sees every class that implements an
  interface without exported visibility, and an embedder implements Content's delegates outside the library.

Apart from the argument, it carries one fix the pin doesn't have: macOS's linker driver writes a shared
library's .TOC with a list joined once, rather than by appending to bytes, which took minutes for this library
(https://issues.chromium.org/issues/587040685). Chromium has the same fix from commit 394153f1299c
(refs/heads/main@{#1747697}).

The patches are applied by `sdk.py patch`. This script runs only when the pin moves, on a checkout with no
changes of its own, and leaves the checkout as it found it.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO/'chromium/patches'
DEFINE = 'LIBCHROMIUM_EXPORTS'
ARG = 'libchromium_exports'

# Export headers in these trees are never compiled into the SDK: Chrome itself and what only it links, other
# platforms, and tests.
SKIPPED = re.compile(r'(^|/)(test|tests|testing)/|^(chrome|ash|chromeos|android_webview|ios|fuchsia_web|'
                     r'remoting|third_party/(jni_zero|nearby|openscreen|pdfium))/|_android\b|_win\b|_ios\b|_fuchsia\b')
# Headers that test COMPONENT_BUILD without exporting anything, but must follow a component build here.
ALSO = ('base/compiler_specific.h',)
DIRECTIVE = re.compile(r'^\s*#\s*(if|elif)\b')
IFDEF = re.compile(r'^(\s*#\s*)(ifdef|ifndef)\s+COMPONENT_BUILD\b')
TOKEN = 'defined(COMPONENT_BUILD)'
REPLACEMENT = f'(defined(COMPONENT_BUILD) || defined({DEFINE}))'
# PartitionAlloc's export header keys on its own build flag rather than the define.
PA_TOKEN = 'PA_BUILDFLAG(IS_COMPONENT_BUILD)'
PA_REPLACEMENT = f'(PA_BUILDFLAG(IS_COMPONENT_BUILD) || defined({DEFINE}))'

# Exact edits to GN files and the linker driver: (path relative to src, old text, new text). Each old text
# must occur exactly once.
EDITS = (
    ('build/config/BUILDCONFIG.gn',
     '  is_component_build = is_debug && current_os != "android" &&\n'
     '                       current_os != "ios" && current_os != "watchos"\n',
     '  is_component_build = is_debug && current_os != "android" &&\n'
     '                       current_os != "ios" && current_os != "watchos"\n'
     '\n'
     '  # Export from a non-component build\'s shared libraries what a component build exports between its\n'
     '  # components, for embedders that link Chromium as one shared library (libchromium).\n'
     f'  {ARG} = false\n'),
    # A non-component build makes components static libraries, from which a link takes only the objects something
    # refers to, so an export nothing in the library calls would be left out. The SDK's library takes every
    # component whole, as a component build's libraries do. Only the target's own toolchain: tools the build
    # runs need no exports.
    ('build/config/BUILDCONFIG.gn',
     '  } else if (defined(invoker.static_component_type)) {\n',
     f'  }} else if ({ARG} && current_toolchain == default_toolchain) {{\n'
     '    _component_mode = "source_set"\n'
     '  } else if (defined(invoker.static_component_type)) {\n'),
    ('build/config/BUILD.gn',
     'config("feature_flags") {\n  defines = []\n  rustflags = []\n',
     'config("feature_flags") {\n  defines = []\n  rustflags = []\n'
     f'  if ({ARG}) {{\n    defines += [ "{DEFINE}" ]\n  }}\n'),
    ('build/config/compiler/BUILD.gn',
     '    if (!is_android || current_cpu != "arm") {\n      cflags += [ "-fwhole-program-vtables" ]\n',
     '    # An SDK\'s embedders implement interfaces outside the library, which whole-program vtable\n'
     '    # optimisation would assume it sees all of.\n'
     f'    if ((!is_android || current_cpu != "arm") && !{ARG}) {{\n'
     '      cflags += [ "-fwhole-program-vtables" ]\n'),
    ('build/config/gcc/BUILD.gn',
     'if (is_component_build && !is_android) {\n',
     f'if ((is_component_build || {ARG}) && !is_android) {{\n'),
    ('build/config/mac/BUILD.gn',
     '  ldflags = [ "-Wl,-ObjC" ]  # Always load Objective-C categories and classes.\n\n'
     '  if (is_component_build) {\n',
     '  ldflags = [ "-Wl,-ObjC" ]  # Always load Objective-C categories and classes.\n\n'
     f'  if (is_component_build || {ARG}) {{\n'),
    # A macOS build without V8's context snapshot, as a Linux machine makes one, fails to generate: Chrome's bundle
    # lists V8's own snapshot without depending on what makes it. Nothing of Chrome's is built.
    ('chrome/BUILD.gn',
     '      if (!use_v8_context_snapshot || include_both_v8_snapshots) {\n'
     '        sources += [ "$root_out_dir/snapshot_blob.bin" ]\n      }\n',
     '      if (!use_v8_context_snapshot || include_both_v8_snapshots) {\n'
     '        sources += [ "$root_out_dir/snapshot_blob.bin" ]\n'
     '        public_deps += [ "//v8:run_mksnapshot_default" ]\n      }\n'),
    ('v8/BUILD.gn',
     '  if (v8_monolithic && v8_monolithic_for_shared_library) {\n    defines += [ "V8_TLS_USED_IN_LIBRARY" ]\n',
     '  if ((v8_monolithic && v8_monolithic_for_shared_library) ||\n'
     f'      (defined({ARG}) && {ARG})) {{\n    defines += [ "V8_TLS_USED_IN_LIBRARY" ]\n'),
    # A non-component macOS build gives a shared library no install name, so what links libc++ would look for it
    # by its build path; a component build gives every library @rpath/<name>, as the SDK's own do.
    ('buildtools/third_party/libc++/BUILD.gn',
     '    # Use libc++_chrome to avoid conflicting with system libc++\n    output_name = "libc++_chrome"\n',
     '    # Use libc++_chrome to avoid conflicting with system libc++\n    output_name = "libc++_chrome"\n'
     f'    if (is_apple && {ARG}) {{\n'
     '      ldflags = [ "-Wl,-install_name,@rpath/libc++_chrome.dylib" ]\n    }\n'),
    # On Apple, a non-component build hides the allocator shim's operator new and delete, so an executable doesn't
    # export them; with libc++ a shared library, as in a component build, that contradicts libc++'s own
    # declarations, and the library is where an embedder's operator new must come from.
    ('base/allocator/partition_allocator/src/partition_alloc/BUILD.gn',
     '  config("mac_no_default_new_delete_symbols") {\n    if (!is_component_build) {\n',
     '  config("mac_no_default_new_delete_symbols") {\n'
     f'    if (!is_component_build && !(defined({ARG}) && {ARG})) {{\n'),
    ('v8/BUILD.gn',
     '  if (is_component_build) {\n    defines = [ "BUILDING_V8_BASE_SHARED" ]\n',
     f'  if (is_component_build || (defined({ARG}) && {ARG})) {{\n    defines = [ "BUILDING_V8_BASE_SHARED" ]\n'),
    ('v8/BUILD.gn',
     '  if (is_component_build) {\n    defines = [ "BUILDING_V8_PLATFORM_SHARED" ]\n',
     f'  if (is_component_build || (defined({ARG}) && {ARG})) {{\n'
     '    defines = [ "BUILDING_V8_PLATFORM_SHARED" ]\n'),
    ('skia/BUILD.gn',
     '  if (is_component_build) {\n    defines += [ "SKIA_DLL" ]\n  }\n',
     f'  if (is_component_build || {ARG}) {{\n    defines += [ "SKIA_DLL" ]\n  }}\n'),
    ('skia/BUILD.gn',
     'config("skcms_public_config") {\n  if (is_component_build) {\n',
     f'config("skcms_public_config") {{\n  if (is_component_build || {ARG}) {{\n'),
    ('third_party/abseil-cpp/absl.gni',
     '      if (is_component_build) {\n        defines += [ "ABSL_BUILD_DLL" ]\n',
     f'      if (is_component_build || {ARG}) {{\n        defines += [ "ABSL_BUILD_DLL" ]\n'),
    ('third_party/boringssl/BUILD.gn',
     '  include_dirs = [ "src/include" ]\n  if (is_component_build) {\n',
     f'  include_dirs = [ "src/include" ]\n  if (is_component_build || {ARG}) {{\n'),
    ('third_party/perfetto/gn/BUILD.gn',
     '  if (defined(is_component_build) && is_component_build) {\n    perfetto_component_build = true\n',
     '  if ((defined(is_component_build) && is_component_build) ||\n'
     f'      (defined({ARG}) && {ARG})) {{\n    perfetto_component_build = true\n'),
    # A mojom target compiles its generated C++ and the traits sources it names hidden, unless it names an
    # export macro, and a component build then compiles them into each component that uses them. The library
    # links each once, so with the argument it exports them, as default visibility, leaving inline functions
    # hidden as Chromium does: an embedder's own mojom that imports one, such as gfx.mojom.Rect, then links.
    # Blink's variant stays as it is, and so do traits a separate target compiles (specs/0001-sdk.md, Mojom).
    ('mojo/public/tools/bindings/mojom.gni',
     '  source_set(shared_cpp_sources_target_name) {\n'
     '    if (defined(invoker.testonly)) {\n'
     '      testonly = invoker.testonly\n'
     '    }\n'
     '    configs += [ "//build/config/compiler:wexit_time_destructors" ]\n',
     f'  if ({ARG}) {{\n'
     '    # Exported from the shared library the SDK links, so that an embedder\'s\n'
     '    # own mojoms can import this one (libchromium).\n'
     '    config("${target_name}__libchromium_exports") {\n'
     '      cflags = [ "-fvisibility=default" ]\n'
     '    }\n'
     '    _libchromium_exports_config = ":${target_name}__libchromium_exports"\n'
     '  }\n'
     '\n'
     '  source_set(shared_cpp_sources_target_name) {\n'
     '    if (defined(invoker.testonly)) {\n'
     '      testonly = invoker.testonly\n'
     '    }\n'
     '    configs += [ "//build/config/compiler:wexit_time_destructors" ]\n'
     f'    if ({ARG}) {{\n'
     '      configs += [ _libchromium_exports_config ]\n'
     '    }\n'),
    ('mojo/public/tools/bindings/mojom.gni',
     '        defines = export_defines\n'
     '        configs += [ "//build/config/compiler:wexit_time_destructors" ]\n'
     '        configs += extra_configs\n',
     '        defines = export_defines\n'
     '        configs += [ "//build/config/compiler:wexit_time_destructors" ]\n'
     '        configs += extra_configs\n'
     f'        if ({ARG} && !(defined(bindings_configuration.for_blink) &&\n'
     '                                    bindings_configuration.for_blink)) {\n'
     '          configs += [ _libchromium_exports_config ]\n'
     '        }\n'),
    # The library's export list names //components/embedder_support:user_agent, Chrome's user agent and its
    # client-hint brands, for embedders, but the target is a static library with no export macros: the link would
    # take only what the library itself calls, hidden. With the argument it's a source set, every object linked,
    # compiled with default visibility, as the mojom targets are.
    ('components/embedder_support/BUILD.gn',
     'static_library("user_agent") {\n'
     '  sources = [\n'
     '    "user_agent_utils.cc",\n'
     '    "user_agent_utils.h",\n'
     '  ]\n',
     f'if ({ARG}) {{\n'
     '  # Exported, every object of it, from the shared library the SDK links,\n'
     '  # whose export list names it (libchromium).\n'
     '  config("user_agent_libchromium_exports") {\n'
     '    cflags = [ "-fvisibility=default" ]\n'
     '  }\n'
     '  _user_agent_type = "source_set"\n'
     '} else {\n'
     '  _user_agent_type = "static_library"\n'
     '}\n'
     '\n'
     'target(_user_agent_type, "user_agent") {\n'
     '  sources = [\n'
     '    "user_agent_utils.cc",\n'
     '    "user_agent_utils.h",\n'
     '  ]\n'
     f'  if ({ARG}) {{\n'
     '    configs += [ ":user_agent_libchromium_exports" ]\n'
     '  }\n'),
    # //ui/events/blink, which turns UI events into Blink's, is in the library's export list too, a source set with
    # no export macros: with the argument it's compiled with default visibility.
    ('ui/events/blink/BUILD.gn',
     'source_set("blink") {\n'
     '  sources = [\n'
     '    "blink_event_util.cc",\n',
     f'if ({ARG}) {{\n'
     '  # Exported from the shared library the SDK links, whose export list names\n'
     '  # it (libchromium).\n'
     '  config("blink_libchromium_exports") {\n'
     '    cflags = [ "-fvisibility=default" ]\n'
     '  }\n'
     '}\n'
     '\n'
     'source_set("blink") {\n'
     f'  if ({ARG}) {{\n'
     '    configs += [ ":blink_libchromium_exports" ]\n'
     '  }\n'
     '  sources = [\n'
     '    "blink_event_util.cc",\n'),
    # Not part of the argument: macOS's linker driver writes a shared library's .TOC by appending to bytes, which
    # copies the whole TOC each time. For a library exporting 310,000 symbols that takes minutes on one core, long
    # after the link itself has finished. A list joined once writes the same TOC. Reported upstream as
    # https://issues.chromium.org/issues/587040685 and fixed by Chromium commit 394153f1299c
    # (refs/heads/main@{#1747697}). Drop these three edits when the pin includes that commit; on such a pin the
    # export stops, because the text they replace is gone.
    ('build/toolchain/apple/linker_driver.py',
     "        toc = b''\n        dylib = self._get_linker_output()\n",
     '        # A list joined once: appending to bytes copies the TOC each time,\n'
     '        # which takes minutes for a library exporting hundreds of thousands\n'
     '        # of symbols (https://issues.chromium.org/issues/587040685).\n'
     "        toc = []\n        dylib = self._get_linker_output()\n"),
    ('build/toolchain/apple/linker_driver.py',
     "                toc += line + b'\\n'\n"
     "                for j in range(5):\n"
     "                    toc += lines[i + 1 + j] + b'\\n'\n",
     "                toc.append(line + b'\\n')\n"
     "                for j in range(5):\n"
     "                    toc.append(lines[i + 1 + j] + b'\\n')\n"),
    ('build/toolchain/apple/linker_driver.py',
     "            toc += b' '.join(fields[1:3]) + b'\\n'\n        return toc\n",
     "            toc.append(b' '.join(fields[1:3]) + b'\\n')\n        return b''.join(toc)\n"),
)


def git(repo, *args, **kwargs):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, text=True,
                          stdout=subprocess.PIPE, **kwargs).stdout


def rewrite_header(text):
    """Returns the header with its COMPONENT_BUILD conditions also taking the SDK's define."""
    out = []
    continuing = False
    for line in text.splitlines(keepends=True):
        match = IFDEF.match(line)
        if match:
            negate = '!' if match.group(2) == 'ifndef' else ''
            line = f'{match.group(1)}if {negate}{REPLACEMENT}{line[match.end():]}'
        elif DIRECTIVE.match(line) or continuing:
            line = line.replace(TOKEN, REPLACEMENT).replace(PA_TOKEN, PA_REPLACEMENT)
        continuing = (DIRECTIVE.match(line) is not None or continuing) and line.rstrip('\n').endswith('\\')
        out.append(line)
    return ''.join(out)


def is_export_header(text):
    return 'COMPONENT_BUILD' in text and re.search(r'visibility\s*\(\s*"default"\s*\)', text) is not None


def apply_gn_edit(text, old, new, path):
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{path}: expected the edited text once, found it {count} times; update EDITS')
    return text.replace(old, new)


def repositories(src):
    """Each git repository in the checkout, as its path relative to src ('' for src itself)."""
    found = ['']
    for git_dir in sorted(src.glob('**/.git')):
        relative = git_dir.parent.relative_to(src).as_posix()
        if relative == '.' or relative.startswith('out/') or relative == 'out':
            continue
        found.append(relative)
    return found


def owner(path, repos):
    """The innermost repository holding a path relative to src."""
    best = ''
    for repo in repos:
        if repo and (path == repo or path.startswith(repo+'/')) and len(repo) > len(best):
            best = repo
    return best


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('src', type=Path, help="the checkout's src directory, clean and at the pin")
    args = parser.parse_args()
    src = args.src.resolve()
    repos = repositories(src)
    touched = {}
    for repo in repos:
        root = src/repo if repo else src
        if git(root, 'status', '--porcelain', '--untracked-files=no').strip():
            raise SystemExit(f'{root} has changes; run this on a clean checkout')
        for name in git(root, 'ls-files', '-z', '--', '*.h').split('\0'):
            if not name:
                continue
            relative = f'{repo}/{name}' if repo else name
            if SKIPPED.search(relative) or owner(relative, repos) != repo:
                continue
            path = src/relative
            if path.is_symlink() or not path.is_file():
                continue
            text = path.read_text(errors='surrogateescape')
            if not (is_export_header(text) or relative in ALSO):
                continue
            changed = rewrite_header(text)
            if changed != text:
                touched[relative] = (text, changed)
    for relative, old, new in EDITS:
        text = touched.get(relative, (None, None))[1] or (src/relative).read_text()
        original = touched.get(relative, ((src/relative).read_text(), None))[0]
        touched[relative] = (original, apply_gn_edit(text, old, new, relative))
    for relative in ALSO:
        if relative not in touched:
            raise SystemExit(f'{relative} no longer tests COMPONENT_BUILD; update ALSO')
    try:
        for relative, (_, new) in touched.items():
            (src/relative).write_text(new, errors='surrogateescape')
        for old in PATCHES.glob('**/*.patch'):
            old.unlink()
        by_repo = {}
        for relative in touched:
            by_repo.setdefault(owner(relative, repos), []).append(relative)
        for repo, files in sorted(by_repo.items()):
            root = src/repo if repo else src
            names = [Path(f).relative_to(repo).as_posix() if repo else f for f in sorted(files)]
            diff = git(root, 'diff', '--no-color', '--no-ext-diff', '--', *names)
            target = PATCHES/(f'{repo}.patch' if repo else 'src.patch')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(diff)
            print(f'{target.relative_to(REPO)}: {len(files)} files')
    finally:
        for relative, (old, _) in touched.items():
            (src/relative).write_text(old, errors='surrogateescape')
    return 0


if __name__ == '__main__':
    sys.exit(main())
