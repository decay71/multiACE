# Vendored tools

## ace2k_flash.pyz

The flasher of ace2k by tobecwb (<https://github.com/tobecwb/ace2k>), GPL-3.0
like multiACE. It flashes an ACE 2 Pro with an ace2k image or with the factory
image. The multiACE web backend runs it unchanged (`ace2k_flasher.py`); the
user guide is the project's `docs/flashing.md`.

| | |
|---|---|
| Version | ace2k v0.14.0 |
| Source | tag `v0.14.0`, commit `a0a8398ccd27d84ef7862d53df6da1cadc436a49`, `firmware/tools/ace2k_flash` |
| sha256 | `290a47cc677b9000eaf6d7edc5bcefb8947a0659ca71b5367865560dfc91547b` |

The file is byte-reproducible. Rebuild it from a checkout of the tag:

```
python3 firmware/scripts/build_pyz.py firmware/tools/ace2k_flash ace2k_flash.pyz
```

When you replace it, update the version, commit and sha256 here and the
constants `FLASHER_VERSION` and `FLASHER_SHA256` in `ace2k_flasher.py`.
