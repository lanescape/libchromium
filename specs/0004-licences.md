# Licences

This repository's own files are under the MIT License (`LICENSE`). What it publishes is Chromium's, under Chromium's licences, and a version that can't meet every obligation below isn't published: a failure in any step that makes notices or source fails CD before it publishes.

## Notices

Every target's `notices/` directory, in the `lib` part and as the `notices` part, holds:

- `LICENSE.chromium`, Chromium's licence;
- `CREDITS.html`, the licences of every third-party component the SDK's targets link or ship (but for the mojom tools, below), made by Chromium's `tools/licenses/licenses.py credits` over `//libchromium:libchromium`;
- `LICENSE.libchromium`, this repository's licence;
- `components.json`, each of those components, its licence, its directory, and whether its corresponding source is published with the version (`copyleft`), and if it is, the directories its source asset holds (`source_dirs`), its own and a Rust crate's vendored source, how many files they hold (`source_files`), and the SHA-256 of their listing, each entry's path, kind, size and link target, as the asset holds them (`source_listing`);
- `gaps/`, the licence texts of the components Chromium's metadata can't credit at the pin, which `chromium/licence-gaps.json` lists, each with its licence, the files whose texts ship and why. Packaging fails if Chromium's metadata has any error that list doesn't explain, so a new gap is reviewed before anything ships;
- `README`, which says what each of these is, that the mojom tools' licences are beside them rather than in `CREDITS.html`, and where Chromium's own source at the pin and this repository's patch at the version's tag are (Corresponding source, below).

The toolchain is LLVM's, under the Apache License 2.0 with LLVM Exceptions, whose text ships as `toolchain/LICENSE.TXT`. The mojom tools in the `headers` part's `mojom/` (specs/0001-sdk.md, Mojom) are Chromium's, under its licence, with Jinja, MarkupSafe and PLY, each under the BSD 3-clause licence whose text ships in its own directory. The sysroots' packages' copyright files are in their Debian source, below.

## Corresponding source

The release that holds a version's binaries also holds the corresponding source of every component whose licence asks for it, as `source-<name>` assets:

- **Components of the build.** Every component in `components.json` whose licence is a copyleft one (LGPL, GPL, MPL, EPL, CDDL and their like) is archived from the checkout as built, with the SDK's patch applied, and without directories the build never reads, such as Blink's web tests. A Rust crate's archive holds Chromium's directory for it and the vendored source its build names. For the first pin, every target has FFmpeg (LGPL 2.1), Blink's WebKit-derived code in `third_party/blink` (LGPL 2 and 2.1) and the `symphonia` Rust crates (MPL 2.0). Linux adds the GTK and Speech Dispatcher headers (LGPL) and plasma-wayland-protocols (LGPL 2.1). macOS adds libusb (`libusbx` in Chromium's metadata, LGPL 2.1), published as `source-third-party-libusb`. The list comes from Chromium's metadata at every build, never from this document, so a new pin's components are covered as they appear.
- **The sysroots.** A Linux SDK ships Chromium's Debian sysroot, which is Debian's packages, among them the GNU C Library (LGPL), GLib (LGPL) and NSS (MPL). `source-debian-bullseye-sysroots` holds the Debian source package (`.dsc` and the files it lists, verified against it) of every package in either Linux sysroot, at the versions in them, from `snapshot.debian.org`'s archive of the date the sysroot was made, and `packages.json`, which lists them.
- **This repository**, at the commit that built the version, which holds the settings, the patch and the scripts: `source-libchromium`. It's MIT, but for `tests/scripts/fixtures/`, an excerpt of Chromium's own code that a test runs, under Chromium's licence beside it.

`publish.py`'s `manifest` and `release` each refuse a version that lacks any of these, each by the name `sdk.py sources` gives it: `source-libchromium`, `source-debian-bullseye-sysroots` if any target has a sysroot, and for every target, the archive of each component its `components.json` marks as copyleft, named for the component's directory and listing it. Each must also be a whole archive, whose zstd frame and tar both end where they should, and hold what it's for: every file this repository tracks at the version's commit, at its size; a `packages.json` that lists every package of every sysroot, as the `sysroot` parts' own control files list them, each with its own whole `.dsc` and every file that names, matching its SHA-256 there; or exactly the entries of the component's `source_dirs` that its `source_listing` lists. They also refuse a `components.json` that isn't a list of components, each with a name and a directory, and each copyleft one with its `source_dirs`, `source_files` and `source_listing`, and `sources` refuses a sysroot with no Debian packages. So a `sources` step that succeeds with less than it should can't publish binaries without their source.

The rest of the source, Chromium's own at the pin, which is BSD-licensed, is pointed to rather than copied. Each release says where it is, in its notes and in `manifest.json`'s `source`:

- **Chromium**: the pin's tag and commit, and Chromium's official source tarball for the tag, `https://commondatastorage.googleapis.com/chromium-browser-official/chromium-<tag>.tar.xz`, with its checksums file, the same URL with `.hashes` after it. `publish.py release` publishes nothing until both are there.
- **The patch** this version applies to Chromium: `chromium/patches/` in this repository at the release's tag, and at its commit. The `source-libchromium` asset holds the same, with the scripts.

With the patch and scripts, Chromium's source at the tag rebuilds the version's library (`sdk.py checkout` fetches the same tag from `chromium.googlesource.com`), so a user can rebuild it with Blink's LGPL code, which is inside `libchromium`, changed, as well as replace FFmpeg (below). The notices' `README` points to the tarball, its checksums and the patch at the version's tag too, so the pointers travel with the binaries.

## FFmpeg

FFmpeg is a separate shared library, `libffmpeg`, that the library loads at runtime. LGPL 2.1 lets a user run the SDK's library with an FFmpeg of their own: they can build `libffmpeg` from its published source with Chromium's build files at the pin and the version's `args.gn`, and put it in the shipped one's place. Proprietary codecs are off.

## Apple's SDK

Apple's SDK is used only on build machines, from the organization's private bucket for CI tools, under Apple's terms. It is never packaged, published or redistributed, in any asset, artifact or log. A macOS embedder builds with its own.
