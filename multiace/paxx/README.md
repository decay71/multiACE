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

PAXX installs the package under its own versioned application root and exposes
the selected version through a `latest` link. The PAXX hook owns the host-side
bind-mount map and applies it at startup. The stock files are never overwritten
on disk.

The package does not contain or run the standalone SSH installer, uninstaller,
updater, init scripts, or mode-switch file-copy helper. Those operations are
owned by the host firmware integration.

PAXX supplies the managed runtime environment to Klipper and the web service.
The exact host paths are a PAXX concern rather than provider package metadata.
The managed environment includes:

- `MULTIACE_MANAGED=1`
- `MULTIACE_MANAGED_MARKER`
- `MULTIACE_APP_DIR`
- `MULTIACE_WEB_DIR`
- `MULTIACE_CONFIG_DIR`
- `MULTIACE_DISABLE_UPDATES=1`

When managed mode is active, multiACE must refuse its own online updater and
must not copy ACE files over the stock Klipper tree. Updates are staged and
activated by PAXX instead.

Read-only translation catalogs are provider data, not persistent user
configuration. Both runtimes prefer the selected package's `i18n/` directory
and retain the historical standalone layout as a fallback. An explicit
`MULTIACE_I18N_DIR` override remains available for custom deployments, but the
standard managed package does not need to copy catalogs into persistent state.

The marker file is a durable fallback for SSH sessions or services that do not
inherit the activation hook's environment. Standalone install and uninstall
also refuse to run when the marker exists; an intentional manual override is
available with `MULTIACE_IGNORE_FIRMWARE_MANAGED=1`.

The managed package's configuration template omits the standalone update
wrapper macros. PAXX may still perform a one-time migration of an older
persistent configuration created by an earlier managed package.

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
