#!/usr/bin/env bash
# Baut ~/Applications/Kontor.app (alle Mandanten, Spec 18.2): Starter-Skript auf die Projekt-.venv, Icon aus
# app/icon/kontor.svg. Nur macOS.
# Aufruf: app/build.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/Kontor.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# Ist beim Bauen KONTOR_REGISTER gesetzt (Instanz-Datei), übernimmt das Startskript den Pfad.
REGISTER_LINE=""
if [ -n "${KONTOR_REGISTER:-}" ]; then REGISTER_LINE="export KONTOR_REGISTER=\"$KONTOR_REGISTER\""; fi
cat > "$APP/Contents/MacOS/kontor" <<EOS
#!/bin/bash
$REGISTER_LINE
cd "$HERE"
exec "$HERE/.venv/bin/python" "$HERE/app/main.py" >> "\$HOME/Library/Logs/kontor.log" 2>&1
EOS
chmod +x "$APP/Contents/MacOS/kontor"
cat > "$APP/Contents/Info.plist" <<EOS
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Kontor</string>
  <key>CFBundleDisplayName</key><string>Kontor</string>
  <key>CFBundleIdentifier</key><string>local.kontor</string>
  <key>CFBundleExecutable</key><string>kontor</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOS
TMP="$(mktemp -d)"
qlmanage -t -s 1024 -o "$TMP" "$HERE/app/icon/kontor.svg" >/dev/null 2>&1   # SVG → PNG mit Bordmitteln
mkdir -p "$TMP/icon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/kontor.svg.png" --out "$TMP/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$TMP/kontor.svg.png" --out "$TMP/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/icon.iconset" -o "$APP/Contents/Resources/icon.icns"
rm -rf "$TMP"
echo "$APP"
