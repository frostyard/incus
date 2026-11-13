#!/usr/bin/env python3
"""
Generate builds-docker.yml from builds.yml.

Transforms the upstream multi-arch matrix build workflow into amd64 builds
for Debian Trixie (13) and Debian Forky (14), each running in a Docker
container on standard GitHub-hosted runners. Builds of stable are then
released on GitHub and published by frostyard/apt-publisher (see
RELEASE_JOB_LINES for trixie and FORKY_RELEASE_JOB_LINES for forky).

Run this whenever builds.yml is updated to sync changes:
    python3 sync-docker-build.py
"""

import os
import re
import sys


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(base_dir, '.github', 'workflows', 'builds.yml')
    dst = os.path.join(base_dir, '.github', 'workflows', 'builds-docker.yml')

    with open(src) as f:
        src_lines = f.read().splitlines()

    env_lines = extract_env_block(src_lines)
    steps = extract_steps(src_lines)
    steps = reorder_steps(steps)

    out = generate(env_lines, steps)

    with open(dst, 'w') as f:
        f.write('\n'.join(out) + '\n')

    print(f'Generated {os.path.relpath(dst, base_dir)}')


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def extract_env_block(lines):
    """Extract env var lines between 'env:' and 'steps:' in the job."""
    result = []
    in_env = False
    for line in lines:
        if re.match(r'    env:$', line):
            in_env = True
            continue
        if in_env:
            if re.match(r'    steps:$', line):
                break
            result.append(line)
    return result


def extract_steps(lines):
    """Extract steps as list of (name, [lines])."""
    steps = []
    in_steps = False
    name = None
    step_lines = []

    for line in lines:
        if re.match(r'    steps:$', line):
            in_steps = True
            continue
        if not in_steps:
            continue

        m = re.match(r'      - name: (.+)', line)
        if m:
            if name is not None:
                steps.append((name, _strip_trailing_blanks(step_lines)))
            name = m.group(1)
            step_lines = [line]
        elif name is not None:
            step_lines.append(line)

    if name is not None:
        steps.append((name, _strip_trailing_blanks(step_lines)))

    return steps


def reorder_steps(steps):
    """Move 'Configure git' after 'Install dependencies' for Docker."""
    names = [n for n, _ in steps]
    by_name = {n: l for n, l in steps}

    if 'Configure git' in names and 'Install dependencies' in names:
        names.remove('Configure git')
        idx = names.index('Install dependencies')
        names.insert(idx + 1, 'Configure git')

    return [(n, by_name[n]) for n in names]


def _strip_trailing_blanks(lines):
    while lines and lines[-1].strip() == '':
        lines.pop()
    return lines


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

# The build targets: 1 job per Debian release, all emitted from the same
# list of upstream steps. The trixie entry must stay exactly as it is: its job
# id, name and every line of it are a required status check on the default
# branch. forky is emitted after trixie and before the release jobs.
TARGETS = [
    {
        'job_id': 'build-incus',
        'name': 'Build Incus (Debian Trixie)',
        'image': 'debian:trixie',
        'os_name': 'debian-13',
        'artifact': 'debian-trixie-amd64',
        'forky': False,
    },
    {
        'job_id': 'build-incus-forky',
        'name': 'Build Incus (Debian Forky)',
        'image': 'debian:forky',
        'os_name': 'debian-14',
        'artifact': 'debian-forky-amd64',
        'forky': True,
    },
]


def generate(env_lines, steps):
    """Generate the full builds-docker.yml content."""
    # A forky rewrite whose pattern has vanished from builds.yml would
    # silently produce a wrong forky build, so each one fails loudly here
    # instead. This is the check for the ubuntu-26.04 -> debian-14 rewrite of
    # the `if` operands; the others live in the step transformers below.
    if _forky_os_operand_count(steps) == 0:
        sys.exit('error: builds.yml has no `if ` shell line with the '
                 '"ubuntu-26.04" operand and no `if:` line with the '
                 "'ubuntu-26.04' operand, so the Debian 14 (forky) build "
                 'cannot be derived. Update sync-docker-build.py.')

    out = []

    # Header
    out.extend([
        'name: Build (Docker)',
        'on:',
        '  push:',
        '    branches: [stable]',
        '  pull_request:',
        '  workflow_dispatch:',
        '',
        'permissions:',
        '  contents: read',
        '',
        'concurrency:',
        '  group: docker-${{ github.ref }}',
        '  cancel-in-progress: true',
        '',
        'jobs:',
    ])

    # 1 build job per target, in the order of TARGETS.
    for target in TARGETS:
        _emit_build_job(out, env_lines, steps, target)

    # Release jobs: trixie first, then forky, 1 blank line apart.
    out.extend(RELEASE_JOB_LINES)
    out.append('')
    out.extend(FORKY_RELEASE_JOB_LINES)

    # The Docker build runs as root (HOME=/root), but upstream's runner
    # setup hardcodes /home/runner in PATH and tool install prefixes
    # (pipx meson, rustup cargo, npm yarn). Point them all at /root so the
    # installed binaries are actually on PATH.
    out = [line.replace('/home/runner/', '/root/') for line in out]

    # Pin the build job's actions, ours and upstream's, to commit SHAs.
    out = [pin_action(line) for line in out]

    return out


def _emit_build_job(out, env_lines, steps, target):
    """Append 1 build job for a target to `out`."""
    out.extend([
        f"  {target['job_id']}:",
        f"    name: {target['name']}",
        '    runs-on: ubuntu-latest',
        '    container:',
        f"      image: {target['image']}",
        '',
        '    env:',
        '      OS_ARCH: "amd64"',
        '      OS_NAME: "%s"' % target['os_name'],
        # The container runs as root; upstream dropped HOME for its
        # /home/runner runner setup. Pin it so pipx/rustup/npm install
        # under /root, matching the rewritten PATH below.
        '      HOME: "/root/"',
    ])

    # Env vars (skip OS_ARCH/OS_NAME, already hardcoded above)
    for line in env_lines:
        s = line.strip()
        if s.startswith('OS_ARCH:') or s.startswith('OS_NAME:'):
            continue
        out.append(line)

    # Steps
    out.append('')
    out.append('    steps:')

    # Checkout
    out.extend([
        '      - name: Checkout code',
        '        uses: actions/checkout@v4',
        '        with:',
        '          persist-credentials: false',
        '',
    ])

    # Docker-only: Migrate to /usr/incus
    out.extend([
        '      - name: Migrate to /usr/incus',
        '        run: ./migrate-to-usr-incus.sh',
        '',
    ])

    # Process upstream steps
    for name, step_lines in steps:
        if name == 'Checkout code':
            continue

        transformed = transform_step(name, step_lines, target)
        out.extend(transformed)
        out.append('')


# Floating tags used by the build job, pinned to full commit SHAs
# (frostyard/core ADR-0021). Each SHA is the commit the floating tag pointed
# at when it was pinned, so pinning changed nothing that runs.
PINNED_ACTIONS = {
    'actions/checkout@v4':
        'actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0',
    'actions/upload-artifact@v4':
        'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2',
}


def pin_action(line):
    """Replace a floating `uses:` reference listed in PINNED_ACTIONS."""
    m = re.match(r'(\s+(?:- )?uses: )(\S+)\s*$', line)
    if m and m.group(2) in PINNED_ACTIONS:
        return m.group(1) + PINNED_ACTIONS[m.group(2)]
    return line


# ---------------------------------------------------------------------------
# Step transformers
# ---------------------------------------------------------------------------

STEP_TRANSFORMS = {}


def transformer(name):
    """Decorator to register a step transformer."""
    def decorator(fn):
        STEP_TRANSFORMS[name] = fn
        return fn
    return decorator


def transform_step(name, lines, target):
    """Apply the transformation for a step and target, or pass through."""
    # Fix upstream typo
    if name == 'Build libtmps':
        lines = [l.replace('Build libtmps', 'Build libtpms') if '- name:' in l else l
                 for l in lines]
        name = 'Build libtpms'

    if name in STEP_TRANSFORMS:
        lines = STEP_TRANSFORMS[name](lines, target)

    # The Docker build runs as root inside the container; sudo isn't
    # installed (and isn't needed). Upstream added sudo for its non-root
    # runner setup, so strip it from every command line.
    lines = [strip_sudo(l) for l in lines]

    # The Docker workflow has no matrix, so `matrix.os`/`matrix.arch` in
    # step-level `if:` conditions evaluate to empty and silently skip the
    # step. Rewrite them against the pinned OS_NAME/OS_ARCH env vars.
    lines = [l.replace('matrix.os', 'env.OS_NAME').replace('matrix.arch', 'env.OS_ARCH')
             if l.strip().startswith('if:') else l
             for l in lines]

    # Debian forky has the same library generation as Ubuntu 26.04, so the
    # forky build runs the ubuntu-26.04 branches of every conditional: same
    # libgpgme45, libnet9 and libfuse3-4 names, the same t64 package names,
    # the same virglrenderer build and its libdrm/libegl/libepoxy/libgbm
    # Depends. Point those operands at debian-14, which the forky build pins
    # as OS_NAME and PKGOS.
    if target['forky']:
        lines = _forky_rewrite_os(lines)

    return lines


def _forky_rewrite_os(lines):
    """Rewrite the ubuntu-26.04 operands of `if` and `if:` lines to debian-14.

    Only lines that start with `if ` (shell) or `if:` (step condition) are
    rewritten. The CODENAME table lines (`[ "${PKGOS}" = ... ] && CODENAME=...`)
    start with `[` and keep every branch verbatim.
    """
    result = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith('if '):
            result.append(line.replace('"ubuntu-26.04"', '"debian-14"'))
        elif stripped.startswith('if:'):
            result.append(line.replace("'ubuntu-26.04'", "'debian-14'"))
        else:
            result.append(line)
    return result


def _forky_os_operand_count(steps):
    """Count the ubuntu-26.04 operands `_forky_rewrite_os` rewrites."""
    count = 0
    for _, step_lines in steps:
        for line in step_lines:
            stripped = line.lstrip()
            if stripped.startswith('if ') and '"ubuntu-26.04"' in line:
                count += 1
            elif stripped.startswith('if:') and "'ubuntu-26.04'" in line:
                count += 1
    return count


def strip_sudo(line):
    """Drop `sudo ` command prefixes (start of line or after a pipe/operator)."""
    return re.sub(r'(?<![\w-])sudo\s+', '', line)


@transformer('Install dependencies')
def _transform_install_deps(lines, target):
    """Add awscli and jq packages, use --break-system-packages first."""
    result = []
    rpcgen_added = False
    for line in lines:
        result.append(line)
        # Insert awscli after automake (alphabetical order)
        if re.match(r'\s+automake \\\s*$', line):
            indent = line[:len(line) - len(line.lstrip())]
            result.append(f'{indent}awscli \\')
        # Insert jq after iproute2
        if re.match(r'\s+iproute2 \\\s*$', line):
            indent = line[:len(line) - len(line.lstrip())]
            result.append(f'{indent}jq \\')
        # Insert python3-yaml after python3-venv: virglrenderer's meson
        # build needs it, and unlike upstream's Ubuntu runner image the
        # bare debian:trixie container doesn't ship it.
        if re.match(r'\s+python3-venv \\\s*$', line):
            indent = line[:len(line) - len(line.lstrip())]
            result.append(f'{indent}python3-yaml \\')
            # Insert rpcsvc-proto after it for forky: libnvidia-container
            # runs rpcgen. In trixie, libc6-dev depends on rpcsvc-proto, which
            # ships rpcgen; in forky it no longer does.
            if target['forky']:
                result.append(f'{indent}rpcsvc-proto \\')
                rpcgen_added = True

    if target['forky'] and not rpcgen_added:
        sys.exit('error: builds.yml has no `python3-venv \\` line in the '
                 '`Install dependencies` step, so the Debian 14 (forky) build '
                 'cannot add rpcsvc-proto (rpcgen). Update sync-docker-build.py.')

    # Swap pip install order: try --break-system-packages first (Trixie needs it)
    final = []
    for line in result:
        if re.match(r'\s+pip3 install meson \|\|', line):
            indent = line[:len(line) - len(line.lstrip())]
            final.append(f'{indent}pip3 install meson --break-system-packages || pip3 install meson')
        elif re.match(r'\s+pip3 install tomli \|\|', line):
            indent = line[:len(line) - len(line.lstrip())]
            final.append(f'{indent}pip3 install tomli --break-system-packages || pip3 install tomli')
        else:
            final.append(line)

    return final


@transformer('Install Node')
def _transform_install_node(lines, target):
    """Simplify for amd64 only (hardcode x64)."""
    node_ver = None
    for line in lines:
        m = re.search(r'nodejs\.org/dist/(v[\d.]+)/', line)
        if m:
            node_ver = m.group(1)
            break

    if not node_ver:
        return [l.replace('${NODE_ARCH}', 'x64') for l in lines
                if 'OS_ARCH' not in l]

    return [
        '      - name: Install Node',
        '        run: |',
        '          mkdir /usr/local/node/',
        f'          curl -sL "https://nodejs.org/dist/{node_ver}/node-{node_ver}-linux-x64.tar.xz" | tar -C /usr/local/node/ -Jx --strip-components=1',
    ]


@transformer('Build Incus')
def _transform_build_incus(lines, target):
    """Simplify agents for amd64 only, comment out doc build."""
    result = []
    in_arch_if = False
    in_x86_block = False
    in_aarch64_block = False

    for line in lines:
        s = line.strip()

        # Detect arch conditional for agents
        if 'if [ "$(uname -m)" = "x86_64" ]; then' in line:
            in_arch_if = True
            in_x86_block = True
            continue
        if in_arch_if and 'elif [ "$(uname -m)" = "aarch64" ]; then' in line:
            in_x86_block = False
            in_aarch64_block = True
            continue
        if in_arch_if and s == 'fi':
            in_arch_if = False
            in_x86_block = False
            in_aarch64_block = False
            continue

        if in_aarch64_block:
            continue

        if in_x86_block:
            # De-indent by 4 spaces (remove if-body indent)
            if line.startswith('              '):
                line = '          ' + line[14:]
            result.append(line)
            continue

        # Comment out doc build
        if s == 'make doc':
            indent = len(line) - len(line.lstrip())
            result.append(' ' * indent + '#' + s)
            continue
        if s == 'cp -R doc/html /usr/incus/doc':
            indent = len(line) - len(line.lstrip())
            result.append(' ' * indent + '#' + s)
            continue

        result.append(line)

    return result


@transformer('Build seabios')
def _transform_build_seabios(lines, target):
    """Remove arch condition (always amd64 in Docker)."""
    return [l for l in lines
            if not (l.strip().startswith('if:') and 'matrix.arch' in l)]


@transformer('Build EDK2')
def _transform_build_edk2(lines, target):
    """Remove aarch64 conditional blocks."""
    return _remove_uname_aarch64_blocks(lines)


@transformer('Build Secure Boot firmware')
def _transform_build_secure_boot(lines, target):
    """Remove aarch64 firmware detection."""
    return _remove_uname_aarch64_blocks(lines)


@transformer('Build QEMU')
def _transform_build_qemu(lines, target):
    """Hardcode x86_64 target."""
    return [l.replace('$(uname -m)-softmmu', 'x86_64-softmmu') for l in lines]


@transformer('Make a Debian package')
def _transform_make_debian_package(lines, target):
    """Pin PKGOS to the target's Debian release and apply the forky rewrites.

    builds-docker.yml builds 1 Debian release per job, so rather than trying
    to collapse the per-release ${PKGOS} conditionals down to a single branch
    (brittle: the match patterns drift out of sync with builds.yml, and the
    flat parser can't handle nested if/else -- which silently leaked the
    bookworm branch and shipped libfuse3-3/libgpgme11 into a Trixie package),
    we keep every conditional verbatim and just pin the matrix-provided PKGOS
    to the target's constant. Each conditional then resolves to the correct
    branch and any future edits to builds.yml flow through unchanged: pinning
    debian-13 selects the Trixie branches (libfuse3-4, libgpgme11t64, libnet1,
    CODENAME=trixie, the debian13 version tag), pinning debian-14 selects the
    ubuntu-26.04 branches (libgpgme45, libnet9, libfuse3-4, the t64 names) and
    the forky additions below.
    """
    os_name = target['os_name']
    result = []
    pinned = False
    for line in lines:
        # Pin the matrix-provided PKGOS to the target's fixed value.
        if line.strip() == 'PKGOS: ${{ matrix.os }}':
            result.append(line.replace('${{ matrix.os }}', '"%s"' % os_name))
            pinned = True
            continue
        result.append(line)

    if not target['forky']:
        return result

    if not pinned:
        sys.exit('error: builds.yml has no `PKGOS: ${{ matrix.os }}` line in '
                 'the `Make a Debian package` step, so the Debian 14 (forky) '
                 'build cannot pin PKGOS to debian-14. Update '
                 'sync-docker-build.py.')

    return _forky_make_debian_package(result)


def _forky_make_debian_package(lines):
    """Add the forky codename and the ~deb14 version tag to the Trixie steps.

    The version carries ~debNN because frostyard/core ADR-0055 publishes a
    ~debNN version only to the codename for Debian NN, and core Plan 0009
    Phase 6 (docs/plans/0009-debian-publication-through-apt-publisher.md in
    frostyard/core) adds forky beside trixie that way: a forky build can sit
    beside the trixie build of the same upstream version.
    """
    trixie_codename = '[ "${PKGOS}" = "debian-13" ] && CODENAME=trixie'
    forky_codename = '[ "${PKGOS}" = "debian-14" ] && CODENAME=forky'
    stamp = '$(date -u +%Y%m%d%H%M)'

    # CODENAME: add the forky branch directly after the trixie one.
    with_codename = []
    codename_done = False
    for line in lines:
        with_codename.append(line)
        if line.strip() == trixie_codename:
            indent = line[:len(line) - len(line.lstrip())]
            with_codename.append(indent + forky_codename)
            codename_done = True
    if not codename_done:
        sys.exit('error: builds.yml has no `%s` line in the `Make a Debian '
                 'package` step, so the Debian 14 (forky) CODENAME cannot be '
                 'added. Update sync-docker-build.py.' % trixie_codename)

    # Version: append the ~deb14 marker the package version needs (ADR-0055).
    result = []
    dch_done = False
    for line in with_codename:
        if 'dch ' in line and '--force-distribution' in line and stamp in line:
            result.append(line.replace(stamp, stamp + '~deb14'))
            dch_done = True
            continue
        result.append(line)
    if not dch_done:
        sys.exit('error: builds.yml has no `dch ... %s --force-distribution` '
                 'line in the `Make a Debian package` step, so the Debian 14 '
                 '(forky) version cannot get its ~deb14 marker. Update '
                 'sync-docker-build.py.' % stamp)

    return result


@transformer('Upload resulting build')
def _transform_upload(lines, target):
    """Change artifact name, remove continue-on-error."""
    artifact = target['artifact']
    result = []
    named = False
    for line in lines:
        if 'continue-on-error' in line:
            continue
        if 'name:' in line and ('matrix.os' in line or 'matrix.arch' in line):
            indent = line[:len(line) - len(line.lstrip())]
            result.append('%sname: %s' % (indent, artifact))
            named = True
            continue
        result.append(line)

    if target['forky'] and not named:
        sys.exit('error: builds.yml has no `${{ matrix.os }}-${{ matrix.arch }}` '
                 'artifact name in the `Upload resulting build` step, so the '
                 'Debian 14 (forky) artifact cannot be named %s. Update '
                 'sync-docker-build.py.' % artifact)

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _remove_uname_aarch64_blocks(lines):
    """Remove if [ "$(uname -m)" = "aarch64" ] ... fi blocks and preceding blank lines."""
    result = []
    skip_depth = 0

    for line in lines:
        s = line.strip()

        if 'if [ "$(uname -m)" = "aarch64" ]' in line:
            skip_depth += 1
            # Remove blank line that preceded this block
            if result and result[-1].strip() == '':
                result.pop()
            continue

        if skip_depth > 0:
            if s == 'fi':
                skip_depth -= 1
                continue
            continue

        result.append(line)

    return result


# ---------------------------------------------------------------------------
# Release jobs (Docker-only, hardcoded)
# ---------------------------------------------------------------------------

# Only pushes to (or runs dispatched on) stable in frostyard/incus release.
# Every other branch, and any tag, stops after the builds.
#
# There are 2 releases, 1 for each Debian release the workflow builds: release
# and request-publication for trixie (Debian 13), then release-forky and
# request-publication-forky for forky (Debian 14). They are separate jobs so
# that a forky failure never blocks the trixie release: the 2 releases share
# no job, and a broken forky build fails only the forky jobs.
#
# The Debian packages are published by frostyard/apt-publisher, the single
# Debian writer (frostyard/core ADR-0055): this workflow attaches them to a
# GitHub release and requests publication. apt-publisher publishes every .deb
# asset of the release, then dispatches `build` to frostyard/snosi once the
# packages are installable (ADR-0056), so nothing here dispatches snosi.
#
# These builds replace Debian's incus packages, so apt-publisher publishes
# them in the `image` component, which only Frostyard image builds enable
# (core ADR-0059). apt-publisher sets the component from its registration of
# this repository; the publish-deb payload names none.
#
# The release tag is the package version without its epoch, for example
# 7.5.1-debian13-202610031200 (and, for forky, without the ~deb14 suffix too:
# 7.6-debian14-202611131200). The build job stamps the version with the UTC
# minute, so every build gets a new tag. Each request names its codename
# explicitly because the trixie version carries no ~deb13 marker.
RELEASE_JOB_LINES = r"""
  release:
    if: github.repository == 'frostyard/incus' && github.ref == 'refs/heads/stable'
    runs-on: ubuntu-latest
    needs: build-incus
    timeout-minutes: 20
    permissions:
      contents: write # create the release and its tag
      attestations: write # store the build provenance attestations
      id-token: write # sign the attestations with this workflow's identity
    outputs:
      tag: ${{ steps.check.outputs.tag }}

    steps:
      - name: Download Artifact
        uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
        with:
          name: debian-trixie-amd64
          path: ./dist

      # apt-publisher publishes every .deb in the release to the codenames
      # the request names, in the image component, and this Debian 13 version
      # carries no ~deb13 marker. The release must therefore hold exactly one
      # complete Debian 13 amd64 build, and nothing else.
      - name: Check the packages
        id: check
        run: |
          set -euo pipefail
          shopt -s nullglob
          want="incus incus-base incus-client incus-extra incus-ui-canonical"
          version="" names=()
          for deb in dist/*.deb; do
            name=$(dpkg-deb -f "$deb" Package)
            arch=$(dpkg-deb -f "$deb" Architecture)
            v=$(dpkg-deb -f "$deb" Version)
            size=$(stat -c %s "$deb")
            if [[ $arch != amd64 ]]; then
              echo "::error::$deb is built for $arch, not amd64"
              exit 1
            fi
            if [[ ! $v =~ ^1:[0-9][0-9.]*-debian13-[0-9]{12}$ ]]; then
              echo "::error::$deb has version $v, which is not a Debian 13 build"
              exit 1
            fi
            if [[ -n $version && $v != "$version" ]]; then
              echo "::error::$deb has version $v, but another package has $version"
              exit 1
            fi
            if ((size >= 2147483648)); then
              echo "::error::$deb is $size bytes; GitHub release assets must be under 2 GiB"
              exit 1
            fi
            version=$v
            names+=("$name")
          done
          got=$(printf '%s\n' "${names[@]}" | sort | paste -sd ' ')
          if [[ $got != "$want" ]]; then
            echo "::error::expected the packages: $want; found: ${got:-none}"
            exit 1
          fi
          echo "tag=${version#1:}" >>"$GITHUB_OUTPUT"

      # Binds each .deb to this workflow run. Verify with
      # gh attestation verify FILE --repo frostyard/incus --source-ref refs/heads/stable
      - name: Attest build provenance
        uses: actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2
        with:
          subject-path: dist/*.deb

      # gh uploads the assets to a draft and publishes it once all are
      # attached, so apt-publisher never sees a partial release. An existing
      # tag fails this step instead of changing a published release.
      - name: Create the GitHub release
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          TAG: ${{ steps.check.outputs.tag }}
        run: |
          gh release create "$TAG" dist/*.deb --target "$GITHUB_SHA" --latest \
            --title "Incus $TAG" \
            --notes "Debian 13 (trixie) amd64 build of stable at $GITHUB_SHA. frostyard/apt-publisher publishes it to https://repository.frostyard.org/debian/ (trixie, component image)."

  # Not continue-on-error: if the request fails, nothing was published and
  # the run must fail. Re-running this job alone requests the same release
  # again. codenames is explicit because these are Debian 13 builds whose
  # version carries no ~deb13 marker. The payload names no component:
  # apt-publisher puts this repository's packages in the component it is
  # registered for, image (core ADR-0059).
  request-publication:
    runs-on: ubuntu-latest
    needs: release
    timeout-minutes: 5
    permissions: {}

    steps:
      - name: Request APT publication
        uses: peter-evans/repository-dispatch@28959ce8df70de7be546dd1250a005dd32156697 # v4
        with:
          token: ${{ secrets.APT_PUBLISH_TOKEN }}
          repository: frostyard/apt-publisher
          event-type: publish-deb
          client-payload: '{"repo": "${{ github.repository }}", "tag": "${{ needs.release.outputs.tag }}", "codenames": ["trixie"]}'
""".strip('\n').split('\n')

# The Debian 14 (forky) release, beside the trixie one above and separate from
# it so that a forky failure never blocks the trixie release: these jobs share
# no job with release and request-publication, and a forky build that fails or
# produces the wrong packages fails only the forky jobs.
#
# The version carries the ~deb14 marker, so apt-publisher publishes it to the
# forky codename only (core ADR-0055, core Plan 0009 Phase 6,
# docs/plans/0009-debian-publication-through-apt-publisher.md in
# frostyard/core). The request still names `forky` explicitly: the trixie
# version carries no ~deb13 marker, so a payload that named no codename would
# have no way to tell the 2 releases apart. The payload names no component:
# apt-publisher puts this repository's packages in the component it is
# registered for, image (core ADR-0059).
FORKY_RELEASE_JOB_LINES = r"""
  # The Debian 14 (forky) release: separate from release so that a forky
  # failure never blocks the trixie release.
  release-forky:
    if: github.repository == 'frostyard/incus' && github.ref == 'refs/heads/stable'
    runs-on: ubuntu-latest
    needs: build-incus-forky
    timeout-minutes: 20
    permissions:
      contents: write # create the release and its tag
      attestations: write # store the build provenance attestations
      id-token: write # sign the attestations with this workflow's identity
    outputs:
      tag: ${{ steps.check.outputs.tag }}

    steps:
      - name: Download Artifact
        uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
        with:
          name: debian-forky-amd64
          path: ./dist

      # apt-publisher publishes every .deb in the release, and it publishes a
      # ~deb14 version to forky only. The release must therefore hold exactly
      # one complete Debian 14 amd64 build, and nothing else.
      - name: Check the packages
        id: check
        run: |
          set -euo pipefail
          shopt -s nullglob
          want="incus incus-base incus-client incus-extra incus-ui-canonical"
          version="" names=()
          for deb in dist/*.deb; do
            name=$(dpkg-deb -f "$deb" Package)
            arch=$(dpkg-deb -f "$deb" Architecture)
            v=$(dpkg-deb -f "$deb" Version)
            size=$(stat -c %s "$deb")
            if [[ $arch != amd64 ]]; then
              echo "::error::$deb is built for $arch, not amd64"
              exit 1
            fi
            if [[ ! $v =~ ^1:[0-9][0-9.]*-debian14-[0-9]{12}~deb14$ ]]; then
              echo "::error::$deb has version $v, which is not a Debian 14 build"
              exit 1
            fi
            if [[ -n $version && $v != "$version" ]]; then
              echo "::error::$deb has version $v, but another package has $version"
              exit 1
            fi
            if ((size >= 2147483648)); then
              echo "::error::$deb is $size bytes; GitHub release assets must be under 2 GiB"
              exit 1
            fi
            version=$v
            names+=("$name")
          done
          got=$(printf '%s\n' "${names[@]}" | sort | paste -sd ' ')
          if [[ $got != "$want" ]]; then
            echo "::error::expected the packages: $want; found: ${got:-none}"
            exit 1
          fi
          # A git tag cannot contain ~, so the tag is the version without its
          # epoch and without the ~deb14 suffix, for example
          # 7.6-debian14-202611131200.
          v=${version#1:}
          echo "tag=${v%~deb14}" >>"$GITHUB_OUTPUT"

      # Binds each .deb to this workflow run. Verify with
      # gh attestation verify FILE --repo frostyard/incus --source-ref refs/heads/stable
      - name: Attest build provenance
        uses: actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2
        with:
          subject-path: dist/*.deb

      # --latest=false: the trixie release stays the latest, because trixie
      # stays the default codename.
      # gh uploads the assets to a draft and publishes it once all are
      # attached, so apt-publisher never sees a partial release. An existing
      # tag fails this step instead of changing a published release.
      - name: Create the GitHub release
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          TAG: ${{ steps.check.outputs.tag }}
        run: |
          gh release create "$TAG" dist/*.deb --target "$GITHUB_SHA" --latest=false \
            --title "Incus $TAG" \
            --notes "Debian 14 (forky) amd64 build of stable at $GITHUB_SHA. frostyard/apt-publisher publishes it to https://repository.frostyard.org/debian/ (forky, component image)."

  # Not continue-on-error: if the request fails, nothing was published and
  # the run must fail. Re-running this job alone requests the same release
  # again. codenames names forky explicitly because the trixie version
  # carries no ~deb13 marker; the forky version's ~deb14 marker is what
  # publishes it to forky alone. The payload names no component:
  # apt-publisher puts this repository's packages in the component it is
  # registered for, image (core ADR-0059).
  request-publication-forky:
    runs-on: ubuntu-latest
    needs: release-forky
    timeout-minutes: 5
    permissions: {}

    steps:
      - name: Request APT publication
        uses: peter-evans/repository-dispatch@28959ce8df70de7be546dd1250a005dd32156697 # v4
        with:
          token: ${{ secrets.APT_PUBLISH_TOKEN }}
          repository: frostyard/apt-publisher
          event-type: publish-deb
          client-payload: '{"repo": "${{ github.repository }}", "tag": "${{ needs.release-forky.outputs.tag }}", "codenames": ["forky"]}'
""".strip('\n').split('\n')

if __name__ == '__main__':
    main()
