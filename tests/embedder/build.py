#!/usr/bin/env python3
"""Builds the test embedder against an extracted SDK, outside Chromium's tree, with nothing but the SDK's own
toolchain and the flags in its sdk.json, as any build tool could (specs/0001-sdk.md, Compiling and linking), and
generates its own mojom's bindings with the SDK's mojom tooling (specs/0001-sdk.md, Mojom):

    build.py --sdk <extracted SDK> --output <directory> [--apple-sdk <MacOSX.sdk>]

The output directory holds the executable with the SDK's runtime libraries and resources beside it, ready to run
(run.py) or to copy to a machine that tests the target.
"""
import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
EXECUTABLE = 'libchromium-test-embedder'
# The embedder's own mojoms, relative to the repository's root, from which they import as Chromium's do from its.
MOJOMS = ('tests/embedder/probe.mojom',)


def expand(value, sdk, apple_sdk):
    value = value.replace('{sdk}', str(sdk))
    if '{apple_sdk}' in value:
        if not apple_sdk:
            raise SystemExit('this SDK needs an Apple SDK: pass --apple-sdk')
        value = value.replace('{apple_sdk}', str(apple_sdk))
    return value


def generate_bindings(description, sdk, output):
    """Generates MOJOMS' C++ bindings under <output>/gen as Chromium's build would, and returns their sources: parse
    each into a module, against the SDK's modules of what it imports; precompile the generator's templates; then
    generate the shared code, the message IDs and the bindings themselves, with the SDK's type mappings."""
    mojom = {key: [expand(item, sdk, None) for item in value] if isinstance(value, list) else expand(value, sdk, None)
             for key, value in description['mojom'].items() if key != 'features'}
    features = description['mojom']['features']
    gen, templates = output/'gen', output/'mojom-templates'
    templates.mkdir(parents=True)
    # -B: nothing written beside the SDK's generator.
    python = [sys.executable, '-B']
    commands = [
        python + [mojom['parser'], '--input-root', str(REPO)] +
        [part for root in mojom['imports'] for part in ('--input-root', root)] +
        ['--module-root', mojom['modules'], '--output-root', str(gen), '--mojoms', *(str(REPO/name) for name in MOJOMS)] +
        [part for feature in features for part in ('--enable-feature', feature)],
        python + [mojom['generator'], '-o', str(templates), '--use_bundled_pylibs', 'precompile', '-g', 'c++'],
    ]
    generate = python + [mojom['generator'], '--use_bundled_pylibs', '-o', str(gen), 'generate', '-d', str(REPO),
                         '-I', str(REPO), '--bytecode_path', str(templates), '-g', 'c++']
    mojoms = [str(REPO/name) for name in MOJOMS]
    for extra in (['--generate_non_variant_code'], ['--generate_non_variant_code', '--generate_message_ids'],
                  ['--typemap', mojom['type_mappings']]):
        commands.append(generate + extra + mojoms)
    for command in commands:
        subprocess.run(command, check=True)
    return [gen/(name+suffix) for name in MOJOMS for suffix in ('.cc', '-shared.cc')]


def compile_command(description, language, source, output, sdk, apple_sdk, gen):
    flags = [expand(flag, sdk, apple_sdk) for flag in description['compile'][language]]
    defines = ['-D'+define for define in description['defines']]
    includes = ['-I'+expand(path, sdk, apple_sdk) for path in description['include_dirs']]
    system = [part for path in description['system_include_dirs'] for part in ('-isystem', expand(path, sdk,
                                                                                                  apple_sdk))]
    # The embedder's own headers, and its generated bindings', found from the repository's root and the output's
    # gen as Chromium's are from its.
    return ([expand(description['toolchain']['cxx'], sdk, apple_sdk), '-x', language] + flags + defines +
            ['-I'+str(REPO), '-I'+str(gen)] + includes + system +
            ['-O2', '-c', str(source), '-o', str(output)])


def link_command(description, objects, output, sdk, apple_sdk):
    link = description['link']
    mac = description['system'] == 'mac'
    rpath = '-Wl,-rpath,@loader_path' if mac else '-Wl,-rpath,$ORIGIN'
    # The embedder's own Objective-C uses AppKit, which a macOS link must name.
    own = ['-framework', 'AppKit', '-framework', 'Foundation'] if mac else []
    return ([expand(description['toolchain']['cxx'], sdk, apple_sdk)] +
            [expand(flag, sdk, apple_sdk) for flag in link['flags']] + [str(o) for o in objects] +
            [expand(library, sdk, apple_sdk) for library in link['libraries']] +
            ['-l'+name for name in link['system_libs']] +
            [part for name in link['frameworks'] for part in ('-framework', name)] + own +
            [rpath, '-o', str(output)])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--sdk', required=True, type=Path, help='the directory the SDK parts are extracted to')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--apple-sdk', type=Path, help="macOS: the Apple SDK to build with, the embedder's own")
    args = parser.parse_args()
    sdk = args.sdk.resolve()
    description = json.loads((sdk/'sdk.json').read_text())
    mac = description['system'] == 'mac'
    output = args.output.resolve()
    if output.exists():
        shutil.rmtree(output)
    objects_dir = output/'obj'
    objects_dir.mkdir(parents=True)
    sources = [(source, 'c++') for source in generate_bindings(description, sdk, output)]
    sources.append((HERE/'embedder.cc', 'c++'))
    sources.append((HERE/'platform_mac.mm', 'objective-c++') if mac else (HERE/'platform_aura.cc', 'c++'))
    commands = []
    objects = []
    for source, language in sources:
        obj = objects_dir/(source.name+'.o')
        objects.append(obj)
        commands.append(compile_command(description, language, source, obj, sdk, args.apple_sdk, output/'gen'))
    with concurrent.futures.ThreadPoolExecutor() as pool:
        results = list(pool.map(lambda command: subprocess.run(command), commands))
    for command, result in zip(commands, results):
        if result.returncode:
            print('failed:', ' '.join(command), file=sys.stderr)
            return 1
    executable = output/EXECUTABLE
    subprocess.run(link_command(description, objects, executable, sdk, args.apple_sdk), check=True)
    # The runtime files beside the executable, where Chromium looks for them by default.
    for library in description['runtime']['libraries']:
        shutil.copy2(sdk/library, output/Path(library).name)
    for resource in description['runtime']['resources']:
        relative = Path(resource).relative_to('resources')
        (output/relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sdk/resource, output/relative)
    for directory in (objects_dir, output/'gen', output/'mojom-templates'):
        shutil.rmtree(directory)
    print(f'built {executable}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
