import json
import tempfile
import unittest
from pathlib import Path

from tests.scripts.common import REPO  # noqa: F401
import package

LINUX = {'target_os': 'linux', 'target_cpu': 'x64', 'sysroot': 'build/linux/debian_bullseye_amd64-sysroot'}
MAC = {'target_os': 'mac', 'target_cpu': 'arm64'}


class Flags(unittest.TestCase):
    def test_compile(self):
        mapping = package.path_mapping(LINUX)
        argv = ['../../third_party/llvm-build/Release+Asserts/bin/clang++', '-MMD', '-MF', 'obj/x.o.d',
                '-DLIBCHROMIUM_EXPORTS', '-D_LIBCPP_ABI_NAMESPACE=__Cr', '-I../..', '-Igen',
                '-I../../third_party/skia', '-Igen/third_party/perfetto', '-isystem',
                '../../third_party/libc++/src/include', '--sysroot=../../build/linux/debian_bullseye_amd64-sysroot',
                '-fno-exceptions', '-std=c++23', '-nostdinc++', '-O2', '-g1', '-Wall', '-Wno-foo', '-Werror',
                '-isystem../../third_party/libc++abi/src/include', '-fmodules', '-fno-implicit-module-maps',
                '-Xclang', '-fmodules-local-submodule-visibility', '-fmodule-map-file=gen/build/modules/module.modulemap',
                '-flto=thin', '-fprofile-use=../../chrome/build/pgo_profiles/x.profdata',
                '-Xclang', '-add-plugin', '-Xclang', 'find-bad-constructs',
                '-Xclang', '-plugin-arg-find-bad-constructs', '-Xclang', 'check-ipc',
                '-Xclang', '-fno-something-abi', '-mllvm', '-instcombine-lower-dbg-declare=0',
                '-Xclang', '-fmodule-file-home-is-cwd', '-Xclang', '-fmodules-cache-path=/not_exist_dummy_dir',
                '-fdebug-compilation-dir=.', '-c', '../../libchromium/embedder_probe.cc', '-o', 'obj/x.o']
        flags, defines, includes, system = package.compile_flags(argv, mapping)
        self.assertEqual(defines, ['LIBCHROMIUM_EXPORTS', '_LIBCPP_ABI_NAMESPACE=__Cr'])
        self.assertEqual(includes, ['{sdk}/include', '{sdk}/gen', '{sdk}/include/third_party/skia',
                                    '{sdk}/gen/third_party/perfetto'])
        self.assertEqual(system, ['{sdk}/include/third_party/libc++/src/include',
                                  '{sdk}/include/third_party/libc++abi/src/include'])
        self.assertEqual(flags, ['--sysroot={sdk}/sysroot', '-fno-exceptions', '-std=c++23', '-nostdinc++',
                                 '-Xclang', '-fno-something-abi'])

    def test_a_build_path_never_ships(self):
        mapping = package.path_mapping(LINUX)
        with self.assertRaises(package.PathLeak):
            package.compile_flags(['clang++', '-fsome-file=../../tools/x.txt'], mapping)
        with self.assertRaises(package.PathLeak):
            package.compile_flags(['clang++', '-I/home/builder/elsewhere'], mapping)
        with self.assertRaises(package.PathLeak):
            package.compile_flags(['clang++', '-Xclang', '-fsome-file=/home/builder/x'], mapping)

    def test_mac_sdk_is_the_embedders(self):
        mapping = package.path_mapping(MAC)
        sdk = ('../../build/mac_files/xcode_binaries/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/'
               'MacOSX26.2.sdk')
        flags, *_ = package.compile_flags(['clang++', '-isysroot', sdk, '-mmacos-version-min=12.0'], mapping)
        self.assertEqual(flags, ['-isysroot', '{apple_sdk}', '-mmacos-version-min=12.0'])

    def test_link(self):
        mapping = package.path_mapping(LINUX)
        variables = {
            'ldflags': ['-Wl,-mllvm,-enable-ext-tsp-block-placement=1', '-Werror', '-fPIC', '-flto=thin',
                        '-Wl,--thinlto-cache-dir=thinlto-cache', '-Wl,--icf=all', '--target=x86_64-unknown-linux-gnu',
                        '-Wl,-O2', '-Wl,--gc-sections', '-gsplit-dwarf', '-Wl,-z,defs', '-Wl,-z,now', '-nostdlib++',
                        '--sysroot=../../build/linux/debian_bullseye_amd64-sysroot',
                        '-L../../build/linux/debian_bullseye_amd64-sysroot/usr/lib/x86_64-linux-gnu',
                        '-Wl,-rpath=$ORIGIN', '-pie'],
            'libs': ['../../third_party/llvm-build/Release+Asserts/lib/clang/24/lib/x86_64-unknown-linux-gnu/'
                     'libclang_rt.builtins.a', '-ldl', '-lpthread'],
            'frameworks': [],
            'solibs': ['./libchromium.so', './libc++.so'],
        }
        flags, libraries, system, frameworks = package.link_flags(variables, mapping)
        self.assertEqual(flags, ['-fPIC', '--target=x86_64-unknown-linux-gnu', '-Wl,--gc-sections', '-Wl,-z,defs',
                                 '-Wl,-z,now', '-nostdlib++', '--sysroot={sdk}/sysroot',
                                 '-L{sdk}/sysroot/usr/lib/x86_64-linux-gnu', '-pie'])
        self.assertEqual(libraries, ['{sdk}/toolchain/lib/clang/24/lib/x86_64-unknown-linux-gnu/libclang_rt.builtins.a',
                                     '{sdk}/lib/libchromium.so', '{sdk}/lib/libc++.so'])
        self.assertEqual(system, ['dl', 'pthread'])
        self.assertEqual(frameworks, [])

    def test_mac_link(self):
        mapping = package.path_mapping(MAC)
        variables = {
            'ldflags': ['-Wl,--no-warn-duplicate-rpath', '-Wcrl,object_path_lto',
                        '-Wcrl,unstripped,/home/builder/checkout/src/out/macos-arm64', '--target=arm64-apple-macos',
                        '-Wl,-ObjC', '-isysroot', '../../build/mac_files/xcode_binaries/Contents/Developer/Platforms/'
                        'MacOSX.platform/Developer/SDKs/MacOSX26.2.sdk', '-mmacos-version-min=12.0'],
            'libs': ['../../third_party/llvm-build/Release+Asserts/lib/clang/24/lib/darwin/libclang_rt.osx.a'],
            'frameworks': ['-framework', 'AppKit', '-weak_framework', 'Metal'],
            'solibs': ['./libchromium.dylib', './libc++_chrome.dylib'],
        }
        flags, libraries, system, frameworks = package.link_flags(variables, mapping)
        self.assertEqual(flags, ['-Wl,--no-warn-duplicate-rpath', '--target=arm64-apple-macos', '-Wl,-ObjC',
                                 '-isysroot', '{apple_sdk}', '-mmacos-version-min=12.0'])
        self.assertEqual(frameworks, ['AppKit', 'Metal'])
        self.assertEqual(libraries[1:], ['{sdk}/lib/libchromium.dylib', '{sdk}/lib/libc++_chrome.dylib'])

    def test_a_link_path_never_ships(self):
        with self.assertRaises(package.PathLeak):
            package.link_flags({'ldflags': ['-Wl,--version-script=/home/builder/x.map']}, package.path_mapping(LINUX))
        with self.assertRaises(package.PathLeak):
            package.link_flags({'solibs': ['obj/libx.so']}, package.path_mapping(LINUX))

    def test_ninja_variables(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            ninja = Path(tmp)/'probe.ninja'
            ninja.write_text('defines = -DA -DB=\\"x\\"\ninclude_dirs = -I../.. -Igen\n'
                             'cflags_cc = -std=c++23 -isystem../../third_party/libc++/src/include\n'
                             'build obj/a.o: cxx ../../a.cc\n  source_file_part = a.cc\n'
                             'build ./probe: link obj/a.o\n  ldflags = -Wl,--thinlto-cache-policy=cache_size=10\\%$:x\n'
                             '  libs = -ldl\n  solibs = ./libchromium.so\n')
            variables = package.target_variables(ninja)
        self.assertEqual(variables['defines'], ['-DA', '-DB="x"'])
        self.assertEqual(variables['ldflags'], ['-Wl,--thinlto-cache-policy=cache_size=10%:x'])
        self.assertEqual(variables['solibs'], ['./libchromium.so'])
        argv = package.compile_argv(variables, 'c++')
        self.assertEqual(argv, ['clang++', '-DA', '-DB="x"', '-I../..', '-Igen', '-std=c++23',
                                '-isystem../../third_party/libc++/src/include'])

    def test_runtime_files(self):
        for name in ('./libffmpeg.so', './libEGL.so', './libGLESv2.dylib', './libvk_swiftshader.so',
                     './libvulkan.so.1', './libvulkan.dylib', './vk_swiftshader_icd.json'):
            self.assertTrue(package.RUNTIME.match(name), name)
        for name in ('./libVkICD_mock_icd.so', './libVkLayer_khronos_validation.so', 'angledata/VkICD_mock_icd.json',
                     'pyproto/google/protobuf/any.py', './libchromium.so'):
            self.assertFalse(package.RUNTIME.match(name), name)

    def test_shipped_runtime_listing(self):
        listing = ('./libchromium.so\nicudtl.dat\n./libEGL.so\n./libc++.so\nvk_swiftshader_icd.json\n'
                   './libvk_swiftshader.so\n./libvulkan.so.1\n./libVkICD_mock_icd.so\nangledata/VkICD_mock_icd.json\n'
                   './libffmpeg.so\nsnapshot_blob.bin\npyproto/google/protobuf/any.py\n')
        self.assertEqual(package.shipped_runtime(listing),
                         ['libEGL.so', 'libffmpeg.so', 'libvk_swiftshader.so', 'libvulkan.so.1',
                          'vk_swiftshader_icd.json'])

    def test_rewrite_path(self):
        mapping = package.path_mapping(LINUX)
        self.assertEqual(package.rewrite_path('../../base/foo.h', mapping), '{sdk}/include/base/foo.h')
        self.assertEqual(package.rewrite_path('gen/base/foo.h', mapping), '{sdk}/gen/base/foo.h')
        self.assertIsNone(package.rewrite_path('/usr/include/stdio.h', mapping))


class Mojom(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.src, self.out = self.tmp/'src', self.tmp/'src/out/x'
        for path in ('ui/gfx/geometry/mojom/geometry.mojom', 'mojo/public/mojom/base/time.mojom',
                     'content/common/hidden.mojom'):
            (self.src/path).parent.mkdir(parents=True, exist_ok=True)
            (self.src/path).write_text('module x;')
        for path in ('ui/gfx/geometry/mojom/geometry.mojom-module', 'mojo/public/mojom/base/time.mojom-module',
                     'content/common/hidden.mojom-module', 'mojo/public/mojom/base/time.mojom.cc',
                     'mojo/public/mojom/base/time.mojom-shared.cc', 'mojo/public/mojom/base/time.mojom.h'):
            (self.out/'gen'/path).parent.mkdir(parents=True, exist_ok=True)
            (self.out/'gen'/path).write_text('')

    def test_modules_and_sources_of_shipped_bindings_only(self):
        headers = {'gen/ui/gfx/geometry/mojom/geometry.mojom.h': None, 'gen/mojo/public/mojom/base/time.mojom.h': None,
                   'gen/ui/gfx/geometry/mojom/geometry.mojom-shared.h': None, 'include/base/time/time.h': None}
        files = package.mojom_files(headers, self.src, self.out)
        self.assertEqual(sorted(files), [
            'gen/mojo/public/mojom/base/time.mojom-module', 'gen/ui/gfx/geometry/mojom/geometry.mojom-module',
            'include/mojo/public/mojom/base/time.mojom', 'include/ui/gfx/geometry/mojom/geometry.mojom'])
        self.assertEqual(files['include/ui/gfx/geometry/mojom/geometry.mojom'],
                         self.src/'ui/gfx/geometry/mojom/geometry.mojom')

    def test_a_generated_mojom_ships_under_gen(self):
        for path in ('third_party/blink/public/mojom/origin_trial_feature.mojom',
                     'third_party/blink/public/mojom/origin_trial_feature.mojom-module'):
            (self.out/'gen'/path).parent.mkdir(parents=True, exist_ok=True)
            (self.out/'gen'/path).write_text('')
        files = package.mojom_files({'gen/third_party/blink/public/mojom/origin_trial_feature.mojom.h': None},
                                    self.src, self.out)
        self.assertEqual(sorted(files), ['gen/third_party/blink/public/mojom/origin_trial_feature.mojom',
                                         'gen/third_party/blink/public/mojom/origin_trial_feature.mojom-module'])

    def test_a_mojom_whose_bindings_ship_without_its_source_or_module_stops_the_packaging(self):
        (self.src/'content/common/hidden.mojom').unlink()
        with self.assertRaisesRegex(SystemExit, 'content/common/hidden.mojom'):
            package.mojom_files({'gen/content/common/hidden.mojom.h': None}, self.src, self.out)
        with self.assertRaisesRegex(SystemExit, 'content/common/absent.mojom'):
            package.mojom_files({'gen/content/common/absent.mojom.h': None}, self.src, self.out)

    def test_the_mappings_headers_are_roots(self):
        (self.src/'media/mojo/mojom').mkdir(parents=True)
        (self.src/'media/mojo/mojom/video_decoder_config_mojom_traits.h').write_text('')
        (self.out/'gen/media/base').mkdir(parents=True)
        (self.out/'gen/media/base/generated.h').write_text('')
        mappings = {'c++': {'media.mojom.VideoDecoderConfig': {
            'public_headers': ['media/base/generated.h'],
            'traits_headers': ['media/mojo/mojom/video_decoder_config_mojom_traits.h']}}}
        roots = package.mapping_headers(mappings, self.src, self.out)
        self.assertEqual(roots, ['../../media/mojo/mojom/video_decoder_config_mojom_traits.h',
                                 'gen/media/base/generated.h'])
        shipped = {'include/media/mojo/mojom/video_decoder_config_mojom_traits.h': None}
        self.assertEqual(package.unshipped_mapping_headers(roots, shipped), ['gen/media/base/generated.h'])
        shipped['gen/media/base/generated.h'] = None
        self.assertEqual(package.unshipped_mapping_headers(roots, shipped), [])

    def test_unusable_mappings_are_dropped_and_usable_ones_stop_the_packaging(self):
        action = {'public_headers': [], 'traits_headers': ['components/x/traits.h']}
        string16 = {'public_headers': ['base/strings/string16.h'], 'traits_headers': []}
        mappings = {'c++': {'omnibox.mojom.Action': action, 'mojo_base.mojom.String16': string16}}
        dropped = package.drop_unusable_mappings(mappings, ['../../components/x/traits.h'], {'mojo_base.mojom'})
        self.assertEqual(dropped, ['omnibox.mojom.Action'])
        self.assertEqual(list(mappings['c++']), ['mojo_base.mojom.String16'])
        mappings['c++']['omnibox.mojom.Action'] = action
        with self.assertRaisesRegex(SystemExit, 'omnibox.mojom.Action is mapped'):
            package.drop_unusable_mappings(mappings, ['../../components/x/traits.h'], {'omnibox.mojom'})
        with self.assertRaisesRegex(SystemExit, 'omnibox.mojom.Action is mapped'):
            package.drop_unusable_mappings(mappings, ['../../components/x/traits.h'], {'mojo_base.mojom', ''})

    def test_a_mapping_header_that_is_nowhere_stops_the_packaging(self):
        mappings = {'c++': {'x.mojom.Y': {'public_headers': ['media/base/absent.h'], 'traits_headers': []}}}
        with self.assertRaisesRegex(SystemExit, 'media/base/absent.h'):
            package.mapping_headers(mappings, self.src, self.out)

    def test_mojo_base_generated_sources_are_roots(self):
        self.assertEqual(package.mojom_sources(self.out), ['gen/mojo/public/mojom/base/time.mojom-shared.cc',
                                                           'gen/mojo/public/mojom/base/time.mojom.cc'])

    def test_companions_of_shipped_bindings_that_the_build_has(self):
        for suffix in ('-forward.h', '-import-headers.h', '-shared.h', '-test-utils.h', '-blink.h'):
            (self.out/f'gen/mojo/public/mojom/base/time.mojom{suffix}').write_text('')
        shipped = {'gen/mojo/public/mojom/base/time.mojom.h': None, 'gen/mojo/public/mojom/base/time.mojom-shared.h': None}
        self.assertEqual(package.mojom_companions(shipped, self.out),
                         ['gen/mojo/public/mojom/base/time.mojom-forward.h',
                          'gen/mojo/public/mojom/base/time.mojom-import-headers.h'])
        self.assertEqual(package.companion_mojom('gen/mojo/public/mojom/base/time.mojom-import-headers.h'),
                         'mojo/public/mojom/base/time.mojom')

    def test_a_mojom_missing_a_companion_is_not_importable(self):
        shipped = {'gen/mojo/public/mojom/base/time.mojom.h': None}
        for suffix in package.MOJOM_COMPANIONS:
            (self.out/f'gen/mojo/public/mojom/base/time.mojom{suffix}').write_text('')
        self.assertEqual(package.absent_companions(shipped, self.out), set())
        (self.out/'gen/mojo/public/mojom/base/time.mojom-import-headers.h').unlink()
        self.assertEqual(package.absent_companions(shipped, self.out), {'mojo/public/mojom/base/time.mojom'})
        shipped['gen/mojo/public/mojom/base/time.mojom-import-headers.h'] = None
        self.assertEqual(package.absent_companions(shipped, self.out), set())

    def test_a_mojom_with_a_companion_that_does_not_ship_is_not_importable(self):
        headers = {'gen/ui/gfx/geometry/mojom/geometry.mojom.h': None, 'gen/mojo/public/mojom/base/time.mojom.h': None}
        files = package.mojom_files(headers, self.src, self.out, incomplete={'ui/gfx/geometry/mojom/geometry.mojom'})
        self.assertEqual(sorted(files), ['gen/mojo/public/mojom/base/time.mojom-module',
                                         'include/mojo/public/mojom/base/time.mojom'])

    def test_no_mojo_base_generated_sources_stops_the_packaging(self):
        for path in (self.out/'gen/mojo/public/mojom/base').glob('*.cc'):
            path.unlink()
        with self.assertRaisesRegex(SystemExit, 'has Chromium moved'):
            package.mojom_sources(self.out)

    def write_mappings(self, label, mappings):
        directory, _, name = label.partition(':')
        path = self.out/'gen'/directory[2:]/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'c++': mappings}))

    def test_type_mappings_merge_without_blink_or_unlinked_targets(self):
        rect = {'typename': '::gfx::Rect', 'public_headers': ['ui/gfx/geometry/mojom/geometry_mojom_traits.h']}
        self.write_mappings('//ui/gfx/geometry/mojom:mojom__type_mappings', {'gfx.mojom.Rect': rect})
        self.write_mappings('//ui/gfx/geometry/mojom:mojom_blink__type_mappings',
                            {'gfx.mojom.Rect': {'typename': '::blink::Rect'}})
        self.write_mappings('//mojo/public/mojom/base:base__type_mappings',
                            {'gfx.mojom.Rect': rect, 'mojo_base.mojom.String16': {'typename': '::std::u16string'}})
        self.write_mappings('//chrome/common:mojom__type_mappings', {'chrome.mojom.X': {'typename': '::X'}})
        linked = {'//ui/gfx/geometry/mojom:mojom__type_mappings', '//ui/gfx/geometry/mojom:mojom_blink__type_mappings',
                  '//mojo/public/mojom/base:base__type_mappings', '//ui/gfx/geometry/mojom:mojom'}
        self.assertEqual(package.type_mappings(linked, self.out), {'c++': {
            'gfx.mojom.Rect': rect, 'mojo_base.mojom.String16': {'typename': '::std::u16string'}}})

    def test_a_type_mapped_two_ways_stops_the_packaging(self):
        self.write_mappings('//a:mojom__type_mappings', {'gfx.mojom.Rect': {'typename': '::gfx::Rect'}})
        self.write_mappings('//b:mojom__type_mappings', {'gfx.mojom.Rect': {'typename': '::other::Rect'}})
        with self.assertRaisesRegex(SystemExit, 'gfx.mojom.Rect is mapped to C\\+\\+ two ways'):
            package.type_mappings({'//a:mojom__type_mappings', '//b:mojom__type_mappings'}, self.out)


if __name__ == '__main__':
    unittest.main()
