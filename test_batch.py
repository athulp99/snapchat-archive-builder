import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

import snapchat_archive as app
import snapchat_batch as b


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base/'input'
        self.source.mkdir()
        self.root = self.base/'library'

    def make_zip(self, name='mydata.zip', items=None):
        p=self.source/name
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',UserWarning)
            with zipfile.ZipFile(p,'w') as z:
                for path, data in items or [('chat_media/2020-01-01_a.mp4',b'video')]:
                    z.writestr(path,data)
        return p

    def run_batch(self, delete=False):
        with contextlib.redirect_stdout(io.StringIO()):
            b.batch(self.source,self.root,'mydata*.zip',delete)

    def db(self):
        db=app.database(self.root)
        self.addCleanup(db.close)
        return db

    def test_duplicates_roundtrip_and_removed(self):
        p=self.make_zip(items=[('chat_media/2020-01-01_a.mp4',b'one'),('chat_media/2020-01-01_a.mp4',b'two'),('chat_media/2020-01-01_b.mp4',b'one')])
        self.run_batch()
        db=self.db()
        self.assertEqual(db.execute('SELECT count(*) FROM entries').fetchone()[0],3)
        self.assertEqual(len(list((self.root/'Media').rglob('*.mp4'))),2)
        entry=db.execute('SELECT * FROM entries ORDER BY id LIMIT 1').fetchone()
        removed='Removed/test.mp4'
        (self.root/'Removed').mkdir()
        (self.root/entry['output_path']).rename(self.root/removed)
        db.execute('UPDATE entries SET removed_path=? WHERE output_path=?',(removed,entry['output_path']))
        db.commit()
        self.make_zip('mydata-2.zip', [('chat_media/2020-01-01_c.mp4',b'one')])
        self.run_batch(True)
        self.assertFalse(p.exists())
        self.assertEqual(db.execute('SELECT count(*) FROM entries WHERE removed_path IS NOT NULL').fetchone()[0],3)
        self.assertEqual(db.execute("SELECT count(*) FROM batch_sources WHERE state='source_deleted'").fetchone()[0],2)

    def test_bad_output_prevents_deletion(self):
        for variant in ('missing','modified'):
            with self.subTest(variant=variant):
                p=self.make_zip()
                if not (self.root/'.archive/manifest.sqlite').exists(): self.run_batch()
                db=self.db()
                target=self.root/db.execute('SELECT output_path FROM entries LIMIT 1').fetchone()[0]
                if variant=='missing': target.unlink()
                else: target.write_bytes(b'wrong')
                with self.assertRaises(ValueError): self.run_batch(True)
                self.assertTrue(p.exists())

    def test_invalid_archive_and_low_space(self):
        p=self.make_zip(items=[('../escape',b'bad')])
        with self.assertRaises(ValueError): self.run_batch(True)
        self.assertTrue(p.exists())
        p.unlink()
        p=self.make_zip('mydata-2.zip')
        with patch.object(b.shutil,'disk_usage',return_value=type('Usage',(),{'free':0})()):
            with self.assertRaises(ValueError): self.run_batch(True)
        self.assertTrue(p.exists())

    def test_renamed_completed(self):
        p=self.make_zip()
        self.run_batch()
        renamed=p.with_name('mydata-9.zip')
        p.rename(renamed)
        self.run_batch(True)
        self.assertFalse(renamed.exists())
        self.assertEqual(self.db().execute('SELECT count(*) FROM entries').fetchone()[0],1)

    def test_changed_zip_at_same_path(self):
        self.make_zip()
        self.run_batch()
        p=self.make_zip(items=[('other.mp4',b'new')])
        with self.assertRaises(ValueError): self.run_batch(True)
        self.assertTrue(p.exists())

    def test_interrupted_extraction(self):
        self.make_zip(items=[('a.mp4',b'a'),('b.mp4',b'b')])
        original=b.verify_file
        counter=0
        def interrupt(*args):
            nonlocal counter
            counter+=1
            if counter==2: raise KeyboardInterrupt()
            return original(*args)
        with patch.object(b,'verify_file',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt): self.run_batch()
        self.run_batch(True)
        self.assertEqual(len(list((self.root/'Media').rglob('*.mp4'))),2)

    def test_interruption_after_deletion(self):
        p=self.make_zip()
        original=b.source_state
        def interrupt(db,path,digest,state):
            if state=='source_deleted': raise KeyboardInterrupt()
            return original(db,path,digest,state)
        with patch.object(b,'source_state',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt): self.run_batch(True)
        self.assertFalse(p.exists())
        self.run_batch(True)
        self.assertEqual(self.db().execute('SELECT state FROM batch_sources').fetchone()[0],'source_deleted')

    def test_deletion_failure_and_retry(self):
        p=self.make_zip()
        original=Path.unlink
        def denied(path,*args,**kwargs):
            if path==p: raise PermissionError('test deletion failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',denied):
            with self.assertRaises(PermissionError): self.run_batch(True)
        self.assertTrue(p.exists())
        self.run_batch(True)
        self.assertFalse(p.exists())

    def test_unwritable_output(self):
        p=self.make_zip()
        with patch.object(b.os,'link',side_effect=PermissionError('test unwritable destination')):
            with self.assertRaises(PermissionError): self.run_batch(True)
        self.assertTrue(p.exists())

    def test_destination_collision(self):
        p=self.make_zip()
        self.root.mkdir()
        digest=b.fingerprint(p)[0]
        target=self.root/'Media/2020/01'/('2020-01-01_00-00-00_'+digest+'_000000.mp4')
        target.parent.mkdir(parents=True)
        target.write_bytes(b'unrelated')
        # Freeze provisional date to make the predicted destination deterministic.
        with patch.object(app,'date_from_name',return_value='2020-01-01 00:00:00'):
            with self.assertRaises(ValueError): self.run_batch(True)
        self.assertEqual(target.read_bytes(),b'unrelated')
        self.assertTrue(p.exists())

    def test_source_changes_before_delete(self):
        p=self.make_zip()
        original=b.remove_source
        def changed(db,root,path,digest,signature):
            with path.open('ab') as handle: handle.write(b'changed')
            return original(db,root,path,digest,signature)
        with patch.object(b,'remove_source',side_effect=changed):
            with self.assertRaises(ValueError): self.run_batch(True)
        self.assertTrue(p.exists())

    def test_symlink_refused(self):
        p=self.make_zip()
        self.run_batch()
        db=self.db()
        target=self.root/db.execute('SELECT output_path FROM entries LIMIT 1').fetchone()[0]
        real=self.base/'elsewhere.mp4'
        target.rename(real)
        target.symlink_to(real)
        with self.assertRaises(ValueError): self.run_batch(True)
        self.assertTrue(p.exists())

    def test_corrupt_crc(self):
        p=self.make_zip()
        raw=p.read_bytes().replace(b'video',b'wrong',1)
        p.write_bytes(raw)
        with self.assertRaises(zipfile.BadZipFile): self.run_batch(True)
        self.assertTrue(p.exists())

    def test_existing_converter_library_reverified(self):
        p=self.make_zip()
        with contextlib.redirect_stdout(io.StringIO()):
            app.scan(p,self.root)
            app.convert(self.root)
        count=len(list((self.root/'Media').rglob('*.*')))
        self.run_batch(True)
        self.assertFalse(p.exists())
        self.assertEqual(len(list((self.root/'Media').rglob('*.*'))),count)


if __name__=='__main__':
    unittest.main()
