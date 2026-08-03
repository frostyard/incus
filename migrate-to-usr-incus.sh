#!/bin/bash
# migrate-to-usr-incus.sh
#
# Migrates the Incus Debian package configuration from /opt/incus to /usr/incus
# for systemd-sysext compatibility.
#
# Usage: ./migrate-to-usr-incus.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Migrating Incus package from /opt/incus to /usr/incus..."

# GitHub Actions workflow
echo "  - Updating .github/workflows/builds.yml..."
sed -i 's|/opt/incus|/usr/incus|g' .github/workflows/builds.yml
# Fix package directory structure (pkg/opt/ -> pkg/usr/)
sed -i 's|pkg/opt/|pkg/usr/|g' .github/workflows/builds.yml
# Remove pkg/opt/ from mkdir command (now redundant with pkg/usr/)
sed -i '/mkdir -p/,/vendor-completions\// { /pkg\/opt\/ \\$/d }' .github/workflows/builds.yml

# Systemd service files
echo "  - Updating systemd service files..."
sed -i 's|/opt/incus|/usr/incus|g' \
    systemd/system/incus.service \
    systemd/system/incus-user.service \
    systemd/system/incus-startup.service \
    systemd/system/incus-lxcfs.service

# Systemd wrapper scripts
echo "  - Updating systemd wrapper scripts..."
sed -i 's|/opt/incus|/usr/incus|g' \
    systemd/wrappers/incusd \
    systemd/wrappers/incus-startup \
    systemd/wrappers/incus-user

# User-facing wrapper scripts
echo "  - Updating bin/ wrapper scripts..."
sed -i 's|/opt/incus|/usr/incus|g' \
    bin/incus \
    bin/incus-compose \
    bin/lxc-to-incus \
    bin/incus-migrate \
    bin/distrobuilder

# Debian package install files
echo "  - Updating debian install files..."
sed -i 's|opt/incus|usr/incus|g' \
    debian/incus.install \
    debian/incus-client.install \
    debian/incus-extra.install

# Remove 'opt' from incus-base.install (usr is already listed)
sed -i '/^opt$/d' debian/incus-base.install

# Debian rules
echo "  - Updating debian/rules..."
sed -i 's|opt/incus|usr/incus|g' debian/rules

# LXCFS patch
echo "  - Updating patches/lxcfs-0001-hook.patch..."
sed -i 's|/opt/incus|/usr/incus|g' patches/lxcfs-0001-hook.patch

echo ""
echo "Migration complete!"
echo ""
echo "Verify no remaining /opt/incus references:"
if grep -r "/opt/incus\|opt/incus" \
    .github/workflows/builds.yml \
    systemd/ \
    bin/ \
    debian/ \
    patches/ 2>/dev/null; then
    echo "WARNING: Some /opt/incus references remain!"
    exit 1
else
    echo "  All references successfully migrated to /usr/incus"
fi
