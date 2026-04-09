#!/bin/bash

# =====================================================
# ODA File Converter Installation Script for Ubuntu/WSL2
# =====================================================
# Run this script with sudo: sudo bash setup_oda.sh

set -e

echo "=========================================="
echo "Installing ODA File Converter Dependencies"
echo "=========================================="

# Update package lists
echo "[1/4] Updating package lists..."
apt-get update

# Install XCB and X11 dependencies for headless Qt applications
echo "[2/4] Installing XCB/X11 dependencies..."
apt-get install -y \
    libxcb-xinerama0 \
    libxcb-cursor0 \
    libxkbcommon-x11-0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-render-util0 \
    libxcb-shape0 \
    xvfb

# Check for DEB file
echo "[3/4] Installing ODA File Converter..."
DEB_FILE=$(find . -maxdepth 1 -name "ODAFileConverter*.deb" 2>/dev/null | head -n1)

if [ -z "$DEB_FILE" ]; then
    echo ""
    echo "ERROR: ODAFileConverter.deb file not found!"
    echo "Please copy the .deb installer to this directory:"
    echo "  - ODAFileConverter_QT6_lnxX64_8.3dll_27.1.deb"
    echo ""
    echo "You can download it from: https://www.opendesign.com/guestfiles/oda_file_converter"
    exit 1
fi

echo "Found installer: $DEB_FILE"
dpkg -i "$DEB_FILE" || true
apt-get install -f -y

# Verify installation
echo "[4/4] Verifying installation..."
if [ -f "/usr/bin/ODAFileConverter" ]; then
    echo ""
    echo "=========================================="
    echo "SUCCESS: ODA File Converter installed!"
    echo "=========================================="
    echo "Location: /usr/bin/ODAFileConverter"
    echo ""
    echo "Test conversion:"
    echo "  xvfb-run -a /usr/bin/ODAFileConverter <input_dir> <output_dir> ACAD2018 DWG 0 1"
    echo ""
else
    echo ""
    echo "ERROR: Installation failed. ODAFileConverter not found in /usr/bin/"
    exit 1
fi