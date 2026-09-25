#!/usr/bin/env python3
"""Tests for tools/faceage_backup.py.

A backup is only worth having if restore works on the day the laptop is
gone, so these run the round trip end to end on temp folders: back up,
change things, back up again, damage the copy, verify, restore three ways.

    python3 tools/test_faceage_backup.py
"""
import os
import sys
import json
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import faceage_backup as fb   # noqa: E402


def write(root, rel, content=b'x'):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'wb') as fh:
        fh.write(content)
    return p


class BackupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, 'FaceAgeData')
        self.dest = os.path.join(self.tmp, 'Backup')
        write(self.data, 'subjects/me/sessions/2026-09-12/IMG_1.jpg', b'\xff\xd8photo1')
        write(self.data, 'subjects/me/sessions/2026-09-12/IMG_2.jpg', b'\xff\xd8photo2')
        write(self.data, 'subjects/me/sessions/2026-09-12/checklist.json', b'{}')
        write(self.data, 'subjects/me/results/faceage_history.csv', b'session_date,mean\n2026-09-12,44.1\n')
        write(self.data, 'settings.json', b'{"luma_tol": 5}')
        write(self.data, '.DS_Store', b'junk')
        # never call brctl from a test, even on a Mac
        self._download = fb.download_evicted
        fb.download_evicted = lambda folder, log=None: False

    def tearDown(self):
        fb.download_evicted = self._download
        shutil.rmtree(self.tmp, ignore_errors=True)

    def files_in(self, root):
        return fb.walk(root)[0]


class TestBackup(BackupCase):
    def test_first_backup_copies_everything_and_writes_manifest(self):
        r = fb.backup(self.data, self.dest)
        self.assertEqual(r['new'], 5)
        self.assertEqual(r['changed'], 0)
        m = fb.read_manifest(self.dest)
        self.assertEqual(set(m['files']), set(self.files_in(self.data)))
        self.assertEqual(m['format'], fb.FORMAT)
        self.assertIn('host', m['wrote'])
        self.assertTrue(os.path.exists(os.path.join(self.dest, 'README.txt')))
        # junk is not data
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'data', '.DS_Store')))
        for rel, entry in m['files'].items():
            self.assertEqual(fb.sha256(os.path.join(self.dest, 'data', rel)), entry['sha256'])

    def test_second_backup_is_incremental(self):
        fb.backup(self.data, self.dest)
        r = fb.backup(self.data, self.dest)
        self.assertEqual((r['new'], r['changed'], r['unchanged'], r['bytes']), (0, 0, 5, 0))
        self.assertIsNone(r['attic'])

    def test_changed_file_goes_to_attic_not_bin(self):
        fb.backup(self.data, self.dest)
        hist = 'subjects/me/results/faceage_history.csv'
        write(self.data, hist, b'session_date,mean\n2026-09-12,44.1\n2026-10-12,43.7\n')
        r = fb.backup(self.data, self.dest)
        self.assertEqual(r['changed'], 1)
        self.assertIsNotNone(r['attic'])
        with open(os.path.join(r['attic'], hist), 'rb') as fh:
            self.assertEqual(fh.read(), b'session_date,mean\n2026-09-12,44.1\n')
        with open(os.path.join(self.dest, 'data', hist), 'rb') as fh:
            self.assertIn(b'2026-10-12', fh.read())

    def test_removed_file_is_kept_in_attic(self):
        fb.backup(self.data, self.dest)
        os.unlink(os.path.join(self.data, 'subjects/me/sessions/2026-09-12/IMG_2.jpg'))
        r = fb.backup(self.data, self.dest)
        self.assertEqual(r['removed'], 1)
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'data/subjects/me/sessions/2026-09-12/IMG_2.jpg')))
        self.assertTrue(os.path.exists(os.path.join(r['attic'], 'subjects/me/sessions/2026-09-12/IMG_2.jpg')))
        self.assertNotIn('subjects/me/sessions/2026-09-12/IMG_2.jpg', fb.read_manifest(self.dest)['files'])

    def test_touched_but_identical_file_is_not_recopied(self):
        fb.backup(self.data, self.dest)
        p = os.path.join(self.data, 'settings.json')
        os.utime(p, ns=(1, 1))
        r = fb.backup(self.data, self.dest)
        self.assertEqual((r['changed'], r['bytes']), (0, 0))

    def test_evicted_source_files_are_reported_not_skipped(self):
        write(self.data, 'subjects/me/sessions/2026-09-12/.IMG_3.jpg.icloud', b'stub')
        r = fb.backup(self.data, self.dest)
        self.assertEqual(r['evicted'], ['subjects/me/sessions/2026-09-12/IMG_3.jpg'])
        self.assertEqual(fb.read_manifest(self.dest)['evicted_at_source'], r['evicted'])

    def test_source_file_offloaded_to_icloud_is_not_treated_as_removed(self):
        rel = 'subjects/me/sessions/2026-09-12/IMG_1.jpg'
        fb.backup(self.data, self.dest)
        before = fb.read_manifest(self.dest)['files'][rel]
        os.unlink(os.path.join(self.data, rel))
        write(self.data, 'subjects/me/sessions/2026-09-12/.IMG_1.jpg.icloud', b'stub')
        r = fb.backup(self.data, self.dest)
        self.assertEqual(r['removed'], 0)
        self.assertEqual(fb.read_manifest(self.dest)['files'][rel], before)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, 'data', rel)))
        self.assertTrue(fb.verify(self.dest)['ok'])

    def test_refuses_backup_inside_the_data_folder(self):
        with self.assertRaises(fb.BackupError):
            fb.backup(self.data, os.path.join(self.data, 'backup'))
        with self.assertRaises(fb.BackupError):
            fb.set_backup_dir(os.path.join(self.data, 'backup'), self.data)

    def test_refuses_to_run_with_nowhere_to_go(self):
        orig = fb.default_dest
        fb.default_dest = lambda: None
        try:
            with self.assertRaises(fb.BackupError):
                fb.backup(self.data)
        finally:
            fb.default_dest = orig
        self.assertFalse(os.path.exists(os.path.join(os.getcwd(), 'manifest.json')))

    def test_refuses_missing_disk(self):
        with self.assertRaises(fb.BackupError):
            fb.backup(self.data, os.path.join(self.tmp, 'Volumes', 'Gone', 'FaceAge'))

    def test_default_destination_is_remembered_in_settings(self):
        fb.set_backup_dir(self.dest, self.data)
        with open(os.path.join(self.data, 'settings.json')) as fh:
            st = json.load(fh)
        self.assertEqual(st['backup_dir'], self.dest)
        self.assertEqual(st['luma_tol'], 5)       # other settings untouched
        self.assertEqual(fb.backup_dir(self.data), self.dest)
        r = fb.backup(self.data)                  # no dest: uses the remembered one
        self.assertEqual(r['dest'], self.dest)


class TestVerify(BackupCase):
    def test_clean_mirror_verifies(self):
        fb.backup(self.data, self.dest)
        v = fb.verify(self.dest)
        self.assertTrue(v['ok'])
        self.assertEqual(v['checked'], 5)
        self.assertEqual((v['missing'], v['mismatch'], v['evicted']), ([], [], []))

    def test_corrupted_and_missing_files_are_found(self):
        fb.backup(self.data, self.dest)
        write(self.dest, 'data/subjects/me/sessions/2026-09-12/IMG_1.jpg', b'bitrot')
        os.unlink(os.path.join(self.dest, 'data/settings.json'))
        v = fb.verify(self.dest)
        self.assertFalse(v['ok'])
        self.assertEqual(v['mismatch'], ['subjects/me/sessions/2026-09-12/IMG_1.jpg'])
        self.assertEqual(v['missing'], ['settings.json'])

    def test_evicted_backup_files_are_reported_apart(self):
        fb.backup(self.data, self.dest)
        p = os.path.join(self.dest, 'data/subjects/me/sessions/2026-09-12/IMG_1.jpg')
        os.unlink(p)
        write(self.dest, 'data/subjects/me/sessions/2026-09-12/.IMG_1.jpg.icloud', b'stub')
        v = fb.verify(self.dest)
        self.assertTrue(v['ok'])
        self.assertEqual(v['evicted'], ['subjects/me/sessions/2026-09-12/IMG_1.jpg'])

    def test_not_a_backup(self):
        with self.assertRaises(fb.BackupError):
            fb.verify(self.tmp)

    def test_evicted_mirror_copy_is_not_recopied_on_the_next_backup(self):
        fb.backup(self.data, self.dest)
        p = os.path.join(self.dest, 'data/subjects/me/sessions/2026-09-12/IMG_1.jpg')
        os.unlink(p)
        write(self.dest, 'data/subjects/me/sessions/2026-09-12/.IMG_1.jpg.icloud', b'stub')
        r = fb.backup(self.data, self.dest)
        self.assertEqual(r['bytes'], 0)


class TestRestore(BackupCase):
    def setUp(self):
        BackupCase.setUp(self)
        fb.backup(self.data, self.dest)
        self.new = os.path.join(self.tmp, 'NewMac', 'FaceAgeData')

    def test_fresh_restore_reproduces_the_folder(self):
        r = fb.restore(self.dest, self.new)
        self.assertEqual(r['restored'], 5)
        self.assertEqual(self.files_in(self.new).keys(), self.files_in(self.data).keys())
        for rel in self.files_in(self.data):
            if rel == 'settings.json':
                continue          # gains backup_dir, checked below
            self.assertEqual(fb.sha256(os.path.join(self.new, rel)),
                             fb.sha256(os.path.join(self.data, rel)))
        with open(os.path.join(self.new, 'settings.json')) as fh:
            st = json.load(fh)
        self.assertEqual(st['luma_tol'], 5)
        self.assertEqual(st['backup_dir'], self.dest)
        # mtimes come along, so the next backup has only that one file to do
        self.assertEqual(fb.status(self.new, self.dest)['pending'], 1)

    def test_restore_keeps_backing_up_to_the_same_place(self):
        fb.restore(self.dest, self.new)
        self.assertEqual(fb.backup_dir(self.new), self.dest)

    def test_restore_accepts_the_data_subfolder_too(self):
        r = fb.restore(os.path.join(self.dest, 'data'), self.new)
        self.assertEqual(r['restored'], 5)

    def test_refuses_over_existing_data_without_a_mode(self):
        write(self.new, 'subjects/me/results/faceage_history.csv', b'other')
        with self.assertRaises(fb.BackupError) as cm:
            fb.restore(self.dest, self.new)
        self.assertIn('--merge', str(cm.exception))
        self.assertIn('--replace', str(cm.exception))

    def test_merge_adds_missing_and_never_overwrites(self):
        write(self.new, 'subjects/me/results/faceage_history.csv', b'local version')
        write(self.new, 'subjects/me/sessions/2026-10-01/IMG_9.jpg', b'newer session')
        r = fb.restore(self.dest, self.new, mode='merge')
        self.assertEqual(r['restored'], 4)
        self.assertEqual(r['kept'], 1)
        self.assertEqual(r['differs'], ['subjects/me/results/faceage_history.csv'])
        with open(os.path.join(self.new, 'subjects/me/results/faceage_history.csv'), 'rb') as fh:
            self.assertEqual(fh.read(), b'local version')
        self.assertTrue(os.path.exists(os.path.join(self.new, 'subjects/me/sessions/2026-10-01/IMG_9.jpg')))

    def test_replace_moves_existing_aside_and_deletes_nothing(self):
        write(self.new, 'subjects/me/results/faceage_history.csv', b'local version')
        r = fb.restore(self.dest, self.new, mode='replace')
        self.assertEqual(r['restored'], 5)
        self.assertTrue(r['moved_aside'].startswith(self.new + '.archive-'))
        with open(os.path.join(r['moved_aside'], 'subjects/me/results/faceage_history.csv'), 'rb') as fh:
            self.assertEqual(fh.read(), b'local version')

    def test_refuses_a_damaged_backup(self):
        write(self.dest, 'data/subjects/me/sessions/2026-09-12/IMG_1.jpg', b'bitrot')
        with self.assertRaises(fb.BackupError) as cm:
            fb.restore(self.dest, self.new)
        self.assertIn('does not match', str(cm.exception))
        self.assertFalse(os.path.exists(self.new))
        # --force restores what it can and still verifies... which fails on
        # the damaged file rather than writing it
        with self.assertRaises(fb.BackupError):
            fb.restore(self.dest, self.new, force=True)

    def test_refuses_an_evicted_backup(self):
        p = os.path.join(self.dest, 'data/subjects/me/sessions/2026-09-12/IMG_1.jpg')
        os.unlink(p)
        write(self.dest, 'data/subjects/me/sessions/2026-09-12/.IMG_1.jpg.icloud', b'stub')
        with self.assertRaises(fb.BackupError) as cm:
            fb.restore(self.dest, self.new)
        self.assertIn('evicted', str(cm.exception))

    def test_stale_backup_dir_from_the_old_mac_is_dropped(self):
        # settings.json in the backup points at the old Mac's disk
        write(self.data, 'settings.json',
              json.dumps({'luma_tol': 5, 'backup_dir': '/Volumes/OldMacOnly/FaceAge'}).encode())
        fb.backup(self.data, self.dest)
        fb.restore(self.dest, self.new)
        self.assertEqual(fb.backup_dir(self.new), self.dest)

    def test_not_a_backup(self):
        with self.assertRaises(fb.BackupError):
            fb.restore(self.tmp, self.new)


class TestArchive(BackupCase):
    def test_zip_round_trip(self):
        out = os.path.join(self.tmp, 'move.zip')
        r = fb.archive(self.data, out)
        self.assertEqual(r['files'], 5)
        self.assertTrue(os.path.exists(out))
        new = os.path.join(self.tmp, 'NewMac', 'FaceAgeData')
        rr = fb.restore(out, new)
        self.assertEqual(rr['restored'], 5)
        for rel in self.files_in(self.data):
            self.assertEqual(fb.sha256(os.path.join(new, rel)), fb.sha256(os.path.join(self.data, rel)))
        # a zip does not say where to keep backing up
        self.assertIsNone(fb.backup_dir(new))

    def test_zip_without_manifest_is_refused(self):
        import zipfile
        out = os.path.join(self.tmp, 'random.zip')
        with zipfile.ZipFile(out, 'w') as z:
            z.writestr('hello.txt', 'hi')
        with self.assertRaises(fb.BackupError):
            fb.restore(out, os.path.join(self.tmp, 'x'))

    def test_extension_is_added(self):
        r = fb.archive(self.data, os.path.join(self.tmp, 'move'))
        self.assertTrue(r['out'].endswith('.zip'))


class TestStatus(BackupCase):
    def test_not_configured(self):
        s = fb.status(self.data)
        self.assertFalse(s['configured'])
        self.assertIsNone(s['last'])

    def test_pending_counts_changes_since_last_backup(self):
        fb.set_backup_dir(self.dest, self.data)
        s = fb.status(self.data)
        self.assertTrue(s['configured'])
        self.assertTrue(s['reachable'])
        self.assertIsNone(s['last'])
        self.assertEqual(s['pending'], 5)
        fb.backup(self.data)
        s = fb.status(self.data)
        self.assertIsNotNone(s['last'])
        self.assertEqual(s['pending'], 0)
        write(self.data, 'subjects/me/sessions/2026-10-12/IMG_1.jpg', b'new')
        os.unlink(os.path.join(self.data, 'subjects/me/sessions/2026-09-12/IMG_2.jpg'))
        self.assertEqual(fb.status(self.data)['pending'], 2)

    def test_unreachable_disk(self):
        fb.set_backup_dir(os.path.join(self.tmp, 'Volumes', 'Gone', 'FaceAge'), self.data)
        s = fb.status(self.data)
        self.assertTrue(s['configured'])
        self.assertFalse(s['reachable'])

    def test_describe_names_icloud(self):
        self.assertEqual(fb.describe(os.path.join(fb.ICLOUD, fb.BACKUP_NAME)), 'iCloud Drive')
        self.assertEqual(fb.describe(os.path.join(fb.ICLOUD, 'Other')), 'iCloud Drive/Other')
        self.assertEqual(fb.describe('/Volumes/T7/FaceAge'), '/Volumes/T7/FaceAge')
        self.assertEqual(fb.describe(''), '')


class TestCommandLine(BackupCase):
    def run_cli(self, *args, **kw):
        import io
        import contextlib
        fb.DATA = kw.get('data') or self.data
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = fb.main(list(args))
        return rc, out.getvalue()

    def test_backup_then_status_then_verify(self):
        rc, out = self.run_cli('backup', self.dest)
        self.assertEqual(rc, 0, out)
        self.assertIn('5 new', out)
        rc, out = self.run_cli('backup', '--status')
        self.assertEqual(rc, 0, out)
        self.assertIn('changed since   : 0', out)
        rc, out = self.run_cli('backup', '--verify')
        self.assertEqual(rc, 0, out)
        self.assertIn('OK', out)

    def test_status_without_setup_exits_nonzero(self):
        rc, out = self.run_cli('backup', '--status')
        self.assertEqual(rc, 1)
        self.assertIn('not set up', out)

    def test_restore_cli(self):
        self.run_cli('backup', self.dest)
        new = os.path.join(self.tmp, 'NewMac')
        rc, out = self.run_cli('restore', self.dest, data=new)
        self.assertEqual(rc, 0, out)
        self.assertIn('Restored 5 file(s)', out)

    def test_unknown_option(self):
        rc, out = self.run_cli('backup', '--bogus')
        self.assertEqual(rc, 1)
        self.assertIn('unknown option', out)

    def test_usage(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn('usage', out)


if __name__ == '__main__':
    unittest.main()
