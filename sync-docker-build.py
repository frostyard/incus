#!/usr/bin/env python3
"""
Generate builds-docker.yml from builds.yml.

Transforms the upstream multi-arch matrix build workflow into a single
Debian Trixie amd64 build running in a Docker container on standard
GitHub-hosted runners.

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

def generate(env_lines, steps):
    """Generate the full builds-docker.yml content."""
    out = []

    # Header
    out.extend([
        'name: Build (Docker)',
        'on:',
        '  push:',
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
        '  build-incus:',
        '    name: Build Incus (Debian Trixie)',
        '    runs-on: ubuntu-latest',
        '    container:',
        '      image: debian:trixie',
        '',
        '    env:',
        '      OS_ARCH: "amd64"',
        '      OS_NAME: "debian-13"',
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

        transformed = transform_step(name, step_lines)
        out.extend(transformed)
        out.append('')

    # Release job
    out.extend(RELEASE_JOB_LINES)

    return out


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


def transform_step(name, lines):
    """Apply transformation for a step, or pass through."""
    # Fix upstream typo
    if name == 'Build libtmps':
        lines = [l.replace('Build libtmps', 'Build libtpms') if '- name:' in l else l
                 for l in lines]
        name = 'Build libtpms'

    if name in STEP_TRANSFORMS:
        return STEP_TRANSFORMS[name](lines)
    return lines


@transformer('Install dependencies')
def _transform_install_deps(lines):
    """Add awscli and jq packages, use --break-system-packages first."""
    result = []
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
def _transform_install_node(lines):
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
def _transform_build_incus(lines):
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
def _transform_build_seabios(lines):
    """Remove arch condition (always amd64 in Docker)."""
    return [l for l in lines
            if not (l.strip().startswith('if:') and 'matrix.arch' in l)]


@transformer('Build EDK2')
def _transform_build_edk2(lines):
    """Remove aarch64 conditional blocks."""
    return _remove_uname_aarch64_blocks(lines)


@transformer('Build Secure Boot firmware')
def _transform_build_secure_boot(lines):
    """Remove aarch64 firmware detection."""
    return _remove_uname_aarch64_blocks(lines)


@transformer('Build QEMU')
def _transform_build_qemu(lines):
    """Hardcode x86_64 target."""
    return [l.replace('$(uname -m)-softmmu', 'x86_64-softmmu') for l in lines]


@transformer('Make a Debian package')
def _transform_make_debian_package(lines):
    """Simplify for Trixie only (no matrix, no PKGOS)."""
    result = []
    i = 0
    codename_emitted = False

    while i < len(lines):
        line = lines[i]
        s = line.strip()

        # Remove the env: PKGOS block (step-level property)
        if s == 'env:' and line.startswith('        '):
            i += 1
            # Skip env sub-properties
            while i < len(lines) and lines[i].startswith('          ') and ':' in lines[i]:
                i += 1
            continue

        # Replace codename detection lines ([ "${PKGOS}" = ... ] && CODENAME=...)
        if 'PKGOS' in line and '&& CODENAME=' in line:
            if not codename_emitted:
                result.append('          CODENAME=trixie')
                codename_emitted = True
            i += 1
            continue

        # First if block: t64 library names for debian-13/ubuntu-24.04
        if ('if [ "${PKGOS}" = "debian-13" ] || [ "${PKGOS}" = "ubuntu-24.04" ]' in line):
            i += 1
            # Extract the if-body (trixie-compatible branch)
            body = []
            while i < len(lines) and lines[i].strip() not in ('else', 'fi'):
                body.append(lines[i])
                i += 1
            # Skip else block
            if i < len(lines) and lines[i].strip() == 'else':
                i += 1
                while i < len(lines) and lines[i].strip() != 'fi':
                    i += 1
            # Skip fi
            if i < len(lines) and lines[i].strip() == 'fi':
                i += 1
            # De-indent body by 2 spaces (if-body -> base level)
            for bl in body:
                if bl.startswith('            '):
                    result.append('          ' + bl[12:])
                else:
                    result.append(bl)
            continue

        # Second if block: LIBFUSE3 for debian-13
        if 'if [ "${PKGOS}" = "debian-13" ]; then' in line:
            i += 1
            body = []
            while i < len(lines) and lines[i].strip() not in ('else', 'fi'):
                body.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].strip() == 'else':
                i += 1
                while i < len(lines) and lines[i].strip() != 'fi':
                    i += 1
            if i < len(lines) and lines[i].strip() == 'fi':
                i += 1
            for bl in body:
                if bl.startswith('            '):
                    result.append('          ' + bl[12:])
                else:
                    result.append(bl)
            continue

        # Replace dch version: $(echo ${PKGOS} | sed "s/-//g") -> debian13
        if 'dch --package incus' in line:
            line = line.replace(
                '$(echo ${PKGOS} | sed "s/-//g")',
                'debian13'
            )

        result.append(line)
        i += 1

    return result


@transformer('Upload resulting build')
def _transform_upload(lines):
    """Change artifact name, remove continue-on-error."""
    result = []
    for line in lines:
        if 'continue-on-error' in line:
            continue
        if 'name:' in line and ('matrix.os' in line or 'matrix.arch' in line):
            indent = line[:len(line) - len(line.lstrip())]
            result.append(f'{indent}name: debian-trixie-amd64')
            continue
        result.append(line)
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
# Release job (Docker-only, hardcoded)
# ---------------------------------------------------------------------------

RELEASE_JOB_LINES = [
    '  release:',
    '    runs-on: ubuntu-latest',
    '    needs: build-incus',
    '    permissions:',
    '      contents: write # to create and upload assets to releases',
    '      attestations: write # to upload assets attestation for build provenance',
    '      id-token: write # grant additional permission to attestation action to mint the OIDC token permission',
    '',
    '    steps:',
    '      - name: Checkout',
    '        uses: actions/checkout@v4',
    '        with:',
    '          fetch-depth: 0',
    '',
    '      - name: Download Artifact',
    '        uses: actions/download-artifact@v4',
    '        with:',
    '          name: debian-trixie-amd64',
    '          path: ./dist',
    '',
    '      - name: Publish to frostyard repo',
    '        uses: frostyard/repogen/.github/actions/publish-to-r2@main',
    '        with:',
    '          r2-account-id: ${{ secrets.R2_ACCOUNT_ID }}',
    '          r2-access-key-id: ${{ secrets.R2_ACCESS_KEY_ID }}',
    '          r2-secret-access-key: ${{ secrets.R2_SECRET_ACCESS_KEY }}',
    '          r2-bucket: frostyardrepo',
    '          purge-cache: "true"',
    '          cloudflare-zone: ${{ secrets.CLOUDFLARE_ZONE }}',
    '          cloudflare-api-token: ${{ secrets.CLOUDFLARE_API_TOKEN }}',
    '          gpg-private-key: ${{ secrets.REPOGEN_GPG_KEY }}',
    '          packages-dir: ./dist',
    '          package-type: deb # or sysext',
    '          base-url: https://repository.frostyard.org # required for sysext',
    '',
    '      - name: Kickoff snosi',
    "        if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
    '        continue-on-error: true',
    '        uses: peter-evans/repository-dispatch@v4',
    '        with:',
    '          token: ${{ secrets.ORG_PAT }}',
    '          repository: frostyard/snosi',
    '          event-type: build',
]


if __name__ == '__main__':
    main()
