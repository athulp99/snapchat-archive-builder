"""Self-contained offline gallery, generated using manifest metadata only."""
import json
import uuid
from pathlib import Path


def render(output, db):
    from snapchat_batch import atomic_text
    db.execute('CREATE TABLE IF NOT EXISTS library_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO library_settings VALUES (?, ?)', ('library_id', str(uuid.uuid4())))
    library_id = db.execute("SELECT value FROM library_settings WHERE key='library_id'").fetchone()[0]
    db.commit()
    grouped = {}
    for row in db.execute("""SELECT output_path, removed_path, captured_at, extension, sha256, size, source_name
                             FROM entries WHERE output_path IS NOT NULL AND asset_kind='media'
                             ORDER BY output_path, id"""):
        path = row['output_path']
        if path not in grouped:
            grouped[path] = dict(path=path, actual=row['removed_path'] or path,
                                 date=row['captured_at'] or '', sha256=row['sha256'],
                                 size=row['size'], removed=bool(row['removed_path']),
                                 type='video' if row['extension'] in ('.mp4', '.mov') else 'photo',
                                 sources=[], refs=0)
        item = grouped[path]
        item['refs'] += 1
        if row['source_name'] not in item['sources']:
            item['sources'].append(row['source_name'])
    payload = json.dumps(dict(library_id=library_id, items=list(grouped.values())), separators=(',', ':')).replace('<', '\\u003c')
    template = Path(__file__).with_name('gallery.html').read_text(encoding='utf-8')
    atomic_text(output / 'Open Gallery.html', template.replace('__GALLERY_DATA__', payload))
