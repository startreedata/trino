# StarTree Pinot connector for Trino

This fork maintains patches to Trino's Pinot connector while changes make their
way upstream. Releases contain **only the complete Pinot plugin ZIP**, including
its runtime dependencies. Continue using the official Trino server or container.

The initial branch is based on upstream Trino **483**. Creating the fork does not
publish a patched connector; a connector fix must be committed before publishing.

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

## Connector development

Use a branch `startree/pinot-<trino-version>` based on the corresponding upstream
release tag. Keep connector changes in `plugin/trino-pinot`; a patch requiring
changes to the Trino engine or SPI cannot be delivered as a drop-in connector.
Keep fixes as small, separate commits and send them upstream when practical.

The connector workflow builds the Pinot module using released Trino dependencies
instead of building the server. Trino 483 also needs the small
`trino-testing-kafka` helper built locally because it is not published to Maven
Central. The workflow selects the JDK required by the upstream release.

Run the build commands from `.github/workflows/pinot-connector.yml` locally, or
use its manual **Run workflow** action on your release branch. Branch and PR
builds produce temporary Actions artifacts; only release tags publish permanent
downloads. Add focused tests for the particular connector fixes you make.

## Maintaining releases

For each supported upstream Trino release:

1. Fetch its exact release tag from `https://github.com/trinodb/trino.git` and
   create `startree/pinot-<version>` from that tag. Do not build a release from
   upstream `master` or a `-SNAPSHOT` version.
2. Carry over this fork's connector workflow, its scripts, and this README as a
   tooling commit. Cherry-pick the applicable Pinot fixes with `git cherry-pick
   -x`, resolve any API differences, and remove fixes already included upstream.
3. Push the branch and inspect its connector CI results. The inherited upstream
   workflows are disabled in this fork so it does not run Trino-wide CI.
4. After validating the fixes, tag the exact commit and push the tag:

   ```bash
   git tag -a 483-startree.1 -m 'Pinot connector for Trino 483, StarTree patch 1'
   git push origin 483-startree.1
   ```

The connector workflow checks that the tag matches the upstream version, runs
focused validation, and publishes the connector assets. It refuses to publish
when the connector is unchanged from upstream. Never move a published tag or
replace its assets; issue the next patch number instead. Record the included
fixes and upstream PR links in the release notes.

Supporting another Trino release means building and testing a separate connector
for that release. New upstream releases do not automatically become supported.

## Upstream and license

Source is forked from [trinodb/trino](https://github.com/trinodb/trino), under the
[Apache License 2.0](LICENSE). The full upstream source remains available for
development and provenance. For general Trino development, see the
[upstream README](https://github.com/trinodb/trino/blob/483/README.md) and
[development guide](.github/DEVELOPMENT.md).
