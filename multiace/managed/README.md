# multiACE managed package

This package is the platform-neutral payload for host-managed multiACE
installations. It is separate from the standalone SSH installer so users on
stock firmware can continue using the existing standalone installation and
self-update process.

## Ownership boundary

- multiACE owns ACE behavior, Klipper modules, and the web interface.
- The host platform owns the selected multiACE version, package installation,
  activation, persistent configuration, updates, rollback, and user-facing
  controls.
- The package omits the standalone installer, uninstaller, updater, boot
  service, and file-copy mode-switch helper. The source repository still
  contains those standalone components.

Managed deployments use a shared runtime contract. The host passes these
variables to both Klipper and the web service:

- `MULTIACE_MANAGED=1` enables managed behavior.
- `MULTIACE_MANAGED_MARKER` points to the durable `.multiace-managed` marker
  under the shared multiACE configuration/state directory.
- `MULTIACE_CONFIG_DIR` is the directory containing `printer.cfg` (for the
  U1, `/home/lava/printer_data/config`).
- `MULTIACE_PRINTER_DATA` is the printer-data root (for the U1,
  `/home/lava/printer_data`).
- `MULTIACE_APP_DIR` is the active package root, used to locate packaged
  provider data such as translation catalogs.

The two printer paths follow the shared contract in
[issue #142](https://github.com/decay71/multiACE/issues/142). multiACE derives
`extended/ace.cfg` and its persistent state from these roots; a host should not
provide a second config-file path that could diverge.

In managed mode, install, uninstall, and self-update entry points refuse to
run. The web Update area reports that updates are managed by the platform
instead of invoking the updater. `SET_ACE_MODE MODE=normal` and the standalone
file-copy mode switch are refused; switching between multi and head remains a
runtime-only operation.

The managed archive contains the provider's clean `ace.cfg` defaults and
macros, but omits the standalone `[save_variables]` path and self-update
wrapper macros. Each platform seeds its own persistent save-variable path and
preserves existing user values. The standalone source config remains
unchanged.

## Archive

The allowlisted archive is named `multiace-managed-<version>.tar.gz` and is
published with a matching SHA256 file for each release. The host pins both the
release and checksum before installation.

From the repository root, a package and checksum can be built with:

```text
python3 multiace/managed/build_package.py
```

## Release and test-build flow

Every branch push and pull request runs validation and builds a check package;
those runs do not publish release assets.

After this workflow is present on the repository's default branch, a maintainer
can manually run **Build and test multiACE packages** with a `source_ref`
(branch, tag, or commit SHA). The workflow uploads both archives, their SHA-256
sidecars, and build metadata as a 14-day Actions artifact; it does not create a
tag or GitHub Release. Anyone with repository read access can download the
artifact from its workflow run, but these files are not release assets and are
not offered by the standalone updater. This is intended for deliberate
compatibility testing, not as an update channel.

Stable releases keep the existing tag-driven process: pushing
`v<VERSION>` runs validation, verifies that the tag matches `multiace/VERSION`,
and automatically publishes both package types with their checksums. GitHub
generates the change list using the categories in `.github/release.yml`; no
hand-maintained changelog is required.

The standalone updater selects only the release's exact
`multiace-<tag>.tar.gz` archive and matching checksum. It will fail closed if
either asset is missing or if checksum verification fails. The managed archive
is never a candidate for the standalone installer. Package construction and
publishing do not replace or alter the standalone installation path.
