"""Restore and hash-verify a Snapchat Library ZIP backup on its backup drive."""
import argparse
import csv
import hashlib
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def inside(root, relative):
    p = PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or '\\' in relative:
        raise ValueError('Unsafe ZIP path: ' + relative)
    # The archive deliberately contains its top-level directory entry
    # ("Snapchat Test/").  Its relative path is empty and maps to root.
    if not p.parts:
        return root
    target = root.joinpath(*p.parts)
    if root.resolve() not in target.parent.resolve().parents and target.parent.resolve() != root.resolve():
        raise ValueError('ZIP path escapes destination')
    return target


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as src:
        for block in iter(lambda: src.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify_partial_and_finalize(drive, archive_name='Snapchat Test.zip'):
    """Finish a complete partial restore after an interruption or metadata-only mismatch."""
    drive = drive.absolute()
    source_root = archive_name.removesuffix('.zip')
    hashes = drive / (source_root + '.file-hashes.csv')
    final = drive / source_root
    partial = drive / (source_root + '.restore.partial')
    if final.exists() or not partial.is_dir() or not hashes.is_file():
        raise ValueError('Expected partial restore and hash inventory are unavailable')
    with hashes.open(newline='', encoding='utf-8') as f:
        expected = {r['path']: (int(r['bytes']), r['sha256']) for r in csv.DictReader(f)}
    # exFAT receives macOS AppleDouble companions (._name) for metadata.  They
    # are filesystem-generated, not archive members, and must not affect the
    # exact comparison of the restored archive contents.
    actual = {
        source_root + '/' + str(p.relative_to(partial)): p
        for p in partial.rglob('*')
        if p.is_file() and not p.name.startswith('._')
    }
    if set(actual) != set(expected):
        raise ValueError('Restored inventory does not match hash inventory')
    for number, (name, (size, digest)) in enumerate(expected.items(), 1):
        p = actual[name]
        if p.stat().st_size != size or sha256(p) != digest:
            raise ValueError('Read-back hash mismatch: ' + name)
        if number % 500 == 0:
            print(f'Rechecked {number}/{len(expected)} files', flush=True)
    os.replace(partial, final)
    print(f'VERIFIED RESTORE: {final}\\nFiles: {len(expected)}', flush=True)


def restore(drive, archive_name='Snapchat Test.zip'):
    drive = drive.absolute()
    archive = drive / archive_name
    source_root = archive_name.removesuffix('.zip')
    hashes = drive / (source_root + '.file-hashes.csv')
    final = drive / source_root
    partial = drive / (source_root + '.restore.partial')
    if not os.path.ismount(drive) or not archive.is_file() or not hashes.is_file():
        raise ValueError('Drive, ZIP, or hash inventory is unavailable')
    if final.exists() or partial.exists():
        raise FileExistsError('Destination already exists; refusing to overwrite')
    expected = {}
    with hashes.open(newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            expected[r['path']] = (int(r['bytes']), r['sha256'])
    required = archive.stat().st_size + 5_000_000_000
    if shutil.disk_usage(drive).free < required:
        raise OSError('Insufficient drive space to restore archive plus 5 GB reserve')
    partial.mkdir()
    files = 0
    try:
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            names = [i.filename for i in infos]
            if len(names) != len(set(names)):
                raise ValueError('ZIP has duplicate entry names; refusing unsafe restore')
            for info in infos:
                name = info.filename
                if not name.startswith(source_root + '/'):
                    raise ValueError('Unexpected ZIP root: ' + name)
                relative = name[len(source_root) + 1:]
                target = inside(partial, relative)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if name not in expected:
                    raise ValueError('ZIP file missing from hash inventory: ' + name)
                target.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256(); size = 0
                with z.open(info) as src, target.open('xb') as dst:
                    for block in iter(lambda: src.read(4 * 1024 * 1024), b''):
                        dst.write(block); h.update(block); size += len(block)
                    dst.flush(); os.fsync(dst.fileno())
                want_size, want_hash = expected[name]
                if size != want_size or h.hexdigest() != want_hash:
                    raise ValueError('Restore write mismatch: ' + name)
                files += 1
                if files % 500 == 0:
                    print(f'Restored and verified {files}/{len(expected)} files', flush=True)
        actual = {source_root + '/' + str(p.relative_to(partial)): p for p in partial.rglob('*') if p.is_file()}
        if set(actual) != set(expected):
            raise ValueError('Restored inventory does not match hash inventory')
        for name, (size, digest) in expected.items():
            p = actual[name]
            if p.stat().st_size != size or sha256(p) != digest:
                raise ValueError('Read-back hash mismatch: ' + name)
        os.replace(partial, final)
        print(f'VERIFIED RESTORE: {final}\nFiles: {files}', flush=True)
    except BaseException:
        print(f'Restore stopped. Partial files remain at {partial}', flush=True)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--drive', type=Path, required=True)
    parser.add_argument('--finish-partial', action='store_true',
                        help='Fully rehash and finalize an already-complete partial restore')
    args = parser.parse_args()
    if args.finish_partial:
        verify_partial_and_finalize(args.drive)
    else:
        restore(args.drive)
