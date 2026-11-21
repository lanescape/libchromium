# libchromium

Prebuilt Chromium for embedders of its Content API. Each version is upstream Chromium at one release tag, built with fixed settings as one shared library that exports the Content API, and shipped with what an embedder needs to compile and link its own code against it: the headers, generated ones included, the build settings, and Chromium's own toolchain, its clang and its libc++. An embedder pins a version, compiles only its own code and links the library; it never compiles Chromium. Moving to a new Chromium release, or to new settings, is moving to a new version.

Targets are `linux-amd64`, `linux-arm64` and `macos-arm64`. Apple's SDK is never part of a version: a macOS embedder brings its own.

## Using a version

Each version is a GitHub release of this repository, tagged `v<Chromium tag>-<revision>`, such as `v157.0.8103.39-1`. Versions are not semver and promise no compatibility with each other: pin an exact version. For a target, download its `lib`, `headers` and `toolchain` parts, and on Linux its `sysroot`, check them against the release's `SHA256SUMS`, and extract them into one directory. Its `sdk.json` gives the compile and link flags, with paths relative to that directory, for any build tool, and where the `headers` part holds Chromium's mojom generator and what it needs to generate the C++ bindings of an embedder's own mojoms; `tests/embedder/build.py` is a complete example of both, and specs/0001-sdk.md gives the generator's commands. The toolchain runs on Linux x86-64. specs/0001-sdk.md says what a version holds and what an embedder must do.

## Building and testing

On a Linux x86-64 machine (specs/0002-build.md):

```
export LIBCHROMIUM_CHECKOUT=$HOME/libchromium/checkout
for step in checkout packages mac-sdk patch configure build; do python3 scripts/sdk.py $step linux-amd64; done
python3 scripts/sdk.py package linux-amd64 --version 157.0.8103.39-dev --output out
mkdir sdk && for part in lib headers toolchain sysroot; do tar --zstd -xf out/libchromium-*-linux-amd64-$part.tar.zst -C sdk; done
python3 tests/embedder/build.py --sdk sdk --output embedder
tar -cf embedder.tar -C embedder . && tests/embedder/test.sh embedder.tar
```

The checks that need no Chromium:

```
python3 -m unittest discover --start-directory tests/scripts --top-level-directory .
python3 scripts/check.py
```

## Specifications

- specs/0001-sdk.md: what a version is and holds, `sdk.json`, the exports, mojom, libc++, the allocator and runtime files
- specs/0002-build.md: the checkout, settings, patch, build and packaging
- specs/0003-ci-cd.md: CI, CD, versions and publishing
- specs/0004-licences.md: notices and corresponding source

## Licences

This repository's own files are under the MIT License (`LICENSE`). The binaries it publishes are Chromium's, under Chromium's licences: each version ships Chromium's `LICENSE`, the credits for every third-party library the build links, and the corresponding source of every component whose licence asks for it, such as FFmpeg's under the LGPL, published from the same place as the binaries. Each release also points to Chromium's own source for its tag, Chromium's official source tarball and its checksums, and to the patch in this repository at its tag (specs/0004-licences.md).
