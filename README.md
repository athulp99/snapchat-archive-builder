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

## Full batch with verified source deletion

Use `batch` for limited disk space. It processes ZIPs in numeric part order,
verifies every entry against saved bytes, writes a receipt, and proceeds to the
next archive. It preserves the existing library and video removal state.

```bash
python3 snapchat_archive.py batch \
  --input "/Users/ap/Downloads" \
  --pattern "mydata~1789158680787*.zip" \
  --output "/Users/ap/Desktop/Snapchat Test" \
  --delete-verified-zips
```

**`--delete-verified-zips` permanently deletes each verified source ZIP.** It does
not use or empty the Bin. The extracted files become your remaining local copy.
Omit this flag to retain the ZIPs. Do not edit/move library files or run another
converter or removal command during the batch.

Before each new archive the command requires its uncompressed size plus 5 GB of
free space. CRC errors, changed or missing outputs, unsafe ZIP paths, and failed
writes stop processing while retaining the affected source. Duplicate paths in
ZIPs are processed separately; byte-identical entries share verified saved files.
Overlays and all auxiliary content are retained for later review, not discarded.
Verification proves byte preservation, not successful overlay matching or video
decodability.

Rerun the same command after interruption. Archive fingerprints identify completed
content even if its ZIP was renamed. Reappearing ZIP copies are deleted only after
their saved outputs are rechecked. Changed content at an existing recorded path
is rejected rather than replacing previous history. The first test ZIP is fully
reverified before deletion.

`Reports/archive-progress.csv` records archive states and errors;
`Reports/archive-receipts/` preserves per-entry hashes and source positions.
`.archive/manifest-backup.sqlite` is refreshed before and after source deletion.
The gallery and normal reports refresh after each archive. Keep the `.archive`
folder with the library: it contains the completion history needed for resuming.

Run fixture tests with `python3 -m unittest -v test_batch`.

## Browse, filter and remove photos or videos

Run `gallery` after upgrading the script to add browsing and marking controls to an
existing library:

```bash
python3 snapchat_archive.py gallery --output "/Users/you/Desktop/Snapchat Library"
```

The self-contained gallery supports filename search, media type, date ranges,
undated files, size ranges, marks and duplicate-reference filters. Sort by date,
size, filename or reference count, and optionally group by day. Each page shows
up to 100 items, with uncropped previews loaded near the viewport.

Multiple export references already share one saved file: they are not redundant
physical copies to delete. Dates are export-derived. File sizes use decimal MB.

Mark individual photos/videos or the current page, then use **Export selection**.
`marked-media.json` includes each file path and SHA-256 hash (v2 format).
Older v1 `marked-videos.json` exports still work. Import replaces marks only if
every entry matches an active file and its hash. Filters and marks are saved
separately per library when browser storage is available. Export selections to
transfer marks between browsers or locations.

Move the selected files into the library's recoverable `Removed` folder:

```bash
python3 snapchat_archive.py remove-marked \
  --output "/Users/you/Desktop/Snapchat Library" \
  --selection "/Users/you/Downloads/marked-media.json"
```

The entire selection is validated before any moves. A rejected entry prevents
all moves. Restore every removed photo and video at any point:

```bash
python3 snapchat_archive.py restore-removed \
  --output "/Users/you/Desktop/Snapchat Library"
```

Each media move/restore action is written to `Reports/removal-log.csv`. These
commands never permanently delete media or source ZIPs; source deletion is a
separate opt-in batch operation.

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
