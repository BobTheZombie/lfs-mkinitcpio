#!/usr/bin/env python3
"""Automate initramfs tooling installation for LFS systems.

This script downloads, builds, and installs util-linux, BusyBox (static),
and mkinitcpio.  It then deploys user supplied configuration files, creates
an initramfs, refreshes GRUB, and regenerates /etc/fstab entries with UUIDs.

The implementation targets chroot friendly behaviour by relying on
information that can be collected from inside the chroot tree (e.g. the
installed kernel version under /lib/modules).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.request import urlopen


SCRIPT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_DIR = SCRIPT_ROOT.parent / "configs"
DEFAULT_BUSYBOX_CONFIG = DEFAULT_CONFIG_DIR / "busybox.config"
DEFAULT_MKINITCPIO_CONFIG = DEFAULT_CONFIG_DIR / "mkinitcpio.conf"


@dataclass
class Package:
    name: str
    version: str
    url: str
    archive_name: Optional[str] = None

    @property
    def full_name(self) -> str:
        return f"{self.name}-{self.version}"

    @property
    def filename(self) -> str:
        if self.archive_name:
            return self.archive_name
        return os.path.basename(self.url)


PACKAGES: Dict[str, Package] = {
    "util-linux": Package(
        name="util-linux",
        version="2.41.1",
        url="https://mirrors.edge.kernel.org/pub/linux/utils/util-linux/v2.41/util-linux-2.41.1.tar.gz",
    ),
    "libarchive": Package(
        name="libarchive",
        version="3.7.4",
        url="https://libarchive.org/downloads/libarchive-3.7.4.tar.gz",
    ),
    "busybox": Package(
        name="busybox",
        version="1.36.1",
        url="https://busybox.net/downloads/busybox-1.36.1.tar.bz2",
    ),
    "mkinitcpio": Package(
        name="mkinitcpio",
        version="38",
        url="https://github.com/archlinux/mkinitcpio/archive/refs/tags/v38.tar.gz",
        archive_name="mkinitcpio-38.tar.gz",
    ),
}


class CommandError(RuntimeError):
    pass


def run_command(
    cmd: Iterable[str], *, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None
) -> None:
    """Execute a command while echoing it to stdout."""
    printable = " ".join(cmd)
    print(f"[CMD] {printable}")
    try:
        subprocess.run(cmd, cwd=cwd, env=env, check=True)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - defensive
        raise CommandError(f"Command failed with exit code {exc.returncode}: {printable}") from exc


def ensure_root(allow_non_root: bool = False) -> None:
    if allow_non_root:
        return
    if os.geteuid() != 0:
        sys.exit("This script must be run as root.")


def prompt_for_confirmation() -> None:
    print(
        "DISCLAIMER: This automation can modify boot-critical components. "
        "I am not responsible for a non-booting or corrupted system."
    )
    print(
        "The script will download sources, build packages, install configuration files, "
        "generate an initramfs, update GRUB, and regenerate /etc/fstab."
    )
    print("Review the planned actions above before continuing.")
    response = input("Enter 'Y' to confirm and continue: ").strip()
    if response.upper() != "Y":
        sys.exit("Aborted by user confirmation.")


def download(package: Package, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / package.filename
    if target.exists():
        print(f"[SKIP] {package.filename} already present")
        return target

    print(f"[DL ] Downloading {package.url}")
    with urlopen(package.url) as response, open(target, "wb") as handle:
        shutil.copyfileobj(response, handle)
    return target


def extract(archive: Path, destination: Path) -> Path:
    print(f"[EX ] Extracting {archive.name}")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        tar.extractall(path=destination)
        members = [m for m in tar.getmembers() if m.isdir()]

    if members:
        root_dir = sorted(members, key=lambda m: len(m.name.split("/")))[0].name.split("/")[0]
        return destination / root_dir

    return destination / archive.stem


def prepare_sources(workdir: Path, packages: Iterable[Package]) -> Dict[str, Path]:
    """Download and extract the requested package sources."""
    sources: Dict[str, Path] = {}
    tarball_dir = workdir / "sources"
    build_dir = workdir / "build"
    for pkg in packages:
        archive = download(pkg, tarball_dir)
        source_root = extract(archive, build_dir)
        sources[pkg.name] = source_root
    return sources


def build_libarchive(source: Path, jobs: int, *, destdir: Optional[Path] = None) -> None:
    configure_cmd = [
        str(source / "configure"),
        "--prefix=/usr",
        "--disable-static",
    ]
    run_command(configure_cmd, cwd=source)
    run_command(["make", f"-j{jobs}"], cwd=source)
    install_cmd: List[str] = ["make", "install"]
    if destdir is not None:
        install_cmd.append(f"DESTDIR={destdir}")
    run_command(install_cmd, cwd=source)


def build_util_linux(source: Path, jobs: int, *, destdir: Optional[Path] = None) -> None:
    build_dir = source / "build"
    build_dir.mkdir(exist_ok=True)
    configure_cmd = [
        str(source / "configure"),
        "--prefix=/usr",
        "--sysconfdir=/etc",
        "--localstatedir=/var",
        "--enable-uuidd",
        "--disable-makeinstall-chown",
        "--disable-chfn-chsh-password",
        "--with-systemd",
        "--disable-static",
        "--enable-write",
        "--enable-chfn-chsh",
        "--with-systemdsystemunitdir=/usr/lib/systemd/system"
    ]
    run_command(configure_cmd, cwd=build_dir)
    run_command(["make", f"-j{jobs}"], cwd=build_dir)
    install_cmd: List[str] = ["make", "install"]
    if destdir is not None:
        install_cmd.append(f"DESTDIR={destdir}")
    run_command(install_cmd, cwd=build_dir)


def build_busybox(
    source: Path, jobs: int, config_file: Path, *, destdir: Optional[Path] = None
) -> None:
    if not config_file.exists():
        raise FileNotFoundError(f"BusyBox config not found: {config_file}")

    run_command(["make", "distclean"], cwd=source)
    shutil.copy2(config_file, source / ".config")
    run_command(["make", f"-j{jobs}"], cwd=source)
    config_prefix = Path("/usr") if destdir is None else destdir / "usr"
    run_command(["make", f"CONFIG_PREFIX={config_prefix}", "install"], cwd=source)


def build_mkinitcpio(source: Path, jobs: int, *, destdir: Optional[Path] = None) -> None:
    build_dir = source / "build"
    build_dir.mkdir(exist_ok=True)
    run_command(["meson", "setup", "--prefix=/usr", "--buildtype=release", str(build_dir)], cwd=source)
    run_command(["meson", "compile", "-C", str(build_dir), f"-j{jobs}"], cwd=source)
    install_cmd = ["meson", "install", "-C", str(build_dir)]
    if destdir is not None:
        install_cmd.extend(["--destdir", str(destdir)])
    run_command(install_cmd, cwd=source)


def libarchive_installed() -> bool:
    try:
        subprocess.run(["pkg-config", "--exists", "libarchive"], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return True


def install_mkinitcpio_config(config_file: Path, system_root: Path) -> Path:
    if not config_file.exists():
        raise FileNotFoundError(f"mkinitcpio config not found: {config_file}")

    destination = system_root / "etc/mkinitcpio.conf"
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_suffix(destination.suffix + ".bak")
    if destination.exists() and not backup.exists():
        shutil.copy2(destination, backup)
        print(f"[INFO] Existing mkinitcpio.conf backed up to {backup}")

    shutil.copy2(config_file, destination)
    print(f"[INFO] Installed mkinitcpio.conf from {config_file}")
    return destination


def detect_kernel_version(system_root: Path) -> str:
    modules_root = system_root / "lib/modules"
    if modules_root.exists():
        candidates = [d.name for d in modules_root.iterdir() if d.is_dir()]
        if candidates:
            candidates.sort()
            kernel = candidates[-1]
            print(f"[INFO] Detected kernel version {kernel} from /lib/modules")
            return kernel

    kernel = os.uname().release
    print(f"[WARN] Falling back to host kernel version {kernel}")
    return kernel


def build_initrd(
    kernel: str,
    mkinitcpio_conf: Path,
    system_root: Path,
    *,
    fake: bool,
) -> Path:
    initrd_path = system_root / f"boot/initrd.img-{kernel}"
    initrd_path.parent.mkdir(parents=True, exist_ok=True)
    env = None
    if fake:
        env = os.environ.copy()
        fake_path = system_root / "usr/bin"
        env["PATH"] = f"{fake_path}:{env.get('PATH', '')}"
    run_command(
        [
            "mkinitcpio",
            "-g",
            str(initrd_path),
            "-k",
            kernel,
            "-c",
            str(mkinitcpio_conf),
        ],
        env=env,
    )
    print(f"[INFO] Generated {initrd_path}")
    return initrd_path


def update_grub_cfg(initrd_path: Path, kernel: str, system_root: Path) -> None:
    grub_cfg = system_root / "boot/grub/grub.cfg"
    if not grub_cfg.exists():
        print("[WARN] GRUB configuration not found. Please ensure GRUB is installed per the LFS/BLFS book.")
        return

    original = grub_cfg.read_text()
    backup = grub_cfg.with_suffix(".bak")
    if not backup.exists():
        shutil.copy2(grub_cfg, backup)
        print(f"[INFO] Backed up grub.cfg to {backup}")

    new_lines: List[str] = []
    replacement_done = False
    for line in original.splitlines():
        if line.strip().startswith("initrd"):
            indent = line[: len(line) - len(line.lstrip())]
            new_lines.append(f"{indent}initrd {initrd_path}")
            replacement_done = True
        else:
            new_lines.append(line)

    if not replacement_done:
        # Append to the first linux entry
        appended = False
        rewritten: List[str] = []
        for line in new_lines:
            rewritten.append(line)
            if line.strip().startswith("linux") and not appended:
                indent = line[: len(line) - len(line.lstrip())]
                rewritten.append(f"{indent}initrd {initrd_path}")
                appended = True
        new_lines = rewritten

    grub_cfg.write_text("\n".join(new_lines) + "\n")
    print(f"[INFO] Updated {grub_cfg} to use {initrd_path.name}")


@dataclass
class FstabEntry:
    device: str
    uuid: str
    fstype: str
    mountpoint: str

    def to_line(self) -> str:
        options = "defaults"
        if self.fstype == "swap" or self.mountpoint == "swap":
            return f"UUID={self.uuid}\tswap\tswap\tpri=0\t0\t0"

        dump = "1" if self.mountpoint == "/" else "0"
        passno = "1" if self.mountpoint == "/" else "2"
        return f"UUID={self.uuid}\t{self.mountpoint}\t{self.fstype}\t{options}\t{dump}\t{passno}"


def rebuild_fstab(system_root: Path) -> None:
    existing = system_root / "etc/fstab"
    existing.parent.mkdir(parents=True, exist_ok=True)
    backup = existing.with_suffix(".bak")
    if existing.exists() and not backup.exists():
        shutil.copy2(existing, backup)
        print(f"[INFO] Backed up fstab to {backup}")

    try:
        result = subprocess.run(
            ["lsblk", "-pnro", "NAME,UUID,FSTYPE,MOUNTPOINT"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        print("[WARN] Unable to query block devices with lsblk; skipping fstab regeneration.")
        return

    entries: List[FstabEntry] = []
    for raw in result.stdout.strip().splitlines():
        parts = raw.split(None, 3)
        if len(parts) < 4:
            continue
        name, uuid, fstype, mountpoint = (part.strip() for part in parts)
        if not uuid:
            continue
        if mountpoint == "[SWAP]" or fstype == "swap":
            entries.append(FstabEntry(name, uuid, "swap", "swap"))
            continue
        if not mountpoint or mountpoint == "-":
            continue
        entries.append(FstabEntry(name, uuid, fstype or "auto", mountpoint))

    if not entries:
        print("[WARN] No mounted devices discovered; skipping fstab regeneration.")
        return

    new_content = (
        "# Generated by lfs_initrd_setup.py\n"
        + "\n".join(entry.to_line() for entry in entries)
        + "\n"
    )
    existing.write_text(new_content)
    print("[INFO] Regenerated /etc/fstab using UUIDs")


def check_grub_installation(system_root: Path) -> None:
    grub_cfg = system_root / "boot/grub/grub.cfg"
    grub_dir = grub_cfg.parent if grub_cfg.exists() else system_root / "boot/grub"
    grub_install = shutil.which("grub-install")
    if not grub_dir.exists() or grub_install is None:
        print("[WARN] GRUB installation not detected. Please consult the LFS/BLFS book and install GRUB before running this script again.")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build util-linux, BusyBox, and mkinitcpio for LFS")
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/lfs-initrd"), help="Working directory for building sources")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2, help="Number of make jobs")
    parser.add_argument("--busybox-config", type=Path, default=DEFAULT_BUSYBOX_CONFIG, help="Path to BusyBox .config file")
    parser.add_argument("--mkinitcpio-config", type=Path, default=DEFAULT_MKINITCPIO_CONFIG, help="Path to mkinitcpio.conf")
    parser.add_argument("--skip-download", action="store_true", help="Skip downloading and extracting sources")
    parser.add_argument("--skip-build", action="store_true", help="Skip building packages")
    parser.add_argument("--skip-initrd", action="store_true", help="Skip creating initramfs and updating bootloader")
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Operate entirely within a fake root under the workdir to avoid touching system files",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    ensure_root(allow_non_root=args.fake)
    prompt_for_confirmation()
    args.workdir.mkdir(parents=True, exist_ok=True)

    system_root = args.workdir / "fake_root" if args.fake else Path("/")
    if args.fake:
        system_root.mkdir(parents=True, exist_ok=True)

    needs_libarchive_build = args.fake or not libarchive_installed()
    packages_to_prepare = [
        pkg
        for pkg in PACKAGES.values()
        if pkg.name != "libarchive" or (needs_libarchive_build and not args.skip_build)
    ]

    sources = {}
    if not args.skip_download:
        print("\n[STEP 1] Preparing package sources (download & extract)")
        sources = prepare_sources(args.workdir, packages_to_prepare)
    else:
        print("\n[STEP 1] Skipping source downloads (verification only)")
        build_root = args.workdir / "build"
        for pkg in packages_to_prepare:
            source = build_root / pkg.full_name
            if not source.exists():
                sys.exit(f"Expected source directory {source} is missing. Remove --skip-download to fetch sources.")
            sources[pkg.name] = source

    if not args.skip_build:
        print("\n[STEP 2] Building required packages")
        destdir = system_root if args.fake else None
        if needs_libarchive_build:
            print("[INFO] libarchive not detected; building from source")
            build_libarchive(sources["libarchive"], args.jobs, destdir=destdir)
        else:
            print("[SKIP] libarchive already installed; skipping build")
        build_util_linux(sources["util-linux"], args.jobs, destdir=destdir)
        build_busybox(sources["busybox"], args.jobs, args.busybox_config, destdir=destdir)
        build_mkinitcpio(sources["mkinitcpio"], args.jobs, destdir=destdir)
    else:
        print("\n[STEP 2] Package build skipped")

    print("\n[STEP 3] Installing mkinitcpio configuration")
    mkinitcpio_conf = install_mkinitcpio_config(args.mkinitcpio_config, system_root)
    print("\n[STEP 4] Verifying GRUB installation")
    check_grub_installation(system_root)

    if not args.skip_initrd:
        print("\n[STEP 5] Generating initramfs and updating GRUB")
        kernel_version = detect_kernel_version(system_root)
        initrd = build_initrd(kernel_version, mkinitcpio_conf, system_root, fake=args.fake)
        update_grub_cfg(initrd, kernel_version, system_root)
    else:
        print("\n[STEP 5] Initramfs generation skipped")

    print("\n[STEP 6] Regenerating /etc/fstab entries")
    rebuild_fstab(system_root)
    print("[DONE] All tasks completed.")


if __name__ == "__main__":
    try:
        main()
    except (CommandError, FileNotFoundError) as exc:
        sys.exit(str(exc))
