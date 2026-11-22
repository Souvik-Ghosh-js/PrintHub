#!/usr/bin/env bash
# ============================================================
#  PrintHub — document converter (idempotent).
#
#  Installs headless LibreOffice so customers can upload Word, Excel,
#  PowerPoint, OpenDocument, RTF and text files (server/convert.py turns
#  them into PDF), plus fonts so documents keep their layout:
#    - Carlito / Caladea / Liberation: same metrics as Calibri, Cambria,
#      Arial and Times New Roman, so pages break where the author saw them
#    - Noto: Bengali, Hindi and other Indian scripts
#  Called by setup.sh and redeploy.sh. Does nothing once installed.
# ============================================================
set -euo pipefail

PKGS="libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
fonts-noto-core fonts-crosextra-carlito fonts-crosextra-caladea \
fonts-liberation2 fonts-dejavu-core"

missing=""
for p in $PKGS; do
  dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"
done

if [ -z "$missing" ]; then
  echo "    document converter already installed ($(soffice --version 2>/dev/null | head -1))"
  exit 0
fi

echo "    installing:$missing"
sudo apt-get update -y -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $missing
soffice --version | head -1
