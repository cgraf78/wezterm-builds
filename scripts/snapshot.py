"""Capture a local candidate release from the selected WezTerm publishers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

NIGHTLY_API = "repos/wezterm/wezterm/releases/tags/nightly"
NIGHTLY_URL = "https://github.com/wezterm/wezterm/releases/download/nightly"
RELEASE_URL = "https://github.com/cgraf78/wezterm-builds/releases/download"
ARCH_URL = "https://geo.mirror.pkgbuild.com/extra/os/x86_64"
RPM_NAMES = ("wezterm", "wezterm-common", "wezterm-gui", "wezterm-mux-server")
TAG = re.compile(r"snapshot-([0-9]{4})([0-9]{2})([0-9]{2})(?:-[1-9][0-9]*)?")
VERSION = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}")
SHA256 = re.compile(r"[0-9a-f]{64}")
DEB_TARGET = re.compile(r"(debian|ubuntu)([0-9]+(?:\.[0-9]+)?)-(x86_64|arm64)")
RPM_TARGET = re.compile(r"((?:centos|fedora)[0-9]+)-x86_64")
ARCH_PACKAGE = re.compile(r"wezterm-[A-Za-z0-9._-]+-[0-9]+-x86_64\.pkg\.tar\.zst")


@dataclass(frozen=True)
class Asset:
    """A payload, its embedded WezTerm version, and its publisher provenance."""

    target: str
    version: str
    checksum: str
    url: str

    @property
    def name(self) -> str:
        return self.url.rsplit("/", 1)[-1]


def check_tag(tag: str) -> str:
    """Validate a release tag before using it in a URL."""
    match = TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"invalid snapshot tag {tag!r}; expected snapshot-YYYYMMDD[-N]")
    try:
        date(*(int(part) for part in match.groups()))
    except ValueError as error:
        raise ValueError(f"invalid date in snapshot tag {tag!r}") from error
    return tag


def check_groups(rows: list[Asset]) -> None:
    """Require complete, consistent package sets for every declared target."""
    groups: dict[str, list[Asset]] = defaultdict(list)
    for row in rows:
        groups[row.target].append(row)

    for target, assets in groups.items():
        names = {row.name for row in assets}
        if target == "macos":
            expected = {"WezTerm-macos-nightly.zip"}
        elif target == "arch-x86_64":
            packages = [name for name in names if ARCH_PACKAGE.fullmatch(name)]
            if len(packages) != 1:
                raise ValueError(f"expected one Arch package for {target}")
            expected = {packages[0], f"{packages[0]}.sig"}
        elif match := DEB_TARGET.fullmatch(target):
            distro, release, arch = match.groups()
            arch_suffix = ".arm64" if arch == "arm64" else ""
            expected = {f"wezterm-nightly.{distro.title()}{release}{arch_suffix}.deb"}
        elif match := RPM_TARGET.fullmatch(target):
            expected = {f"{name}-nightly-{match[1]}.rpm" for name in RPM_NAMES}
        else:
            raise ValueError(f"unsupported snapshot target {target!r}")

        if names != expected or len(assets) != len(expected):
            raise ValueError(
                f"expected exactly these assets for {target}: {', '.join(sorted(expected))}"
            )
        if len({row.version for row in assets}) != 1:
            raise ValueError(f"assets for {target} have different embedded versions")


def read_manifest(path: Path) -> list[Asset]:
    """Load publisher rows in their existing order and validate the complete file."""
    rows = []
    filenames = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 4:
            raise ValueError(
                f"{path}:{number}: expected target, version, SHA-256, and publisher URL"
            )
        row = Asset(*fields)
        publisher = ARCH_URL if row.target == "arch-x86_64" else NIGHTLY_URL
        if row.url != f"{publisher}/{row.name}" or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", row.name
        ):
            raise ValueError(f"{path}:{number}: invalid publisher URL or unsafe filename")
        if not VERSION.fullmatch(row.version) or not SHA256.fullmatch(row.checksum):
            raise ValueError(f"{path}:{number}: invalid embedded version or SHA-256")
        if row.name in filenames:
            raise ValueError(f"{path}:{number}: duplicate asset filename {row.name}")
        filenames.add(row.name)
        rows.append(row)
    if not rows:
        raise ValueError(f"{path}: manifest has no assets")
    check_groups(rows)
    return rows


def render_lock(rows: list[Asset], tag: str | None = None) -> str:
    """Render provenance rows or replace URLs with the named snapshot release."""
    if tag is not None:
        check_tag(tag)
    lines = ["# Target  Embedded version  SHA-256  URL"]
    for row in rows:
        url = row.url if tag is None else f"{RELEASE_URL}/{tag}/{row.name}"
        lines.append(f"{row.target} {row.version} {row.checksum} {url}")
    return "\n".join(lines) + "\n"


def run(*args: str) -> str:
    """Run an external tool and retain its failure message for the caller."""
    env = None
    if args[0] != "gh":
        # Package inspection and public downloads do not need GitHub credentials.
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GH_TOKEN", "GITHUB_TOKEN"}
        }
    try:
        return subprocess.run(args, capture_output=True, text=True, check=True, env=env).stdout
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or f"exit {error.returncode}"
        raise ValueError(f"{args[0]} failed: {detail}") from error


def nightly_digests(rows: list[Asset], metadata: Path | None) -> dict[str, str]:
    """Resolve the expected SHA-256 for each selected artifact from GitHub."""
    if not rows:
        return {}
    raw = metadata.read_text(encoding="utf-8") if metadata else run("gh", "api", NIGHTLY_API)
    release = json.loads(raw)
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise TypeError("GitHub nightly response has no asset list")
    digests = {}
    for row in rows:
        matches = [
            asset
            for asset in release["assets"]
            if isinstance(asset, dict) and asset.get("name") == row.name
        ]
        if len(matches) != 1 or matches[0].get("browser_download_url") != row.url:
            raise ValueError(f"GitHub nightly does not list the expected asset {row.name}")
        digest = matches[0].get("digest")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or not SHA256.fullmatch(digest[7:])
        ):
            raise ValueError(f"GitHub nightly has no SHA-256 digest for {row.name}")
        digests[row.name] = digest[7:]
    return digests


def download(url: str, destination: Path, expected: str) -> None:
    """Use a public HTTPS download and validate its bytes before opening it."""
    run(
        "curl",
        "--disable",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--no-netrc",
        "--connect-timeout",
        "10",
        "--speed-limit",
        "1024",
        "--speed-time",
        "60",
        "--retry",
        "3",
        "--output",
        str(destination),
        url,
    )
    hasher = hashlib.sha256()
    with destination.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            hasher.update(block)
    if hasher.hexdigest() != expected:
        raise ValueError(
            f"SHA-256 mismatch for {destination.name}; the publisher may have rotated it"
        )


def mac_version(path: Path) -> str:
    """Find the build in the publisher's ZIP layout without writing its contents."""
    binary = "/WezTerm.app/Contents/MacOS/wezterm"
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    prefix = "WezTerm-macos-"
    matching = [name for name in names if name.startswith(prefix) and name.endswith(binary)]
    if len(matching) != 1 or f"{matching[0]}-gui" not in names:
        raise ValueError(f"{path.name}: expected one WezTerm.app with CLI and GUI executables")
    return matching[0][len(prefix) : -len(binary)]


def embedded_version(row: Asset, path: Path) -> str:
    """Read an upstream build and confirm the native identity for its target."""
    if row.target == "macos":
        version = mac_version(path)
    elif match := DEB_TARGET.fullmatch(row.target):
        arch = "amd64" if match[3] == "x86_64" else "arm64"
        info = (
            run(
                "dpkg-deb",
                "--show",
                r"--showformat=${Package}\t${Version}\t${Architecture}\n",
                str(path),
            )
            .strip()
            .split("\t")
        )
        if len(info) != 3 or info[0] != "wezterm-nightly" or info[2] != arch:
            raise ValueError(f"{path.name}: expected the wezterm-nightly DEB for {arch}")
        version = info[1]
    elif match := RPM_TARGET.fullmatch(row.target):
        package = row.name.split("-nightly-", 1)[0]
        info = (
            run(
                "rpm",
                "-qp",
                "--queryformat",
                r"%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\n",
                str(path),
            )
            .strip()
            .split("\t")
        )
        if len(info) != 5 or info[:2] != [package, "0"] or info[3:] != [f"1.{match[1]}", "x86_64"]:
            raise ValueError(f"{path.name}: expected epoch-zero {package}-1.{match[1]}.x86_64 RPM")
        version = info[2].replace("_", "-")
    else:
        raise ValueError(f"no version reader for {row.target}")
    if not VERSION.fullmatch(version):
        raise ValueError(f"{path.name}: invalid WezTerm build {version!r}")
    return version


def capture(
    base_manifest: Path, base_tag: str, tag: str, output: Path, metadata: Path | None = None
) -> None:
    """Stage every validated payload and publish a new local candidate directory."""
    check_tag(base_tag)
    check_tag(tag)
    if base_tag == tag:
        raise ValueError("the candidate tag must differ from the base snapshot tag")
    if os.path.lexists(output):
        raise ValueError(f"output already exists: {output}")

    old_rows = read_manifest(base_manifest)
    digests = nightly_digests([row for row in old_rows if row.target != "arch-x86_64"], metadata)
    rows = []
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=output.parent) as temporary:
        staging = Path(temporary)
        for old in old_rows:
            destination = staging / old.name
            if old.target == "arch-x86_64":
                # Keep the exact signed Arch pair even after mirrors retire it.
                download(f"{RELEASE_URL}/{base_tag}/{old.name}", destination, old.checksum)
                rows.append(old)
            else:
                digest = digests[old.name]
                download(old.url, destination, digest)
                rows.append(
                    replace(old, version=embedded_version(old, destination), checksum=digest)
                )
        check_groups(rows)
        (staging / "snapshot.lock").write_text(render_lock(rows), encoding="utf-8")
        sums = "".join(f"{row.checksum}  {row.name}\n" for row in rows)
        (staging / "SHA256SUMS").write_text(sums, encoding="utf-8")

        # Claim the name exclusively after validation. The same-filesystem
        # rename replaces only the empty directory this invocation created.
        output.mkdir(mode=0o700)
        try:
            staging.rename(output)
        except OSError:
            output.rmdir()
            raise


def main(argv: list[str] | None = None) -> int:
    """Dispatch the local snapshot operations."""
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    capture_command = actions.add_parser("capture", help="capture a new candidate locally")
    capture_command.add_argument("--base-manifest", type=Path, required=True)
    capture_command.add_argument("--base-tag", required=True)
    capture_command.add_argument("--tag", required=True)
    capture_command.add_argument("--output", type=Path, required=True)
    capture_command.add_argument("--nightly-metadata", type=Path)
    lock_command = actions.add_parser("lock", help="print an existing snapshot's consumer lock")
    lock_command.add_argument("--manifest", type=Path, required=True)
    lock_command.add_argument("--tag", required=True)
    args = parser.parse_args(argv)

    try:
        if args.action == "capture":
            capture(args.base_manifest, args.base_tag, args.tag, args.output, args.nightly_metadata)
        else:
            sys.stdout.write(render_lock(read_manifest(args.manifest), args.tag))
    except (OSError, TypeError, ValueError, zipfile.BadZipFile) as error:
        print(f"snapshot: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
