"""Exercise the capture and lock command lines with local publisher fixtures."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/snapshot.py"
NIGHTLY = "https://github.com/wezterm/wezterm/releases/download/nightly"
RELEASES = "https://github.com/cgraf78/wezterm-builds/releases/download"
ARCH_MIRROR = "https://geo.mirror.pkgbuild.com/extra/os/x86_64"
BASE_TAG = "snapshot-20261121"
TAG = "snapshot-20261122-2"
OLD_VERSION = "20261112-061903-ce8e6b14"
NEW_VERSION = "20261121-002403-ee6f3044"
ARCH_VERSION = "20260716-195552-76b606ec"
ARCH_PACKAGE = "wezterm-20240203.110809.5046fc22.r869.g76b606ec5-4-x86_64.pkg.tar.zst"
OLD_SHA256 = "0" * 64
RPM_NAMES = ("wezterm", "wezterm-common", "wezterm-gui", "wezterm-mux-server")

FAKE_COMMAND = r"""
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
command = sys.argv[2]
args = sys.argv[3:]
state = json.loads((root / "state.json").read_text())
if command != "gh":
    assert "GH_TOKEN" not in os.environ and "GITHUB_TOKEN" not in os.environ
if command == "gh":
    assert args == ["api", "repos/wezterm/wezterm/releases/tags/nightly"], args
    print(json.dumps({"assets": state["assets"]}))
elif command == "curl":
    assert args[0] == "--disable", args
    urls = [arg for arg in args if arg.startswith("https://")]
    content = (root / "assets" / state["downloads"][urls[0]]).read_bytes()
    destination_flag = "--output" if "--output" in args else "-o"
    Path(args[args.index(destination_flag) + 1]).write_bytes(content)
else:
    print(state["metadata"][Path(args[-1]).name])
"""


class SnapshotTest(unittest.TestCase):
    """Run the actual CLI and substitute only the external tool boundaries."""

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.commands = self.root / "bin"
        self.commands.mkdir()
        tool = self.root / "fake_commands.py"
        tool.write_text(FAKE_COMMAND, encoding="utf-8")
        for command in ("gh", "curl", "dpkg-deb", "rpm"):
            wrapper = self.commands / command
            wrapper.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{tool}" "{self.root}" {command} "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
        self.manifest = self.root / "previous.lock"
        self.output = self.root / "published"
        self.state = {"assets": [], "downloads": {}, "metadata": {}}
        self.rows = []

    def cli(self, *args):
        self.manifest.write_text("\n".join(self.rows) + "\n", encoding="utf-8")
        (self.root / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env={
                **os.environ,
                "PATH": str(self.commands) + os.pathsep + os.environ["PATH"],
                "GH_TOKEN": "fixture-only",
                "GITHUB_TOKEN": "fixture-only",
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def capture(self, *extra):
        return self.cli(
            "capture",
            "--base-manifest",
            str(self.manifest),
            "--base-tag",
            BASE_TAG,
            "--tag",
            TAG,
            "--output",
            str(self.output),
            *extra,
        )

    def add_nightly(self, target, basename, metadata=None):
        url = f"{NIGHTLY}/{basename}"
        artifact = self.assets / basename
        if basename.endswith(".zip"):
            with zipfile.ZipFile(artifact, "w") as archive:
                for executable in ("wezterm", "wezterm-gui"):
                    archive.writestr(
                        f"WezTerm-macos-{NEW_VERSION}/WezTerm.app/Contents/MacOS/{executable}",
                        b"executable fixture",
                    )
        else:
            artifact.write_bytes(b"published archive fixture")
        checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.rows.append(f"{target} {OLD_VERSION} {OLD_SHA256} {url}")
        self.state["assets"].append(
            {"name": basename, "browser_download_url": url, "digest": f"sha256:{checksum}"}
        )
        self.state["downloads"][url] = basename
        if metadata is not None:
            self.state["metadata"][basename] = metadata
        return f"{target} {NEW_VERSION} {checksum} {url}"

    def add_arch(self):
        rows = []
        for basename in (ARCH_PACKAGE, ARCH_PACKAGE + ".sig"):
            data = basename.encode()
            (self.assets / basename).write_bytes(data)
            checksum = hashlib.sha256(data).hexdigest()
            row = f"arch-x86_64 {ARCH_VERSION} {checksum} {ARCH_MIRROR}/{basename}"
            self.rows.append(row)
            self.state["downloads"][f"{RELEASES}/{BASE_TAG}/{basename}"] = basename
            rows.append(row)
        return rows

    def add_rpms(self, *, gui_version=NEW_VERSION):
        rows = []
        for name in RPM_NAMES:
            version = gui_version if name == "wezterm-gui" else NEW_VERSION
            rpm_version = version.replace("-", "_")
            rows.append(
                self.add_nightly(
                    "centos9-x86_64",
                    f"{name}-nightly-centos9.rpm",
                    f"{name}\t0\t{rpm_version}\t1.centos9\tx86_64",
                )
            )
        return rows

    def test_capture_writes_verified_assets_and_publisher_manifest(self):
        rows = [self.add_nightly("macos", "WezTerm-macos-nightly.zip")]
        rows.extend(self.add_arch())
        rows.append(
            self.add_nightly(
                "debian12-arm64",
                "wezterm-nightly.Debian12.arm64.deb",
                f"wezterm-nightly\t{NEW_VERSION}\tarm64",
            )
        )
        rows.extend(self.add_rpms())
        result = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_rows("snapshot.lock"), rows)
        self.assertFalse((self.output / "dotfiles.lock").exists())
        self.assertEqual(
            self.read_rows("SHA256SUMS"),
            [f"{row.split()[2]}  {row.rsplit('/', 1)[1]}" for row in rows],
        )
        for row in rows:
            basename = row.rsplit("/", 1)[1]
            self.assertEqual(
                (self.output / basename).read_bytes(), (self.assets / basename).read_bytes()
            )

    def read_rows(self, filename):
        return [
            line
            for line in (self.output / filename).read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]

    def test_capture_rejects_changed_download_without_exposing_output(self):
        self.add_nightly("macos", "WezTerm-macos-nightly.zip")
        self.state["assets"][0]["digest"] = f"sha256:{OLD_SHA256}"
        result = self.capture()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(any(path.name.startswith(".snapshot-") for path in self.root.iterdir()))

    def test_capture_uses_prefetched_metadata_without_calling_gh(self):
        row = self.add_nightly("macos", "WezTerm-macos-nightly.zip")
        metadata = self.root / "nightly.json"
        metadata.write_text(json.dumps({"assets": self.state["assets"]}), encoding="utf-8")
        self.state["assets"] = []
        result = self.capture("--nightly-metadata", str(metadata))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_rows("snapshot.lock"), [row])

    def test_capture_rejects_mixed_rpm_versions(self):
        self.add_rpms(gui_version=OLD_VERSION)
        result = self.capture()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("centos9-x86_64", result.stderr)
        self.assertFalse(self.output.exists())

    def test_capture_rejects_an_incomplete_nightly_rpm_set(self):
        self.add_rpms()
        self.state["assets"].pop()
        result = self.capture()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wezterm-mux-server-nightly-centos9.rpm", result.stderr)
        self.assertFalse(self.output.exists())

    def test_capture_verifies_the_arch_signature_file_checksum(self):
        self.add_arch()
        (self.assets / (ARCH_PACKAGE + ".sig")).write_bytes(b"unexpected signature")
        result = self.capture()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256", result.stderr)
        self.assertIn(ARCH_PACKAGE + ".sig", result.stderr)
        self.assertFalse(self.output.exists())

    def test_capture_rejects_replaced_deb_package_identity(self):
        self.add_nightly(
            "debian12-arm64",
            "wezterm-nightly.Debian12.arm64.deb",
            f"wezterm\t{NEW_VERSION}\tamd64",
        )
        result = self.capture()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wezterm-nightly.Debian12.arm64.deb", result.stderr)
        self.assertFalse(self.output.exists())

    def test_capture_preserves_existing_output_directory(self):
        self.add_nightly("macos", "WezTerm-macos-nightly.zip")
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("existing data", encoding="utf-8")
        result = self.capture()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("output already exists", result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "existing data")
        self.assertEqual(list(self.output.iterdir()), [marker])

    def test_lock_rewrites_only_urls(self):
        self.add_nightly("macos", "WezTerm-macos-nightly.zip")
        result = self.cli("lock", "--manifest", str(self.manifest), "--tag", TAG)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [line for line in result.stdout.splitlines() if not line.startswith("#")]
        self.assertEqual(
            rows, [f"macos {OLD_VERSION} {OLD_SHA256} {RELEASES}/{TAG}/WezTerm-macos-nightly.zip"]
        )

    def test_lock_rejects_unsafe_tag(self):
        self.add_nightly("macos", "WezTerm-macos-nightly.zip")
        result = self.cli(
            "lock", "--manifest", str(self.manifest), "--tag", "snapshot-20261122/../x"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid snapshot tag", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
