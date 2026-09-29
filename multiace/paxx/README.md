# PAXX-managed multiACE package

This directory describes the package boundary used when PAXX manages
multiACE. It is intentionally separate from the existing SSH installer.

## Ownership

- multiACE owns the ACE protocols, Klipper integration modules, web interface,
  and versioned release archives.
- PAXX owns activation, compatibility checks, persistent configuration,
  upgrades, rollback, and the firmware-config user interface.

PAXX must select a specific release and verify its archive checksum. It must
not install an arbitrary moving `latest` build.

## Runtime contract

PAXX installs the package under `runtime.versioned_app_root` and exposes the
selected version through `runtime.active_app_root` (`latest`). The PAXX hook then
bind-mounts the listed Klipper modules at startup. The stock files are never
overwritten on disk.

The package does not contain or run the standalone SSH installer, uninstaller,
updater, init scripts, or mode-switch file-copy helper. Those operations are
owned by the host firmware integration.

The following environment variables are supplied to Klipper and the web
service by PAXX:

- `MULTIACE_MANAGED=1`
- `MULTIACE_APP_DIR`
- `MULTIACE_WEB_DIR`
- `MULTIACE_CONFIG_DIR`
- `MULTIACE_DISABLE_UPDATES=1`

When managed mode is active, multiACE must refuse its own online updater and
must not copy ACE files over the stock Klipper tree. Updates are staged and
activated by PAXX instead.

## Building a package

From the repository root:

```text
python3 multiace/paxx/build_package.py
```

The builder uses an allowlist from `manifest.json`, writes a deterministic
versioned archive, and emits a matching `.sha256` file. The resulting archive
is safe to use as the payload for a PAXX `extended-pkg` definition; PAXX still
supplies the activation hook and persistent configuration seeding.

The `paxx-package.yml` workflow runs the same tests and publishes the archive
and checksum as release assets for tags named `multiace-v< version >`.

The existing `install_multiace.sh` path remains available for standalone
multiACE installations. It is not used by the PAXX-managed package.
