# lfs-mkinitcpio

This repository provides automation for building an initramfs on Linux From Scratch (LFS) systems.

## Included tooling

* `scripts/lfs_initrd_setup.py` – Downloads, builds, and installs util-linux, BusyBox (static), and mkinitcpio, deploys the provided configuration files, generates an initramfs, updates GRUB, and rewrites `/etc/fstab` with UUID entries.
* `configs/busybox.config` – BusyBox configuration tuned for static binaries. Adjust as needed for your environment.
* `configs/mkinitcpio.conf` – Example mkinitcpio configuration that the script installs by default.

## Usage

> **Important:** Run this script as `root` from inside your LFS chroot environment.

```bash
python3 scripts/lfs_initrd_setup.py \
  --workdir /sources/build-initrd \
  --busybox-config /path/to/your/busybox.config \
  --mkinitcpio-config /path/to/your/mkinitcpio.conf
```

Options:

* `--workdir`: Directory used to store downloaded tarballs and build artifacts. Defaults to `/tmp/lfs-initrd`.
* `--jobs`: Number of parallel jobs when compiling. Defaults to detected CPU count.
* `--busybox-config`: Path to the BusyBox configuration file. Defaults to `configs/busybox.config`.
* `--mkinitcpio-config`: Path to mkinitcpio configuration. Defaults to `configs/mkinitcpio.conf`.
* `--skip-download`, `--skip-build`, `--skip-initrd`: Allow reusing previously downloaded sources, skipping compilation, or skipping initramfs generation respectively.
* `--fake`: Run every filesystem modification inside a fake root under the chosen workdir so you can validate the workflow without touching your live system.

The script will:

1. Download util-linux, BusyBox, and mkinitcpio sources.
2. Compile and install each component.
3. Install the mkinitcpio configuration you provide.
4. Detect the kernel inside `/lib/modules`, generate a matching `initrd.img`, and update GRUB entries.
5. Regenerate `/etc/fstab` with UUID-based entries via `lsblk`.
6. Warn if GRUB is not installed so you can revisit the relevant LFS/BLFS book sections.

## Providing custom configurations

Place your BusyBox and mkinitcpio configuration files anywhere accessible from the chroot and reference them via the script arguments. The examples in the `configs/` directory are known-good defaults that you can use as a starting point.

## Troubleshooting

* Ensure networking is available inside the chroot so source tarballs can be fetched.
* Verify `/boot` is mounted before running the script; the initramfs and GRUB update require it.
* If `lsblk` is unavailable, install `util-linux` first or rerun the script after the build stage completes.
