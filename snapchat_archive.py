#!/usr/bin/env python3
"""Build an auditable, local library from Snapchat My Data ZIP exports.

The program intentionally never modifies input archives. It indexes members by
their position in the central directory because Snapchat exports can contain the
same path more than once.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

APP = "Snapchat Archive Builder"
SCHEMA_VERSION = 1
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
DATE_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:[_ -](?P<time>\d{2}[-:]\d{2}[-:]\d{2}))?")
ASSET_RE = re.compile(r"_(?P<kind>media|overlay|thumbnail|metadata)~(?P<token>.+?)(?:\.[^.]+)?$")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_member(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(name) and not name.startswith(("/", "\\")) and ".." not in parts


def classify(name: str) -> tuple[str, Optional[str], Optional[str]]:
    filename = PurePosixPath(name).name
    ext = Path(filename).suffix.lower()
    match = ASSET_RE.search(filename)
    if match:
        return match.group("kind"), match.group("token"), ext
    if ext in MEDIA_EXTENSIONS:
        return "media", None, ext
    return "other", None, ext


def date_from_name(name: str, zip_time: Optional[tuple[int, int, int, int, int, int]] = None) -> Optional[str]:
    match = DATE_RE.match(PurePosixPath(name).name)
    if not match:
        return None
    value = match.group("date")
    time = match.group("time")
    if time:
        return value + " " + time.replace("-", ":")
    # Snapchat frequently puts only the calendar day in the name while the ZIP
    # entry retains the time shown in the export's own directory listing.
    if zip_time:
        return value + f" {zip_time[3]:02d}:{zip_time[4]:02d}:{zip_time[5]:02d}"
    return value + " 00:00:00"


def database(output: Path) -> sqlite3.Connection:
    archive = output / ".archive"
    archive.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(archive / "manifest.sqlite")
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA foreign_keys = ON;
    CREATE TABLE IF NOT EXISTS archives (
      id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL,
      bytes INTEGER NOT NULL, scanned_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS entries (
      id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL REFERENCES archives(id),
      entry_index INTEGER NOT NULL, source_name TEXT NOT NULL, crc INTEGER NOT NULL,
      size INTEGER NOT NULL, asset_kind TEXT NOT NULL, asset_token TEXT,
      extension TEXT, captured_at TEXT, sha256 TEXT, status TEXT NOT NULL DEFAULT 'indexed',
      output_path TEXT, note TEXT, UNIQUE(archive_id, entry_index)
    );
    CREATE INDEX IF NOT EXISTS entries_hash ON entries(sha256);
    CREATE INDEX IF NOT EXISTS entries_token ON entries(asset_token);
    CREATE TABLE IF NOT EXISTS memory_records (
      id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL REFERENCES archives(id),
      captured_at TEXT, media_type TEXT, location TEXT
    );
    """)
    return db


def zip_files(input_dir: Path) -> list[Path]:
    return sorted(p for p in input_dir.rglob("*.zip") if p.is_file() and not p.name.startswith("._"))


def scan(input_dir: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    reports = output / "Reports"
    reports.mkdir(exist_ok=True)
    db = database(output)
    zips = zip_files(input_dir)
    if not zips:
        raise SystemExit(f"No ZIP files found under {input_dir}")
    stats = Counter()
    for path in zips:
        source = str(path.resolve())
        archive_hash = sha256_path(path)
        row = db.execute("SELECT id, sha256 FROM archives WHERE path=?", (source,)).fetchone()
        if row and row["sha256"] == archive_hash:
            print(f"Already indexed: {path.name}")
            continue
        if row:
            db.execute("DELETE FROM entries WHERE archive_id=?", (row["id"],))
            db.execute("DELETE FROM memory_records WHERE archive_id=?", (row["id"],))
            db.execute("UPDATE archives SET sha256=?, bytes=?, scanned_at=? WHERE id=?",
                       (archive_hash, path.stat().st_size, datetime.now().isoformat(timespec="seconds"), row["id"]))
            archive_id = row["id"]
        else:
            archive_id = db.execute("INSERT INTO archives(path,sha256,bytes,scanned_at) VALUES(?,?,?,?)",
                (source, archive_hash, path.stat().st_size, datetime.now().isoformat(timespec="seconds"))).lastrowid
        print(f"Indexing: {path.name}")
        with zipfile.ZipFile(path) as archive:
            for index, info in enumerate(archive.infolist()):
                if info.is_dir() or not safe_member(info.filename):
                    continue
                kind, token, extension = classify(info.filename)
                note = "Folder placement timestamp derived from Snapchat filename and ZIP entry time; not embedded as capture metadata."
                db.execute("""INSERT INTO entries(archive_id,entry_index,source_name,crc,size,asset_kind,asset_token,extension,captured_at,note)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (archive_id, index, info.filename, info.CRC, info.file_size, kind, token, extension,
                                                       date_from_name(info.filename, info.date_time), note))
                stats[kind] += 1
            try:
                payload = json.loads(archive.read("json/memories_history.json"))
                for record in payload.get("Saved Media", []):
                    db.execute("INSERT INTO memory_records(archive_id,captured_at,media_type,location) VALUES(?,?,?,?)",
                        (archive_id, record.get("Date"), record.get("Media Type"), record.get("Location")))
            except (KeyError, json.JSONDecodeError):
                pass
        db.commit()
    total = db.execute("SELECT count(*) FROM entries").fetchone()[0]
    unique_paths = db.execute("SELECT count(DISTINCT archive_id || ':' || source_name) FROM entries").fetchone()[0]
    duplicates = db.execute("""SELECT count(*) FROM (SELECT archive_id,source_name FROM entries GROUP BY archive_id,source_name HAVING count(*)>1)""").fetchone()[0]
    with (reports / "scan-summary.txt").open("w", encoding="utf-8") as handle:
        handle.write(f"{APP}\nSchema version: {SCHEMA_VERSION}\n\n")
        handle.write(f"Archives indexed: {db.execute('SELECT count(*) FROM archives').fetchone()[0]}\n")
        handle.write(f"Entries indexed: {total}\nRepeated paths within archives: {duplicates}\n")
        handle.write("\nEntry kinds:\n")
        for kind, count in db.execute("SELECT asset_kind,count(*) FROM entries GROUP BY asset_kind ORDER BY asset_kind"):
            handle.write(f"  {kind}: {count}\n")
        handle.write("\nRun convert only after reviewing the archive count above.\n")
    print(f"Indexed {total} entries from {len(zips)} ZIP(s). Report: {reports / 'scan-summary.txt'}")


def copy_entry(archive_path: Path, info: zipfile.ZipInfo, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial")
    digest = hashlib.sha256()
    with zipfile.ZipFile(archive_path) as archive, archive.open(info) as source, temporary.open("wb") as destination:
        while block := source.read(1024 * 1024):
            digest.update(block)
            destination.write(block)
    os.replace(temporary, target)
    return digest.hexdigest()


def output_name(entry: sqlite3.Row, ordinal: int) -> Path:
    extension = entry["extension"] or ".bin"
    if not entry["captured_at"]:
        return Path("Undated") / f"undated_{ordinal:06d}_{entry['id']}{extension}"
    stamp = entry["captured_at"].replace(":", "-").replace(" ", "_")
    return Path(stamp[:4]) / stamp[5:7] / f"{stamp}_{ordinal:06d}_{entry['id']}{extension}"


def render_gallery(output: Path, db: sqlite3.Connection) -> None:
    media = db.execute("""SELECT output_path,captured_at,extension FROM entries
                        WHERE output_path IS NOT NULL AND asset_kind='media'
                        GROUP BY output_path ORDER BY captured_at DESC, output_path DESC""").fetchall()
    cards = []
    for row in media:
        rel = html.escape(row["output_path"])
        label = html.escape(row["captured_at"] or "Undated")
        if row["extension"] in VIDEO_EXTENSIONS:
            content = f'<video controls preload="metadata" src="{rel}"></video>'
        else:
            content = f'<img loading="lazy" src="{rel}" alt="{label}">'
        cards.append(f'<article><a href="{rel}">{content}</a><p>{label}</p></article>')
    page = """<!doctype html><meta charset=\"utf-8\"><title>Snapchat Library</title>
<style>body{background:#101114;color:#eee;font:16px system-ui;margin:0;padding:24px}h1{margin:0 0 6px}p{color:#abb0bb}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}article{background:#1b1d23;border-radius:10px;overflow:hidden}img,video{display:block;width:100%;aspect-ratio:9/16;object-fit:cover;background:#000}article p{margin:10px;font-size:13px}</style>
<h1>Snapchat Library</h1><p>Locally generated. Review Reports/unresolved.csv before relying on incomplete items.</p><main class=\"grid\">""" + "\n".join(cards) + "</main>"
    (output / "Open Gallery.html").write_text(page, encoding="utf-8")


def write_reports(output: Path, db: sqlite3.Connection) -> None:
    reports = output / "Reports"
    reports.mkdir(exist_ok=True)
    headers = ["id", "archive", "entry_index", "source_name", "kind", "captured_at", "sha256", "output_path", "status", "note"]
    rows = db.execute("""SELECT e.id,a.path,e.entry_index,e.source_name,e.asset_kind,e.captured_at,e.sha256,e.output_path,e.status,e.note
                       FROM entries e JOIN archives a ON a.id=e.archive_id ORDER BY e.id""")
    with (reports / "items.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle); writer.writerow(headers); writer.writerows(rows)
    unresolved = db.execute("""SELECT e.id,a.path,e.entry_index,e.source_name,e.asset_kind,e.captured_at,e.status,e.note
                              FROM entries e JOIN archives a ON a.id=e.archive_id
                              WHERE e.asset_kind IN ('overlay','thumbnail','metadata') OR e.status='failed' ORDER BY e.id""")
    with (reports / "unresolved.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle); writer.writerow(["id","archive","entry_index","source_name","kind","captured_at","status","note"]); writer.writerows(unresolved)


def convert(output: Path) -> None:
    db = database(output)
    if not db.execute("SELECT 1 FROM archives LIMIT 1").fetchone():
        raise SystemExit("No scan manifest found. Run scan first.")
    media_root, review_root = output / "Media", output / "Needs Review"
    media_root.mkdir(exist_ok=True); review_root.mkdir(exist_ok=True)
    archive_rows = {r["id"]: Path(r["path"]) for r in db.execute("SELECT id,path FROM archives")}
    rows = db.execute("SELECT * FROM entries WHERE status='indexed' ORDER BY archive_id,entry_index").fetchall()
    infos: dict[int, list[zipfile.ZipInfo]] = {}
    copy_count = 0
    for ordinal, entry in enumerate(rows, 1):
        archive_path = archive_rows[entry["archive_id"]]
        try:
            if entry["archive_id"] not in infos:
                with zipfile.ZipFile(archive_path) as z: infos[entry["archive_id"]] = z.infolist()
            info = infos[entry["archive_id"]][entry["entry_index"]]
            # Main library contains only real media. Auxiliary assets stay auditable in review.
            root = media_root if entry["asset_kind"] == "media" and entry["extension"] in MEDIA_EXTENSIONS else review_root
            relative = output_name(entry, ordinal)
            target = root / relative
            digest = copy_entry(archive_path, info, target)
            existing = db.execute("SELECT output_path FROM entries WHERE sha256=? AND output_path IS NOT NULL LIMIT 1", (digest,)).fetchone()
            if existing:
                target.unlink()
                output_path = existing["output_path"]
                status, note = "duplicate", "Exact byte duplicate; source retained in manifest."
            else:
                output_path = str(target.relative_to(output))
                status = "converted" if root == media_root else "review"
                note = ""
                if entry["asset_kind"] == "overlay": note = "Separate overlay; automatic compositing is intentionally disabled until a verified media match is available."
                elif entry["asset_kind"] == "thumbnail": note = "Thumbnail retained outside the main library."
            db.execute("UPDATE entries SET sha256=?,output_path=?,status=?,note=? WHERE id=?", (digest, output_path, status, note, entry["id"]))
            copy_count += 1
            if copy_count % 50 == 0:
                db.commit(); print(f"Processed {copy_count}/{len(rows)} entries")
        except Exception as exc:
            db.execute("UPDATE entries SET status='failed',note=? WHERE id=?", (str(exc), entry["id"]))
            db.commit()
            print(f"Failed entry {entry['id']}: {exc}", file=sys.stderr)
    db.commit()
    render_gallery(output, db); write_reports(output, db)
    print(f"Finished {copy_count} entries. Open: {output / 'Open Gallery.html'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=APP)
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan", help="inventory export ZIP files")
    scan_parser.add_argument("--input", type=Path, required=True)
    scan_parser.add_argument("--output", type=Path, required=True)
    convert_parser = sub.add_parser("convert", help="create the local library from a previous scan")
    convert_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "scan": scan(args.input.expanduser(), args.output.expanduser())
    else: convert(args.output.expanduser())


if __name__ == "__main__":
    main()
