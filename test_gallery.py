"""Regression tests using only disposable library files."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
import snapchat_archive as app


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = app.database(self.root)
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO archives VALUES(1,'fixture.zip','hash',1,'2026-01-01')")
        self.items = []
        for i, ext in enumerate(('.jpg', '.mp4', '.jpg')):
            path = 'Media/example' + (str(i) if i < 2 else '0') + ext
            data = b'photo' if ext == '.jpg' else b'video'
            p = self.root / path
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            self.db.execute('''INSERT INTO entries(archive_id,entry_index,source_name,crc,size,asset_kind,extension,captured_at,sha256,status,output_path)
              VALUES(1,?,?,0,?,'media',?,'2020-01-01',?,'converted',?)''', (i, 'original'+str(i)+ext, len(data), ext, digest, path))
            self.items.append(dict(path=path, sha256=digest))
        self.db.commit()

    def select(self, items, version=2):
        path = self.root / 'selection.json'
        path.write_text(json.dumps({'format': 'snapchat-removal-selection-v'+str(version), 'items' if version == 2 else 'videos': items}))
        return path

    def test_roundtrip_shared_photo_and_video(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app.remove_marked(self.root, self.select(self.items))
            self.assertEqual(self.db.execute('SELECT count(*) FROM entries WHERE removed_path IS NOT NULL').fetchone()[0], 3)
            self.assertEqual(self.db.execute("SELECT count(*) FROM removal_log WHERE result='moved'").fetchone()[0], 2)
            app.restore_removed(self.root)
        self.assertTrue(all((self.root / x['path']).is_file() for x in self.items))
        self.assertEqual(self.db.execute('SELECT count(*) FROM entries WHERE removed_path IS NOT NULL').fetchone()[0], 0)

    def test_invalid_selection_never_moves_valid_file(self):
        for invalid in (dict(path='../escape', sha256='bad'), dict(self.items[1], sha256='bad'), dict(path='Media/missing.mp4', sha256='bad')):
            with self.assertRaises(SystemExit):
                app.remove_marked(self.root, self.select([self.items[0], invalid]))
            self.assertTrue((self.root / self.items[0]['path']).is_file())
        (self.root / self.items[1]['path']).unlink()
        with self.assertRaises(SystemExit):
            app.remove_marked(self.root, self.select(self.items[:2]))
        self.assertTrue((self.root / self.items[0]['path']).is_file())

    def test_old_selection_and_conflicting_hashes(self):
        self.assertEqual(app.load_selection(self.select([self.items[1]], 1)), [self.items[1]])
        with self.assertRaises(SystemExit):
            app.load_selection(self.select([self.items[0], dict(self.items[0], sha256='bad')]))

    def test_gallery_unique_files_and_stable_id(self):
        app.render_gallery(self.root, self.db)
        text = (self.root / 'Open Gallery.html').read_text()
        data = json.loads(text.split('const DATA=', 1)[1].split(';\nconst items=', 1)[0])
        self.assertEqual(len(data['items']), 2)
        photo = next(x for x in data['items'] if x['type'] == 'photo')
        self.assertEqual(photo['refs'], 2)
        self.assertEqual(len(photo['sources']), 2)
        app.render_gallery(self.root, self.db)
        self.assertIn(data['library_id'], (self.root / 'Open Gallery.html').read_text())


if __name__ == '__main__':
    unittest.main()
