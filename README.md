# Snapchat Archive Builder

`snapchat_archive.py` turns one or more Snapchat **My Data** ZIP exports into a
local, date-organized media library. It reads ZIP entries directly, so repeated
paths inside a Snapchat ZIP are retained instead of silently being overwritten.

## Quick start

Pass one export ZIP to test it, or put every `mydata*.zip` export in a single
folder for the complete archive. Keep the ZIPs unchanged.

```bash
python3 snapchat_archive.py scan \
  --input "/Users/you/Downloads/mydata~1789158680787.zip" \
  --output "/Users/you/Desktop/Snapchat Library"

python3 snapchat_archive.py convert \
  --output "/Users/you/Desktop/Snapchat Library"
```

`scan` creates an inventory at `Reports/scan-summary.txt` and a SQLite manifest.
`convert` creates date folders, a local `Open Gallery.html`, a CSV item report,
and a `Needs Review` directory for overlays, thumbnails, and unresolved assets.
Run `convert` again after interruption; completed source entries are reused.

## Review and remove videos

Run `gallery` after upgrading the script to add video marking controls to an
existing library:

```bash
python3 snapchat_archive.py gallery --output "/Users/you/Desktop/Snapchat Library"
```

In `Open Gallery.html`, mark videos and use **Export selection**. The exported
`marked-videos.json` includes each video path and SHA-256 hash. Move only those
verified videos into the library's recoverable `Removed` folder:

```bash
python3 snapchat_archive.py remove-marked \
  --output "/Users/you/Desktop/Snapchat Library" \
  --selection "/Users/you/Downloads/marked-videos.json"
```

Restore every removed video at any point:

```bash
python3 snapchat_archive.py restore-removed \
  --output "/Users/you/Desktop/Snapchat Library"
```

Each action is written to `Reports/removal-log.csv`. Nothing is permanently
deleted and the source ZIPs remain unchanged.

## What it does now

* Preserves every ZIP member by its archive and entry position, including
  duplicate filenames.
* Groups media by the timestamp in Snapchat's filename where available.
* Combines identical bytes across all ZIPs while recording all source entries.
* Creates stable, readable filenames and an offline browser gallery.
* Keeps overlays separate unless they have an unambiguous matching media ID.
* Emits `Reports/items.csv` and `Reports/unresolved.csv` for audit/review.

## Optional enhancement tools

The base script requires only Python 3. Pillow and FFmpeg are deliberately not
required yet: the first conversion keeps unresolved overlays separate because
the supplied export does not provide reliable media-to-overlay IDs. A later
compositing pass can use these tools only after the report establishes verified
matches.

```bash
python3 -m pip install --user Pillow
# macOS, if Homebrew is installed:
brew install ffmpeg
```

Always inspect `Reports/unresolved.csv` before deleting the source ZIPs. The
export's `memories_history.json` often contains dates and GPS but does not always
link records to media files; this tool never guesses those relationships.
