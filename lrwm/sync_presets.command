#!/bin/bash
# Syncs Lightroom presets between this SSD and the Mac, in both directions.
# Placed here by Lightroom Workflow Manager at Leave.
#
# Run it after connecting the SSD, and again before ejecting it if you made
# presets on the Mac. Newer wins, replaced files are backed up, nothing is
# deleted. Works with the bash 3.2 that comes with macOS.
set -u

SSD="$(cd "$(dirname "$0")" && pwd)" || exit 1
MAC="${LRWM_ADOBE_DIR:-$HOME/Library/Application Support/Adobe}"
STATE="${LRWM_STATE_DIR:-$HOME/Library/Application Support/LRWM}"
FOLDERS=("CameraRaw/Settings" "CameraRaw/ImportedSettings" "Lightroom/Local Adjustment Presets")

finish() {
  if [ -t 0 ]; then
    printf '\nPress Return to close. '
    read -r _
  fi
  exit "$1"
}

if [ -z "${LRWM_ADOBE_DIR:-}" ]; then
  if [ "$(uname)" != "Darwin" ]; then
    echo "This script is for the Mac. On the PC, use Leave and Arrive."
    finish 1
  fi
  if pgrep -f "Adobe Lightroom Classic.app/Contents/MacOS/Adobe Lightroom Classic" >/dev/null; then
    echo "Lightroom Classic is running. Quit it, then run this again."
    finish 1
  fi
fi

mkdir -p "$STATE" || finish 1
SEEN="$STATE/presets-seen.txt"
MARKER="$STATE/last-sync"
BACKUP="$STATE/PresetBackups/$(date +%Y-%m-%d_%H%M%S)"
LIST="$STATE/list.tmp"
touch "$SEEN" || finish 1

failed=0

list_presets() {  # folder: writes the NUL-separated relative paths to LIST
  (cd "$1" && find . -type f \( -iname '*.xmp' -o -iname '*.lrtemplate' \) ! -name '._*' -print0) > "$LIST"
}

copy_file() {  # source, destination
  local tmp="$2.lrwm-tmp"
  if mkdir -p "$(dirname "$2")" && cp "$1" "$tmp" && touch -r "$1" "$tmp" && mv -f "$tmp" "$2"; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}

sync_one_way() {  # source root, destination root, name of destination
  local from="$1" to="$2" name="$3" copied=0 kept=0 skipped=0 folder file rel src dst
  for folder in "${FOLDERS[@]}"; do
    [ -d "$from/$folder" ] || continue
    if ! list_presets "$from/$folder"; then
      echo "  Could not read $from/$folder"
      failed=$((failed + 1))
      continue
    fi
    while IFS= read -r -d '' file; do
      rel="$folder/${file#./}"
      src="$from/$rel"
      dst="$to/$rel"
      if [ -e "$dst" ]; then
        cmp -s "$src" "$dst" && continue
        if ! [ "$src" -nt "$dst" ]; then
          kept=$((kept + 1))
          continue
        fi
        if ! copy_file "$dst" "$BACKUP/$name/$rel"; then
          failed=$((failed + 1))
          continue
        fi
      elif [ "$name" = Mac ] && grep -Fxq -- "$rel" "$SEEN" &&
           [ -e "$MARKER" ] && ! [ "$src" -nt "$MARKER" ]; then
        skipped=$((skipped + 1))  # deleted on this Mac since the last sync
        continue
      fi
      if copy_file "$src" "$dst"; then
        copied=$((copied + 1))
      else
        echo "  Could not copy $rel"
        failed=$((failed + 1))
      fi
    done < "$LIST"
  done
  echo "To $name: $copied copied, $kept newer there (left alone), $skipped deleted there (not restored)"
}

remember() {  # record every preset this Mac has now
  local folder file
  cp "$SEEN" "$SEEN.tmp" || return 1
  for folder in "${FOLDERS[@]}"; do
    [ -d "$MAC/$folder" ] || continue
    list_presets "$MAC/$folder" || return 1
    while IFS= read -r -d '' file; do
      printf '%s\n' "$folder/${file#./}" >> "$SEEN.tmp"
    done < "$LIST"
  done
  LC_ALL=C sort -u "$SEEN.tmp" > "$SEEN" && rm -f "$SEEN.tmp"
}

echo "Syncing Lightroom presets (this can take a minute)..."
sync_one_way "$SSD" "$MAC" Mac
sync_one_way "$MAC" "$SSD" SSD
remember || failed=$((failed + 1))
rm -f "$LIST"

if [ "$failed" -gt 0 ]; then
  echo "Finished with $failed error(s). Fix them and run this again."
  finish 1
fi
touch "$MARKER"
echo "Done. Nik presets are not included: use export and import inside Nik."
finish 0
