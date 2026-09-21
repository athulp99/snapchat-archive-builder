"""Create and independently verify a ZIP64 library backup on a mounted drive."""
import argparse
import csv
import hashlib
import json
import os
import stat
import time
import uuid
import zipfile
import shutil
from datetime import datetime, timezone
from pathlib import Path


def signature(path):
    s = path.lstat()
    return (s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_dev, s.st_ino)


def inventory(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(directory) / name
            s = signature(p)
            if not (stat.S_ISREG(s[0]) or stat.S_ISDIR(s[0])):
                raise ValueError('Unsupported special file: ' + str(p))
            result[str(p.relative_to(root))] = s
    return result


def backup(root, drive):
    root, drive = root.absolute(), drive.absolute()
    if not root.is_dir() or root.is_symlink() or not os.path.ismount(drive):
        raise ValueError('Source directory or mounted destination is unavailable')
    device = drive.stat().st_dev
    def mounted():
        if not os.path.ismount(drive) or drive.stat().st_dev != device:
            raise OSError('Backup drive disconnected or changed')
    final = drive / (root.name + '.zip')
    report = drive / (root.name + '.verification.json')
    hashes = drive / (root.name + '.file-hashes.csv')
    for p in (final, report, hashes):
        if p.exists():
            raise FileExistsError('Will not overwrite: ' + str(p))
    before = inventory(root)
    total = sum(s[1] for s in before.values() if stat.S_ISREG(s[0]))
    count = sum(stat.S_ISREG(s[0]) for s in before.values())
    if shutil.disk_usage(drive).free < total + 5_000_000_000:
        raise OSError('Insufficient destination space including 5 GB reserve')
    partial = drive / (root.name + '.zip.partial')
    if partial.exists():
        partial = drive / (root.name + '.' + uuid.uuid4().hex + '.zip.partial')
    records = []
    done = 0
    last = time.monotonic()
    print(f'Inventory: {count} files, {total/1e9:.2f} GB. Writing {partial}', flush=True)
    with partial.open('xb') as raw:
        with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            z.writestr(zipfile.ZipInfo(root.name + '/'), b'')
            for rel, sig in sorted(before.items()):
                mounted()
                source = root / rel
                if signature(source) != sig:
                    raise ValueError('Source changed: ' + rel)
                arcname = root.name + '/' + rel
                info = zipfile.ZipInfo.from_file(source, arcname, strict_timestamps=False)
                if stat.S_ISDIR(sig[0]):
                    z.writestr(info, b'')
                    continue
                h = hashlib.sha256()
                size = 0
                with source.open('rb') as src, z.open(info, 'w', force_zip64=True) as dst:
                    for block in iter(lambda: src.read(4*1024*1024), b''):
                        dst.write(block)
                        h.update(block)
                        size += len(block)
                if size != sig[1] or signature(source) != sig:
                    raise ValueError('Source changed while copying: ' + rel)
                records.append({'path': arcname, 'bytes': size, 'sha256': h.hexdigest(), 'mtime_ns': sig[2]})
                done += size
                if time.monotonic() - last > 20:
                    print(f'Writing: {len(records)}/{count} files, {done/1e9:.2f}/{total/1e9:.2f} GB', flush=True)
                    last = time.monotonic()
        raw.flush()
        os.fsync(raw.fileno())
    print('Write complete. Reading every ZIP member back for CRC and SHA-256 verification.', flush=True)
    expected = {r['path']: r for r in records}
    expected_dirs = {root.name + '/'} | {root.name+'/'+r+'/' for r,s in before.items() if stat.S_ISDIR(s[0])}
    seen = set()
    done = n = 0
    with zipfile.ZipFile(partial) as z:
        for info in z.infolist():
            mounted()
            if info.filename in seen:
                raise ValueError('Unexpected duplicate archive name')
            seen.add(info.filename)
            if info.is_dir():
                if info.filename not in expected_dirs:
                    raise ValueError('Unexpected directory')
                continue
            record = expected[info.filename]
            h = hashlib.sha256()
            size = 0
            with z.open(info) as src:
                for block in iter(lambda: src.read(4*1024*1024), b''):
                    h.update(block)
                    size += len(block)
            if size != record['bytes'] or h.hexdigest() != record['sha256']:
                raise ValueError('Archive verification failed: ' + info.filename)
            done += size
            n += 1
            if time.monotonic() - last > 20:
                print(f'Verifying: {n}/{count} files, {done/1e9:.2f}/{total/1e9:.2f} GB', flush=True)
                last = time.monotonic()
    if seen != set(expected) | expected_dirs or inventory(root) != before:
        raise ValueError('Archive contents incomplete or source inventory changed')
    mounted()
    # Reserve the final name exclusively before replacing this tool-owned placeholder.
    with final.open('xb'):
        pass
    try:
        os.replace(partial, final)
    except BaseException:
        if final.exists() and final.stat().st_size == 0:
            final.unlink()
        raise
    with hashes.open('x', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['path','bytes','sha256','mtime_ns'])
        writer.writeheader()
        writer.writerows(records)
        handle.flush()
        os.fsync(handle.fileno())
    with report.open('x', encoding='utf-8') as handle:
        json.dump({'result':'verified','completed_at':datetime.now(timezone.utc).isoformat(),
                   'source':str(root),'archive':str(final),'files':count,'source_bytes':total,
                   'archive_bytes':final.stat().st_size,'verification':'Every file CRC, size and SHA-256; full source inventory unchanged',
                   'source_retained':True},handle,indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    print(f'VERIFIED BACKUP: {final}\nFiles: {count}; source GB: {total/1e9:.2f}\nReport: {report}',flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--drive',type=Path,required=True)
    args=parser.parse_args()
    backup(args.source,args.drive)
