# StarTree Pinot connector for Trino

This fork maintains patches to Trino's Pinot connector while changes make their
way upstream. Releases contain **only the complete Pinot plugin ZIP**, including
its runtime dependencies. Continue using the official Trino server or container.

The default development branch is **`pinot-dev`**, based on upstream `master`.
Its initial patch stack contains all four commits from
[Trino PR #30881](https://github.com/trinodb/trino/pull/30881): authoritative server
host lookup, background instance refresh, coordinator-side instance resolution,
and synchronized gRPC client registration. Connector releases initially start at
Trino **429** and continue with every later stable release.

Known limitation in the imported PR: worker instance metadata is shared between
splits. During server host changes, overlapping splits can reuse another split's
cached address. This fork initially preserves the selected upstream PR as-is.

## Compatibility and downloads

Download from [GitHub Releases](https://github.com/startreedata/trino/releases).
Each release is named `<trino-version>-startree.<patch>`, for example
`483-startree.1`, and is intended **only for that exact Trino version**. Do not use
a 483 connector with a different Trino release. Trino's SPI can change between
releases; see the [upstream compatibility guidance](https://trino.io/docs/current/develop/spi-overview.html#compatibility).

Each release includes the plugin ZIP, a SHA-256 checksum, and JSON build metadata
identifying its upstream version and source commits. The Maven project and Trino
dependency versions remain the upstream release number; the StarTree suffix
identifies the downloadable patched build. We do not publish over upstream Maven
artifacts.

## Installation

The following example assumes `483-startree.1` has been published and your cluster
runs Trino 483. Substitute the release matching your cluster.

```bash
release=483-startree.1
archive="trino-pinot-${release}.zip"
base="https://github.com/startreedata/trino/releases/download/${release}"
curl --fail --location --remote-name "${base}/${archive}"
curl --fail --location --remote-name "${base}/${archive}.sha256"
sha256sum --check "${archive}.sha256"  # macOS: shasum -a 256 -c "${archive}.sha256"
unzip "${archive}" -d staged-pinot
```

1. Stop Trino on every coordinator and worker.
2. Move the current `plugin/pinot` directory to a backup **outside `plugin/`**.
3. Copy the extracted directory containing the plugin JAR and dependency JARs
   into `plugin/pinot` on every node. Replace the entire directory; copying a
   single JAR or overlaying old dependencies can leave incompatible classes.
4. Keep your existing Pinot catalog configuration (`connector.name=pinot`).
5. Start all nodes and run your Pinot query checks. To roll back, stop all nodes,
   restore the backup directory on each node, and restart.

For the official Docker image the destination is `/usr/lib/trino/plugin/pinot`.
Use a derived image or a directory mount to replace it. Each plugin directory
must contain the JAR files directly; avoid an extra nested extraction directory.
Do not retain a second copy of the Pinot plugin under another name in `plugin/`.
See the [upstream installation instructions](https://trino.io/docs/current/installation/plugins.html#installation).

## Development and automatic releases

Commit connector development to `pinot-dev`; keep its patch history linear.
Changes may touch `plugin/trino-pinot` and the Pinot connector documentation.
Engine or SPI changes cannot be shipped as a drop-in connector. Upstream Trino
workflow definitions are removed from downstream branches; only the connector
workflows run.

The [Pinot connector workflow](.github/workflows/pinot-connector.yml) checks daily
for new upstream releases starting at 429, including historical releases from
Maven Central that predate Trino's GitHub release downloads. It also synchronizes fork `master`
and rebases `pinot-dev` onto upstream `master`. A real source conflict stops the
run for maintainer resolution; it never silently drops a conflicting patch.
Pushes and pull requests to `pinot-dev` validate the complete patch stack against
the latest stable Trino release without publishing.

For each new upstream release, automation:

1. Pins the complete `pinot-dev` snapshot and its upstream base.
2. Starts from the exact upstream release tag, retaining that release's Maven
   and dependency versions, and replays **every downstream connector commit in
   order**. Patches already present upstream are recorded as skipped. Tooling is
   copied from the same pinned development snapshot.
3. Runs connector unit tests and loads the resulting complete plugin ZIP into
   the matching official Trino Docker image. The load check verifies packaging
   and catalog initialization; it does not validate queries against a live Pinot
   cluster. Existing cluster integration suites are excluded from this
   small packaging pipeline.
4. Publishes `<trino-version>-startree.<patch>` with only the connector assets. A failed
   replay, build, or runtime check prevents publication. Existing release tags
   and assets are never replaced.

There is no per-release commit selection: every release receives the whole
applicable development stack. Keep fixes in separate commits and send them
upstream when practical. Add focused regression coverage for each fix.

## Manual releases and reproducibility

To publish the current development stack for a specific Trino version, run the
workflow on `pinot-dev`. For example, a second patched build of Trino 483:

```bash
gh workflow run pinot-connector.yml --repo startreedata/trino --ref pinot-dev \
  -f trino_version=483 -f patch_number=2
```

The defaults are `trino_version=latest` and `patch_number=1`. Automatic runs skip
versions that already have a published release. If an incomplete publication
reserved a tag, the next automatic attempt uses a higher patch number. Use the
next patch number when publishing additional fixes for an existing Trino version.

To backfill all missing releases from 429 onward:

```bash
gh workflow run pinot-connector.yml --repo startreedata/trino --ref pinot-dev \
  -f trino_version=all-missing
```

Historical builds use each upstream tag's Java version and dependencies.
Compatibility adaptations preserve the complete development patch stack across
older connector APIs and test frameworks; provenance records every adaptation.

Each release retains an immutable annotated development snapshot tag,
`pinot-dev-snapshot/<full-development-sha>`. Its provenance records that snapshot,
the upstream base and release SHAs, and the complete ordered commit list with
replay/skip status. To rebuild an older development stack after `pinot-dev` has
moved or dropped fixes incorporated upstream, pass its retained snapshot:

```bash
gh workflow run pinot-connector.yml --repo startreedata/trino --ref pinot-dev \
  -f trino_version=483 -f patch_number=3 \
  -f dev_snapshot=pinot-dev-snapshot/<full-development-sha>
```

The reusable [build workflow](.github/workflows/pinot-connector-build.yml) builds
only the connector and its small unpublished `trino-testing-kafka` test helper,
using released Trino artifacts for other dependencies. A `master` development
version such as `484-SNAPSHOT` is never relabeled as a stable release build.
The workflow selects the JDK required by the targeted upstream release.

## Upstream and license

Source is forked from [trinodb/trino](https://github.com/trinodb/trino), under the
[Apache License 2.0](LICENSE). The full upstream source remains available for
development and provenance. For general Trino development, see the
[upstream README](https://github.com/trinodb/trino/blob/483/README.md) and
[development guide](.github/DEVELOPMENT.md).
