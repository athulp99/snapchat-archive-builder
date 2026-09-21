"""Verified sequential ingestion. Source deletion is explicitly opt-in."""
import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import snapchat_archive as app

RESERVE = 5_000_000_000


def now():
    return datetime.now(timezone.utc).isoformat()


def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS batch_archives (
      sha256 TEXT PRIMARY KEY, archive_id INTEGER NOT NULL,
      state TEXT NOT NULL DEFAULT 'pending', entry_count INTEGER NOT NULL DEFAULT 0,
      verified_at TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS batch_sources (
      path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, state TEXT NOT NULL,
      updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS batch_members (
      archive_sha256 TEXT NOT NULL, entry_index INTEGER NOT NULL,
      source_name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT,
      entry_id INTEGER, is_directory INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(archive_sha256,entry_index));
    ''')
    db.commit()


def checked_path(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError('Invalid library path')
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or '..' in parts:
        raise ValueError('Unsafe library path: ' + relative)
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError('Symlink in library path: ' + relative)
    if root.resolve() not in candidate.resolve().parents:
        raise ValueError('Path escapes library')
    return candidate


def verify_file(root, relative, size, digest):
    path = checked_path(root, relative)
    if not path.is_file() or path.stat().st_size != size or app.sha256_path(path) != digest:
        raise ValueError('Saved file missing or changed: ' + relative)
    return path


def check_member(info):
    name = info.filename
    if not name or '\\' in name or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
        raise ValueError('Unsafe ZIP entry: ' + name)
    mode = stat.S_IFMT(info.external_attr >> 16)
    if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or (mode == stat.S_IFDIR and not info.is_dir()):
        raise ValueError('Unsupported ZIP special entry: ' + name)
    if info.flag_bits & 1:
        raise ValueError('Encrypted ZIP entry: ' + name)


def fingerprint(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Source must be a regular ZIP file: ' + str(path))
    before = path.stat()
    digest = app.sha256_path(path)
    after = path.stat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if signature(before) != signature(after):
        raise ValueError('Source changed while reading: ' + str(path))
    return digest, signature(after)


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def progress(db, root):
    rows = db.execute('''SELECT s.path,s.sha256,s.state,b.entry_count,b.verified_at,
                        s.updated_at,b.error FROM batch_sources s JOIN batch_archives b
                        ON b.sha256=s.sha256 ORDER BY s.path''').fetchall()
    import io
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(['source','sha256','state','entries','verified_at','updated_at','error'])
    writer.writerows(rows)
    atomic_text(root / 'Reports/archive-progress.csv', text.getvalue())


def backup(db, root):
    target = root / '.archive/manifest-backup.sqlite'
    temporary = target.with_suffix('.tmp')
    with sqlite3.connect(temporary) as copy:
        db.backup(copy)
    with temporary.open('rb') as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    sync_directory(target.parent)


def verify_outputs(db, root, digest):
    archive = db.execute('SELECT * FROM batch_archives WHERE sha256=?', (digest,)).fetchone()
    members = db.execute('SELECT * FROM batch_members WHERE archive_sha256=? ORDER BY entry_index', (digest,)).fetchall()
    if len(members) != archive['entry_count']:
        raise ValueError('Incomplete archive inventory')
    verified = set()
    for member in members:
        if member['is_directory']:
            continue
        entry = db.execute('SELECT * FROM entries WHERE id=?', (member['entry_id'],)).fetchone()
        if not entry or entry['sha256'] != member['sha256'] or entry['status'] not in ('converted','duplicate','review'):
            raise ValueError('Incomplete entry mapping: ' + member['source_name'])
        location = entry['removed_path'] or entry['output_path']
        identity = (location, member['size'], member['sha256'])
        if identity not in verified:
            verify_file(root, *identity)
            verified.add(identity)
    return members


def receipt(db, root, digest):
    members = verify_outputs(db, root, digest)
    document = {'format':'snapchat-verified-archive-v1','sha256':digest,
                'verified_at':now(),'entries':[dict(m) for m in members]}
    atomic_text(root / 'Reports/archive-receipts' / (digest + '.json'), json.dumps(document, indent=2))
    db.execute("UPDATE batch_archives SET state='verified',verified_at=?,error=NULL WHERE sha256=?", (now(),digest))
    db.execute("UPDATE batch_sources SET state='verified',updated_at=? WHERE sha256=? AND state!='source_deleted'", (now(),digest))
    db.commit()
    progress(db, root)
    backup(db, root)


def source_state(db, path, digest, state):
    db.execute('INSERT OR REPLACE INTO batch_sources VALUES(?,?,?,?)', (str(path),digest,state,now()))
    db.execute('UPDATE batch_archives SET state=? WHERE sha256=?', (state,digest))
    db.commit()


def remove_source(db, root, path, digest, expected_signature):
    current_digest, signature = fingerprint(path)
    if current_digest != digest or signature != expected_signature:
        raise ValueError('ZIP changed before deletion; retained: ' + str(path))
    source_state(db,path,digest,'deletion_pending')
    progress(db,root)
    backup(db,root)
    # Only this previously hashed, explicit regular-file path is deleted.
    if path.is_symlink() or (path.stat().st_dev,path.stat().st_ino,path.stat().st_size,path.stat().st_mtime_ns) != signature:
        raise ValueError('ZIP changed immediately before deletion')
    path.unlink()
    sync_directory(path.parent)
    source_state(db,path,digest,'source_deleted')
    progress(db,root)
    backup(db,root)
    print('Verified and permanently deleted: ' + path.name, flush=True)


def ingest(db, root, path, digest):
    archive = db.execute('SELECT * FROM batch_archives WHERE sha256=?', (digest,)).fetchone()
    if archive:
        archive_id = archive['archive_id']
    else:
        known = db.execute('SELECT * FROM archives WHERE path=?', (str(path),)).fetchone()
        if known and known['sha256'] != digest:
            raise ValueError('Different ZIP now occupies a recorded source path; retained')
        if not known:
            known = db.execute('SELECT * FROM archives WHERE sha256=? ORDER BY id LIMIT 1', (digest,)).fetchone()
        archive_id = known['id'] if known else db.execute(
            'INSERT INTO archives(path,sha256,bytes,scanned_at) VALUES(?,?,?,?)',
            (str(path),digest,path.stat().st_size,now())).lastrowid
        db.execute('INSERT INTO batch_archives(sha256,archive_id) VALUES(?,?)', (digest,archive_id))
        db.commit()
    source_state(db,path,digest,'processing')
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        for info in infos:
            check_member(info)
        needed = sum(i.file_size for i in infos) + RESERVE
        if shutil.disk_usage(root).free < needed:
            raise ValueError(f'Insufficient space: need {needed / 1e9:.2f} GB including reserve')
        db.execute('UPDATE batch_archives SET entry_count=? WHERE sha256=?', (len(infos),digest))
        db.commit()
        for index, info in enumerate(infos):
            if info.is_dir():
                db.execute('INSERT OR REPLACE INTO batch_members VALUES(?,?,?,?,?,?,1)', (digest,index,info.filename,0,None,None))
                db.commit()
                continue
            row = db.execute('SELECT * FROM entries WHERE archive_id=? AND entry_index=?', (archive_id,index)).fetchone()
            if row and (row['source_name'],row['size'],row['crc']) != (info.filename,info.file_size,info.CRC):
                raise ValueError('Existing manifest differs from ZIP entry')
            if not row:
                kind,token,extension = app.classify(info.filename)
                db.execute('''INSERT INTO entries(archive_id,entry_index,source_name,crc,size,asset_kind,asset_token,extension,captured_at)
                              VALUES(?,?,?,?,?,?,?,?,?)''', (archive_id,index,info.filename,info.CRC,info.file_size,kind,token,extension,app.date_from_name(info.filename,info.date_time)))
                db.commit()
                row = db.execute('SELECT * FROM entries WHERE archive_id=? AND entry_index=?', (archive_id,index)).fetchone()
            temp = None
            h = hashlib.sha256()
            try:
                if not row['output_path']:
                    work = root / '.archive/work'
                    work.mkdir(parents=True, exist_ok=True)
                    fd,temp_name = tempfile.mkstemp(prefix='entry-',dir=work)
                    temp = Path(temp_name)
                    destination = os.fdopen(fd,'wb')
                else:
                    destination = None
                try:
                    with z.open(info) as source:
                        for block in iter(lambda: source.read(1024*1024), b''):
                            h.update(block)
                            if destination:
                                destination.write(block)
                    if destination:
                        destination.flush()
                        os.fsync(destination.fileno())
                finally:
                    if destination:
                        destination.close()
                content_hash = h.hexdigest()
                if row['output_path']:
                    if row['sha256'] and row['sha256'] != content_hash:
                        raise ValueError('Source hash differs from recorded entry')
                    verify_file(root,row['removed_path'] or row['output_path'],info.file_size,content_hash)
                else:
                    twin = db.execute('SELECT * FROM entries WHERE sha256=? AND output_path IS NOT NULL ORDER BY id LIMIT 1', (content_hash,)).fetchone()
                    if twin:
                        verify_file(root,twin['removed_path'] or twin['output_path'],info.file_size,content_hash)
                        output_path,removed_path,removed_at = twin['output_path'],twin['removed_path'],twin['removed_at']
                        status = 'duplicate'
                    else:
                        base = 'Media' if row['asset_kind']=='media' and row['extension'] in app.MEDIA_EXTENSIONS else 'Needs Review'
                        stamp = row['captured_at']
                        folder = Path(stamp[:4])/stamp[5:7] if stamp else Path('Undated')
                        label = stamp.replace(':','-').replace(' ','_') if stamp else 'undated'
                        # Full archive fingerprint and entry position prevent restart collisions.
                        output_path = str(Path(base)/folder/(f'{label}_{digest}_{index:06d}'+(row['extension'] or '.bin')))
                        target = checked_path(root,output_path)
                        target.parent.mkdir(parents=True,exist_ok=True)
                        if target.exists():
                            verify_file(root,output_path,info.file_size,content_hash)
                        else:
                            # Exclusive publication: never overwrite an unrelated file.
                            os.link(temp,target)
                            sync_directory(target.parent)
                        verify_file(root,output_path,info.file_size,content_hash)
                        removed_path=removed_at=None
                        status='converted' if base=='Media' else 'review'
                    db.execute('UPDATE entries SET output_path=?,removed_path=?,removed_at=?,status=? WHERE id=?',
                               (output_path,removed_path,removed_at,status,row['id']))
                db.execute("UPDATE entries SET sha256=?,status=CASE WHEN status='failed' THEN CASE WHEN asset_kind='media' THEN 'converted' ELSE 'review' END ELSE status END WHERE id=?",(content_hash,row['id']))
                db.execute('INSERT OR REPLACE INTO batch_members VALUES(?,?,?,?,?,?,0)', (digest,index,info.filename,info.file_size,content_hash,row['id']))
                db.commit()
            finally:
                if temp and temp.exists():
                    temp.unlink()
            if (index+1) % 250 == 0:
                print(f'  {index+1}/{len(infos)} entries verified',flush=True)
    receipt(db,root,digest)


def natural_key(path):
    match = re.search(r'-(\d+)\.zip$',path.name)
    return (int(match.group(1)) if match else 0,path.name)


def batch(input_dir, root, pattern, delete=False):
    root = root.expanduser().absolute()
    if any(p.is_symlink() for p in [root,*root.parents]):
        raise ValueError('Library location must not contain symlinks')
    db = app.database(root)
    migrate(db)
    with (root/'.archive/batch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for source in db.execute("SELECT * FROM batch_sources WHERE state='deletion_pending'").fetchall():
            path=Path(source['path'])
            if not path.exists():
                verify_outputs(db,root,source['sha256'])
                if not (root/'Reports/archive-receipts'/(source['sha256']+'.json')).is_file():
                    raise ValueError('Missing verification receipt during recovery')
                source_state(db,path,source['sha256'],'source_deleted')
        if '/' in pattern or '\\' in pattern or not pattern.endswith('.zip'):
            raise ValueError('Pattern must select ZIP basenames only')
        paths=sorted(input_dir.expanduser().absolute().glob(pattern),key=natural_key)
        print(f'{len(paths)} source ZIP(s) found. '+('Verified source ZIPs will be permanently deleted.' if delete else 'Originals will be retained.'),flush=True)
        for number,path in enumerate(paths,1):
            digest=None
            try:
                print(f'[{number}/{len(paths)}] {path.name}',flush=True)
                digest,signature=fingerprint(path)
                old=db.execute('SELECT sha256 FROM batch_sources WHERE path=?',(str(path),)).fetchone()
                if old and old['sha256']!=digest:
                    raise ValueError('Source path now contains different content')
                completed=db.execute("SELECT * FROM batch_archives WHERE sha256=? AND state IN ('verified','source_deleted','deletion_pending')",(digest,)).fetchone()
                if completed:
                    print('  Already extracted; checking saved outputs',flush=True)
                    verify_outputs(db,root,digest)
                    source_state(db,path,digest,'verified')
                    receipt(db,root,digest)
                else:
                    ingest(db,root,path,digest)
                app.render_gallery(root,db)
                app.write_reports(root,db)
                if delete:
                    remove_source(db,root,path,digest,signature)
                else:
                    progress(db,root)
            except BaseException as exc:
                if digest:
                    db.execute('UPDATE batch_archives SET error=? WHERE sha256=?',(str(exc) or type(exc).__name__,digest))
                    db.commit()
                progress(db,root)
                backup(db,root)
                raise
        progress(db,root)
        backup(db,root)
        for row in db.execute('SELECT state,count(*) FROM batch_sources GROUP BY state'):
            print(f'{row[0]}: {row[1]}',flush=True)
        print('Batch complete. Report: '+str(root/'Reports/archive-progress.csv'),flush=True)
