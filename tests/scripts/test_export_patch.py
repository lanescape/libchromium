import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.scripts.common import REPO  # noqa: F401
import export_patch


class RewriteHeader(unittest.TestCase):
    def test_export_header(self):
        text = ('#if defined(COMPONENT_BUILD)\n#if defined(FOO_IMPLEMENTATION)\n'
                '#define FOO_EXPORT __attribute__((visibility("default")))\n#endif\n#else  // defined(COMPONENT_BUILD)\n'
                '#define FOO_EXPORT\n#endif  // defined(COMPONENT_BUILD)\n')
        out = export_patch.rewrite_header(text)
        self.assertTrue(out.startswith('#if (defined(COMPONENT_BUILD) || defined(LIBCHROMIUM_EXPORTS))\n'))
        # Comments after #else and #endif stay as they were.
        self.assertIn('#else  // defined(COMPONENT_BUILD)\n', out)
        self.assertIn('#endif  // defined(COMPONENT_BUILD)\n', out)

    def test_negated_and_elif(self):
        self.assertEqual(export_patch.rewrite_header('#if !defined(COMPONENT_BUILD)\n'),
                         '#if !(defined(COMPONENT_BUILD) || defined(LIBCHROMIUM_EXPORTS))\n')
        self.assertEqual(export_patch.rewrite_header('#elif defined(COMPONENT_BUILD) && defined(WIN32)\n'),
                         '#elif (defined(COMPONENT_BUILD) || defined(LIBCHROMIUM_EXPORTS)) && defined(WIN32)\n')

    def test_partition_alloc(self):
        self.assertEqual(export_patch.rewrite_header('#if PA_BUILDFLAG(IS_COMPONENT_BUILD)\n'),
                         '#if (PA_BUILDFLAG(IS_COMPONENT_BUILD) || defined(LIBCHROMIUM_EXPORTS))\n')
        self.assertEqual(export_patch.rewrite_header('#endif  // PA_BUILDFLAG(IS_COMPONENT_BUILD)\n'),
                         '#endif  // PA_BUILDFLAG(IS_COMPONENT_BUILD)\n')

    def test_ifdef(self):
        self.assertEqual(export_patch.rewrite_header('#ifdef COMPONENT_BUILD\n'),
                         '#if (defined(COMPONENT_BUILD) || defined(LIBCHROMIUM_EXPORTS))\n')
        self.assertEqual(export_patch.rewrite_header('#ifndef COMPONENT_BUILD // x\n'),
                         '#if !(defined(COMPONENT_BUILD) || defined(LIBCHROMIUM_EXPORTS)) // x\n')

    def test_continued_condition(self):
        text = '#if defined(A) && \\\n    !defined(COMPONENT_BUILD) && \\\n    defined(B)\nint defined(COMPONENT_BUILD);\n'
        out = export_patch.rewrite_header(text)
        self.assertIn('    !(defined(COMPONENT_BUILD) || defined(LIBCHROMIUM_EXPORTS)) && \\\n', out)
        # Code, not a condition, is left alone.
        self.assertTrue(out.endswith('int defined(COMPONENT_BUILD);\n'))

    def test_export_header_detection(self):
        self.assertTrue(export_patch.is_export_header('#if defined(COMPONENT_BUILD)\n__attribute__((visibility("default")))'))
        self.assertFalse(export_patch.is_export_header('#if defined(COMPONENT_BUILD)\nint x;'))
        self.assertFalse(export_patch.is_export_header('__attribute__((visibility("default")))'))

    def test_skipped_trees(self):
        for path in ('chrome/common/chrome_export.h', 'base/test/test_export.h', 'ui/base/foo_win.h',
                     'third_party/pdfium/public/fpdfview.h'):
            self.assertTrue(export_patch.SKIPPED.search(path), path)
        for path in ('content/common/content_export.h', 'base/base_export.h', 'ui/gfx/gfx_export.h'):
            self.assertFalse(export_patch.SKIPPED.search(path), path)

    def test_gn_edit_must_match_once(self):
        self.assertEqual(export_patch.apply_gn_edit('a b c', 'b', 'x', 'f'), 'a x c')
        with self.assertRaises(SystemExit):
            export_patch.apply_gn_edit('a b b', 'b', 'x', 'f')
        with self.assertRaises(SystemExit):
            export_patch.apply_gn_edit('a c', 'b', 'x', 'f')

    def test_owner(self):
        repos = ['', 'v8', 'third_party/perfetto']
        self.assertEqual(export_patch.owner('v8/include/v8config.h', repos), 'v8')
        self.assertEqual(export_patch.owner('third_party/perfetto/gn/BUILD.gn', repos), 'third_party/perfetto')
        self.assertEqual(export_patch.owner('v8x/foo.h', repos), '')


# GN files whose edits each add only what the SDK's switch guards, and GN syntax around it.
GUARDED = ('mojo/public/tools/bindings/mojom.gni', 'components/embedder_support/BUILD.gn',
           'ui/events/blink/BUILD.gn')
# The one file the patches change for a reason other than the SDK's switch.
TOC_FIX = 'build/toolchain/apple/linker_driver.py'


class CarriedPatches(unittest.TestCase):
    def test_every_edit_is_carried(self):
        """The checked-in patches are the generator's output: every line each edit adds is in its patch."""
        repos = ('v8', 'third_party/perfetto')
        for path, old, new in export_patch.EDITS:
            repo = next((r for r in repos if path.startswith(r+'/')), '')
            patch = (REPO/'chromium/patches'/(f'{repo}.patch' if repo else 'src.patch')).read_text()
            relative = path[len(repo)+1:] if repo else path
            self.assertIn(f'+++ b/{relative}', patch, path)
            for line in set(new.splitlines()) - set(old.splitlines()):
                self.assertIn('+'+line, patch.splitlines(), f'{path}: {line}')

    def test_patches_only_add_the_sdk_switch(self):
        """Every line the carried patches add mentions the SDK's switch, or is a comment or GN syntax around it,
        or is in the linker driver's TOC fix, the one change that isn't the switch's, or in an edit of GUARDED, each
        of which branches on the switch."""
        fix = {line for path, old, new in export_patch.EDITS if path == TOC_FIX
               for line in set(new.splitlines()) - set(old.splitlines())}
        self.assertTrue(fix)
        guarded_edits = [(path, old, new) for path, old, new in export_patch.EDITS if path in GUARDED]
        self.assertEqual({path for path, _, _ in guarded_edits}, set(GUARDED))
        for path, old, new in guarded_edits:
            self.assertIn(f'if ({export_patch.ARG}', new)
            if path == GUARDED[0]:
                # The mojom template's edits keep all of its own text, and only add blocks the switch guards.
                self.assertIn(old, new)
            else:
                # The other two keep every line they replace but the target's declaration, in order.
                kept = [line for line in old.splitlines() if not line.startswith(('static_library(', 'source_set('))]
                remaining = iter(new.splitlines())
                self.assertTrue(all(line in remaining for line in kept), path)
                self.assertIn('"user_agent")' if 'embedder_support' in path else 'source_set("blink")', new)
        guarded = {path: {line for edit_path, _, new in guarded_edits if edit_path == path for line in new.splitlines()}
                   for path in GUARDED}
        for patch in sorted((REPO/'chromium/patches').glob('**/*.patch')):
            path = None
            for line in patch.read_text().splitlines():
                if line.startswith('+++ b/'):
                    path = line[len('+++ b/'):]
                if not line.startswith('+') or line.startswith('+++'):
                    continue
                if path == TOC_FIX:
                    self.assertIn(line[1:], fix, f'{patch.name}: {line}')
                    continue
                if path in GUARDED:
                    self.assertIn(line[1:], guarded[path], f'{patch.name}: {line}')
                    continue
                body = line[1:].strip()
                if not body or body.startswith(('#', '//')) and 'COMPONENT_BUILD' not in body:
                    continue
                if body in ('}', 'defines += [ "LIBCHROMIUM_EXPORTS" ]', 'libchromium_exports = false',
                            'cflags += [ "-fwhole-program-vtables" ]', 'defines += [ "SKIA_DLL" ]',
                            'defines += [ "ABSL_BUILD_DLL" ]', 'perfetto_component_build = true',
                            'if ((defined(is_component_build) && is_component_build) ||',
                            'public_deps += [ "//v8:run_mksnapshot_default" ]',
                            'defines += [ "V8_TLS_USED_IN_LIBRARY" ]',
                            'if ((v8_monolithic && v8_monolithic_for_shared_library) ||',
                            'ldflags = [ "-Wl,-install_name,@rpath/libc++_chrome.dylib" ]',
                            '_component_mode = "source_set"'):
                    continue
                if body.startswith('if (!is_component_build && !(defined(libchromium_exports)'):
                    continue
                self.assertTrue('LIBCHROMIUM_EXPORTS' in body or 'libchromium_exports' in body,
                                f'{patch.name}: {line}')



class TocFix(unittest.TestCase):
    """The linker driver's TOC fix, run: the pin's _extract_toc (tests/scripts/fixtures), with EDITS applied in
    order, against fixed otool and nm output."""

    OTOOL = (b'Load command 3\n      cmd LC_ID_DYLIB\n  cmdsize 56\n     name @rpath/libx.dylib (offset 24)\n'
             b'   time stamp 1 Thu Jan  1 00:00:01 1970\n      current version 0.0.0\n'
             b'compatibility version 0.0.0\nLoad command 4\n      cmd LC_LOAD_DYLIB\n')
    NM = (b'0000000000001000 T __ZN4base3FooEv\n0000000000002000 D _kTable\nshort line\n'
          b'0000000000003000 T __ZN4base3BarEi\n')
    EXPECTED = (b'      cmd LC_ID_DYLIB\n  cmdsize 56\n     name @rpath/libx.dylib (offset 24)\n'
                b'   time stamp 1 Thu Jan  1 00:00:01 1970\n      current version 0.0.0\n'
                b'compatibility version 0.0.0\n'
                b'T __ZN4base3FooEv\nD _kTable\nT __ZN4base3BarEi\n')

    def sources(self):
        before = (REPO/'tests/scripts/fixtures/linker_driver_extract_toc.py').read_text()
        after = before
        for path, old, new in export_patch.EDITS:
            if path == TOC_FIX:
                after = export_patch.apply_gn_edit(after, old, new, path)
        return before, after

    def toc(self, source, otool=OTOOL, nm=NM):
        namespace = {}
        exec(compile(source, TOC_FIX, 'exec'), namespace)
        driver = namespace['LinkerDriver'].__new__(namespace['LinkerDriver'])
        driver._otool_cmd, driver._nm_cmd = ['otool'], ['nm']
        driver._get_linker_output = lambda: 'libx.dylib'
        answers = {'otool': otool, 'nm': nm}
        with mock.patch.object(subprocess, 'check_output', side_effect=lambda cmd: answers[cmd[0]]):
            return driver._extract_toc()

    def test_the_edits_change_the_code_not_the_toc(self):
        before, after = self.sources()
        self.assertNotEqual(before, after)
        self.assertNotIn('toc +=', after)
        self.assertEqual(self.toc(before), self.EXPECTED)
        self.assertEqual(self.toc(after), self.EXPECTED)
        # No LC_ID_DYLIB, and no symbols.
        self.assertEqual(self.toc(after, otool=b'Load command 0\n', nm=b''), self.toc(before, otool=b'Load command 0\n', nm=b''))
        with self.assertRaises(ValueError):
            self.toc(after, otool=self.OTOOL + self.OTOOL)

    def test_the_patch_is_the_edits(self):
        """git applies the carried patch's linker driver hunks to the pin's code, which leaves the edited code."""
        before, after = self.sources()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/TOC_FIX
            target.parent.mkdir(parents=True)
            target.write_text(before)
            target.chmod(0o755)  # as the pin's file is
            # Outside any repository, git apply patches the files under its working directory.
            subprocess.run(['git', 'apply', f'--include={TOC_FIX}', str(REPO/'chromium/patches/src.patch')],
                           cwd=tmp, check=True, env={**os.environ, 'GIT_CEILING_DIRECTORIES': tmp})
            self.assertEqual(target.read_text(), after)

if __name__ == '__main__':
    unittest.main()
