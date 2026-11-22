# WezTerm desktop snapshots

This repository keeps reviewed sets of [upstream WezTerm](https://wezterm.org/)
packages available at versioned GitHub release URLs. The original bytes are
preserved, including the macOS app signature and Arch package signature. The
repositories that consume snapshots can advance or roll back their pins
independently of the upstream nightly release.

Each `snapshot-YYYYMMDD` release records the version, checksum, and original
publisher URL for every target in `snapshot.lock`. `SHA256SUMS` verifies the
package files. One release can contain different WezTerm builds because the
publisher builds each platform independently.

Keep published tags and files in place. When a set changes, use a new tag
such as `snapshot-YYYYMMDD` or `snapshot-YYYYMMDD-2`. There is no scheduled
capture and no automatic change to a consumer's pin.
Keep GitHub's [immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
enabled so existing snapshot URLs retain the same files.

## Capture a new set

1. Open **Actions → Capture WezTerm snapshot → Run workflow**. Enter an
   unused tag and leave `base_tag` empty to use the latest published snapshot.
   `base_tag` can also name a specific earlier release.
2. The workflow fetches the current official nightlies for the targets in the
   base manifest and verifies the publisher's checksums. It checks the
   package versions and carries forward the Arch package and its signature
   because Arch publishes its own build. The result is a draft release. Turn
   off `create_draft` for a verification run that leaves GitHub releases alone.
3. After the workflow succeeds and confirms the uploaded asset inventory and
   digests, review the embedded versions and test the candidates you will use.
   Confirm the draft targets the commit shown in the workflow summary, then
   publish it to make its URLs available to Shdeps. Consumers stay on their
   existing release until you repin.

The same workflow can be started from a shell:

```sh
gh workflow run snapshot.yml --repo cgraf78/wezterm-builds -f tag=snapshot-YYYYMMDD
```

After reviewing the draft, publish it from the Releases page or with:

```sh
gh release edit snapshot-YYYYMMDD --repo cgraf78/wezterm-builds --draft=false
```

If the publisher removes an asset or rotates it during capture, the workflow
stops before creating a draft. Rerun after the assets are available and the
split packages agree on a version.
If GitHub interrupts the upload, a partial draft may remain. Inspect or
discard that draft before retrying the same tag. The workflow refuses to
overwrite an existing draft, release, or tag.

## Move or roll back the dotfiles pin

Download `snapshot.lock` from the selected release and render the Shdeps lock
with its release URLs. Run these commands from this repository:

```sh
tag=snapshot-YYYYMMDD
stage=$(mktemp -d)
gh release download "$tag" --repo cgraf78/wezterm-builds --pattern snapshot.lock --dir "$stage"
python3 scripts/snapshot.py lock --manifest "$stage/snapshot.lock" --tag "$tag" > "$stage/wezterm-build.lock"
```

Replace `.config/shdeps/hooks.d/wezterm-build.lock` in the dotfiles checkout
with the generated file. Review and land that change, then run `dot update`.
To roll back, render the lock for an earlier snapshot or revert a commit
that moved the pin between snapshots. The older package must still be
compatible with the system. On Linux the ordinary installed check compares
package and executable versions; an explicit reinstall verifies the archive
checksum even when its version is already installed.

## Supporting script

`scripts/snapshot.py` is the workflow's capture and lock generation tool. It
requires Python 3.11 or newer, `gh`, `curl`, `dpkg-deb`, and `rpm` for a full
capture. On Linux, download the base release's `snapshot.lock`, then run:

```sh
mkdir -p dist
python3 scripts/snapshot.py capture \
  --base-manifest snapshot.lock \
  --base-tag snapshot-YYYYMMDD \
  --tag snapshot-YYYYMMDD-2 \
  --output dist/snapshot-YYYYMMDD-2
```

Use `python3 scripts/snapshot.py lock --manifest snapshot.lock --tag TAG` to
render only the consumer lock. Run the script's tests with
`python3 -m unittest discover -s tests`.

## Seed the archive or refresh the Arch base

The initial set uses [`snapshots/20261121.lock`](snapshots/20261121.lock).
Before creating its draft, download the original package files, verify them
against the manifest, and include the manifest as `snapshot.lock` with a
`SHA256SUMS` file for those bytes. Include the upstream license files listed
below. After upload, confirm the draft's tag target and compare every asset's
GitHub digest with the staged bytes before publication.
Verify the seed's Arch package signature with the official Arch keyring,
using the same command as for a later replacement.

Arch builds are published by Arch, so future nightly captures carry the
existing package and signature. To advance Arch, stage a candidate locally,
replace the old pair with the new packages from Arch, and verify the new
signature using `pacman-key --verify SIGNATURE PACKAGE` on an updated Arch
host with its official keyring. Update the two manifest rows and checksums,
then stage a new release and use it as the next base. Shdeps continues to
verify the exact bytes and passes the detached signature to pacman.

## Upstream notices

WezTerm is distributed under the [MIT license](LICENSE.md). Bundled font
notices are in [licenses/](licenses/). See upstream [WezTerm releases](https://github.com/wezterm/wezterm/releases)
and the [Arch package](https://archlinux.org/packages/extra/x86_64/wezterm/)
for their respective sources.
