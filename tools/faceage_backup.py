#!/usr/bin/env python3
"""FaceAge backup: a verifiable mirror of the data folder, and its restore.

Everything else on the Mac -- the VM, the container image, Rosetta, the model
weights, the shell PATH -- is rebuilt by tools/install.sh in ten minutes. The
photos and the series under ~/FaceAgeData are the only thing that cannot be
rebuilt, so that is what this backs up.

The backup is a plain copy of the folder, not an archive format:

    <dest>/data/...        the data folder, file for file
    <dest>/manifest.json   sha256, size and mtime of every file, plus which
                           machine and which code wrote it
    <dest>/attic/<when>/   whatever changed or vanished at the source since
                           the previous run; nothing is ever deleted here
    <dest>/README.txt      how to restore, written for a person with no code

Plain files, so the photos are still viewable in Finder in ten years, iCloud
Drive / Dropbox / an external disk all work, and each run copies only what
changed. The manifest is what makes it a backup rather than a copy: every
file is re-hashed after it is written, `verify` re-hashes the whole mirror
against the manifest, and `restore` refuses a mirror that does not match.

    python3 tools/faceage_backup.py backup [DEST] [--verify|--status|--archive FILE.zip]
    python3 tools/faceage_backup.py restore SRC [--merge|--replace] [--force]

Usually reached through `faceage backup` / `faceage restore` and the app.
No dependencies beyond the standard library, deliberately.
"""
import os
import re
import sys
import json
import time
import shutil
import socket
import hashlib
import zipfile
import datetime
import platform
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get('FACEAGE_REPO') or os.path.dirname(HERE)
DATA = os.path.expanduser(os.environ.get('FACEAGE_DATA', '~/FaceAgeData'))

FORMAT = 'faceage-backup/1'
BACKUP_NAME = 'FaceAge Backup'
ICLOUD = os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs')
SKIP_NAMES = {'.DS_Store', 'Icon\r', '.localized'}
ICLOUD_STUB = re.compile(r'^\.(.+)\.icloud$')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.heic', '.heif', '.zip')   # already compressed


class BackupError(Exception):
    pass


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _stamp():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _stub_of(path):
    """The iCloud placeholder that stands in for an evicted file."""
    return os.path.join(os.path.dirname(path), '.%s.icloud' % os.path.basename(path))


def walk(root):
    """Every regular file under root, keyed by relative path, as (size, mtime_ns).

    Returns (files, evicted): evicted are iCloud placeholders whose real file
    is not on this disk. They are counted, never silently skipped, because a
    backup that quietly misses 4 of 12 photos is worse than none.
    """
    files, evicted = {}, []
    if not os.path.isdir(root):
        return files, evicted
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
        for fn in sorted(filenames):
            if fn in SKIP_NAMES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            m = ICLOUD_STUB.match(fn)
            if m:
                evicted.append(os.path.join(os.path.dirname(rel), m.group(1)))
                continue
            if fn.startswith('.') or os.path.islink(full):
                continue
            st = os.stat(full)
            files[rel] = (st.st_size, st.st_mtime_ns)
    return files, evicted


def _present(path):
    return os.path.isfile(path) or os.path.isfile(_stub_of(path))


def download_evicted(folder, log=None):
    """Ask iCloud for the evicted files under folder. Best effort: brctl is
    macOS-only and may take a while; the caller re-walks afterwards."""
    if not shutil.which('brctl'):
        return False
    if log:
        log('Downloading evicted iCloud files under %s ...' % folder)
    try:
        subprocess.run(['brctl', 'download', folder], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return False
    # brctl returns before the files land; give them a moment
    for _ in range(60):
        if not walk(folder)[1]:
            return True
        time.sleep(1)
    return not walk(folder)[1]


# ----------------------------------------------------------------------------
# manifest
# ----------------------------------------------------------------------------

def manifest_path(dest):
    return os.path.join(dest, 'manifest.json')


def read_manifest(dest):
    try:
        with open(manifest_path(dest)) as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or not isinstance(m.get('files'), dict):
        return None
    return m


def fingerprint():
    """What produced this backup. Not needed to restore; invaluable when a
    number on the tracker looks wrong two years from now."""
    fp = {'host': socket.gethostname(),
          'os': ('macOS %s' % platform.mac_ver()[0]) if platform.mac_ver()[0] else platform.platform(),
          'python': platform.python_version(),
          'faceage_backup': FORMAT}
    try:
        fp['repo_commit'] = subprocess.run(
            ['git', '-C', REPO, 'rev-parse', 'HEAD'], check=True, timeout=10,
            capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    weights = os.path.join(REPO, 'models', 'faceage_model.h5')
    if os.path.isfile(weights):
        fp['model_sha256'] = sha256(weights)
    return fp


def _write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


README_TEXT = """FaceAge backup
==============

This folder is a complete copy of one Mac's FaceAge data: every photo session,
every result, the longitudinal series (faceage_history.csv per person) and the
settings. It was written by `faceage backup`, and it is kept up to date by the
FaceAge app after every session that is added to the tracker.

    data/           the FaceAge data folder, file for file (usually ~/FaceAgeData)
    manifest.json   the sha256 of every file in data/, and what machine wrote it
    attic/          earlier versions of files that changed or were removed
                    since the previous backup; nothing here is ever deleted

To use it on a new Mac
----------------------
1. Install FaceAge:   git clone https://github.com/geoffbrown/FaceAge.git
                      ~/Documents/GitHub/FaceAge && ~/Documents/GitHub/FaceAge/tools/install.sh
2. Restore:           faceage restore "<this folder>"
3. Check:             faceage doctor

`restore` re-hashes every file against manifest.json before it touches
anything, and refuses if the copy does not match. Add --merge to fill in only
what the new Mac is missing, or --replace to move its existing data aside
first (nothing is deleted).

Without FaceAge at all, data/ is still readable: photos are ordinary JPEGs and
the series is a CSV. Nothing in here is encrypted or in a private format.
"""


def _write_readme(dest):
    p = os.path.join(dest, 'README.txt')
    try:
        with open(p) as fh:
            if fh.read() == README_TEXT:
                return
    except OSError:
        pass
    with open(p, 'w') as fh:
        fh.write(README_TEXT)


# ----------------------------------------------------------------------------
# settings (shared file with the CLI and the app)
# ----------------------------------------------------------------------------

def read_settings(data_dir=None):
    p = os.path.join(data_dir or DATA, 'settings.json')
    try:
        with open(p) as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def set_backup_dir(dest, data_dir=None):
    data_dir = data_dir or DATA
    dest = os.path.abspath(os.path.expanduser(dest))
    if os.path.abspath(data_dir) == dest or dest.startswith(os.path.abspath(data_dir) + os.sep):
        raise BackupError('the backup cannot live inside the data folder it backs up')
    os.makedirs(data_dir, exist_ok=True)
    st = read_settings(data_dir)
    st['backup_dir'] = dest
    st['backup_set_at'] = _now()
    _write_json(os.path.join(data_dir, 'settings.json'), st)
    return dest


def backup_dir(data_dir=None):
    d = read_settings(data_dir).get('backup_dir')
    return os.path.expanduser(d) if d else None


def default_dest():
    """Where a backup goes when nobody has said: iCloud Drive if this Mac has
    it, because that is the one place that leaves the machine without any
    extra account or service. Otherwise there is no default; the person must
    choose a folder that is synced or on another disk."""
    if os.path.isdir(ICLOUD):
        return os.path.join(ICLOUD, BACKUP_NAME)
    return None


def describe(dest):
    """A short human name for a backup location: 'iCloud Drive' rather than
    the Mobile Documents path nobody recognises."""
    if not dest:
        return ''
    d = os.path.abspath(os.path.expanduser(dest))
    if d.startswith(ICLOUD):
        rel = os.path.relpath(d, ICLOUD)
        return 'iCloud Drive' if rel == BACKUP_NAME else 'iCloud Drive/%s' % rel
    home = os.path.expanduser('~')
    if d.startswith(home + os.sep):
        return '~/' + os.path.relpath(d, home)
    return d


# ----------------------------------------------------------------------------
# backup
# ----------------------------------------------------------------------------

def backup(src=None, dest=None, log=None):
    """Bring dest into line with src. Copies what is new or changed, moves the
    previous version of anything changed or removed into attic/, re-hashes
    every copy it made, and writes the manifest last. Returns a summary."""
    log = log or (lambda *a: None)
    src = os.path.abspath(src or DATA)
    dest = dest or backup_dir(src) or default_dest()
    if not dest:
        raise BackupError('no backup location. Pass one: faceage backup /path/to/folder')
    dest = os.path.abspath(os.path.expanduser(dest))
    if not os.path.isdir(src):
        raise BackupError('nothing to back up: %s does not exist' % src)
    if src == dest or dest.startswith(src + os.sep):
        raise BackupError('the backup cannot live inside the data folder it backs up')
    if not os.path.isdir(os.path.dirname(dest)):
        raise BackupError('%s does not exist (is the disk connected?)' % os.path.dirname(dest))

    data = os.path.join(dest, 'data')
    os.makedirs(data, exist_ok=True)
    prev = read_manifest(dest) or {}
    prev_files = prev.get('files') or {}

    cur, evicted = walk(src)
    if evicted and download_evicted(src, log):
        cur, evicted = walk(src)

    stamp = _stamp()
    attic = os.path.join(dest, 'attic', stamp)
    files, n_new, n_changed, n_same, n_removed, n_bytes = {}, 0, 0, 0, 0, 0

    def to_attic(rel):
        target = os.path.join(data, rel)
        if not os.path.isfile(target):
            return                      # evicted to iCloud, or never there: nothing to keep
        keep = os.path.join(attic, rel)
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        shutil.move(target, keep)

    for rel, (size, mtime) in cur.items():
        p = prev_files.get(rel)
        target = os.path.join(data, rel)
        if p and p.get('size') == size and p.get('mtime') == mtime and _present(target):
            files[rel] = p
            n_same += 1
            continue
        digest = sha256(os.path.join(src, rel))
        entry = {'size': size, 'mtime': mtime, 'sha256': digest}
        if p and p.get('sha256') == digest and _present(target):
            files[rel] = entry         # touched, not changed
            n_same += 1
            continue
        if p:
            to_attic(rel)
            n_changed += 1
        else:
            n_new += 1
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + '.part'
        shutil.copy2(os.path.join(src, rel), tmp)
        if sha256(tmp) != digest:
            os.unlink(tmp)
            raise BackupError('copy of %s did not verify; is the disk healthy?' % rel)
        os.replace(tmp, target)
        files[rel] = entry
        n_bytes += size
        log('  copied %s' % rel)

    offloaded = set(evicted)
    for rel in prev_files:
        if rel in offloaded:
            # Still at the source, just offloaded to iCloud: the copy we
            # already hold is the only one on a disk. Keep it as it is.
            files[rel] = prev_files[rel]
            continue
        if rel not in cur:
            to_attic(rel)
            n_removed += 1
            log('  kept previous %s in attic' % rel)

    manifest = {'format': FORMAT, 'written_at': _now(), 'source': src,
                'wrote': fingerprint(), 'files': files,
                'evicted_at_source': sorted(evicted)}
    _write_json(manifest_path(dest), manifest)
    _write_readme(dest)
    return {'dest': dest, 'files': len(files), 'new': n_new, 'changed': n_changed,
            'unchanged': n_same, 'removed': n_removed, 'bytes': n_bytes,
            'evicted': sorted(evicted), 'written_at': manifest['written_at'],
            'attic': attic if os.path.isdir(attic) else None}


# ----------------------------------------------------------------------------
# verify
# ----------------------------------------------------------------------------

def verify(dest, log=None):
    """Re-hash the mirror against its manifest. ok is True only when every
    listed file is on disk and matches; evicted files are reported apart,
    since they are safe in iCloud but not checkable here."""
    log = log or (lambda *a: None)
    dest = os.path.abspath(os.path.expanduser(dest))
    m = read_manifest(dest)
    if not m:
        raise BackupError('no manifest.json in %s: not a FaceAge backup' % dest)
    data = os.path.join(dest, 'data')
    missing, mismatch, evicted, checked = [], [], [], 0
    for rel, entry in sorted(m['files'].items()):
        path = os.path.join(data, rel)
        if os.path.isfile(path):
            checked += 1
            if sha256(path) != entry.get('sha256'):
                mismatch.append(rel)
                log('  MISMATCH %s' % rel)
        elif os.path.isfile(_stub_of(path)):
            evicted.append(rel)
        else:
            missing.append(rel)
            log('  MISSING  %s' % rel)
    extra = [rel for rel in walk(data)[0] if rel not in m['files']]
    return {'dest': dest, 'ok': not missing and not mismatch, 'checked': checked,
            'listed': len(m['files']), 'missing': missing, 'mismatch': mismatch,
            'evicted': evicted, 'extra': extra, 'written_at': m.get('written_at'),
            'wrote': m.get('wrote') or {}}


# ----------------------------------------------------------------------------
# archive: one file, for AirDrop or a USB stick
# ----------------------------------------------------------------------------

def archive(src=None, out=None, log=None):
    log = log or (lambda *a: None)
    src = os.path.abspath(src or DATA)
    if not os.path.isdir(src):
        raise BackupError('nothing to archive: %s does not exist' % src)
    out = os.path.abspath(os.path.expanduser(out or 'FaceAge-backup-%s.zip' % _stamp()))
    if not out.lower().endswith('.zip'):
        out += '.zip'
    cur, evicted = walk(src)
    if evicted and download_evicted(src, log):
        cur, evicted = walk(src)
    files, total = {}, 0
    tmp = out + '.part'
    with zipfile.ZipFile(tmp, 'w', allowZip64=True) as z:
        for rel, (size, mtime) in cur.items():
            full = os.path.join(src, rel)
            comp = zipfile.ZIP_STORED if rel.lower().endswith(IMAGE_EXTS) else zipfile.ZIP_DEFLATED
            z.write(full, 'data/' + rel, compress_type=comp)
            files[rel] = {'size': size, 'mtime': mtime, 'sha256': sha256(full)}
            total += size
        manifest = {'format': FORMAT, 'written_at': _now(), 'source': src,
                    'wrote': fingerprint(), 'files': files,
                    'evicted_at_source': sorted(evicted)}
        z.writestr('manifest.json', json.dumps(manifest, indent=1, sort_keys=True))
        z.writestr('README.txt', README_TEXT)
    with zipfile.ZipFile(tmp) as z:
        bad = z.testzip()
    if bad:
        os.unlink(tmp)
        raise BackupError('archive did not verify (%s); is the disk healthy?' % bad)
    os.replace(tmp, out)
    return {'out': out, 'files': len(files), 'bytes': total, 'evicted': sorted(evicted),
            'size': os.path.getsize(out)}


# ----------------------------------------------------------------------------
# restore
# ----------------------------------------------------------------------------

def _source_kind(src):
    src = os.path.abspath(os.path.expanduser(src))
    if os.path.isfile(src) and zipfile.is_zipfile(src):
        return 'zip', src
    if os.path.isdir(src):
        if os.path.isfile(manifest_path(src)):
            return 'dir', src
        up = os.path.dirname(src)
        if os.path.basename(src) == 'data' and os.path.isfile(manifest_path(up)):
            return 'dir', up
    raise BackupError('%s is not a FaceAge backup (no manifest.json, and not a .zip '
                      'written by faceage backup --archive)' % src)


def restore(src, data_dir=None, mode='fresh', force=False, log=None):
    """Put a backup into the data folder.

    mode 'fresh'   the data folder must be empty or absent
         'merge'   add what the data folder lacks; never overwrite; report
                   files that exist in both and differ
         'replace' move the existing data folder aside (<data>.archive-<when>,
                   nothing deleted, same as `faceage reset`) and restore into
                   a clean one
    A folder backup is verified in full first and refused on any mismatch
    unless force is set. A zip is checked by CRC as it is read, and every
    restored file is re-hashed against the manifest either way.
    """
    log = log or (lambda *a: None)
    data_dir = os.path.abspath(data_dir or DATA)
    kind, src = _source_kind(src)
    if mode not in ('fresh', 'merge', 'replace'):
        raise BackupError('mode must be fresh, merge or replace')

    if kind == 'dir':
        if src == data_dir or src.startswith(data_dir + os.sep):
            raise BackupError('the backup is inside the data folder; restore it somewhere else')
        m = read_manifest(src)
        if walk(os.path.join(src, 'data'))[1]:
            download_evicted(os.path.join(src, 'data'), log)
        v = verify(src, log)
        if v['evicted']:
            raise BackupError('%d file(s) in the backup are evicted to iCloud and not on this '
                              'disk. Open %s in Finder and download the folder, then retry.'
                              % (len(v['evicted']), src))
        if not v['ok'] and not force:
            raise BackupError('the backup does not match its manifest (%d missing, %d changed). '
                              'Not restoring from a damaged copy; pass --force to override.'
                              % (len(v['missing']), len(v['mismatch'])))
        zf = None
    else:
        zf = zipfile.ZipFile(src)
        try:
            m = json.loads(zf.read('manifest.json').decode('utf-8'))
        except (KeyError, ValueError):
            raise BackupError('%s has no manifest.json: not a FaceAge archive' % src)
        if zf.testzip():
            raise BackupError('%s is damaged (CRC check failed)' % src)

    files = m.get('files') or {}
    existing = walk(data_dir)[0]
    moved_aside = None
    if existing:
        if mode == 'fresh':
            raise BackupError('%s already has data (%d files). Use --merge to add only what is '
                              'missing, or --replace to move it aside first (nothing is deleted).'
                              % (data_dir, len(existing)))
        if mode == 'replace':
            moved_aside = '%s.archive-%s' % (data_dir, _stamp())
            shutil.move(data_dir, moved_aside)
            log('Moved the existing data folder to %s' % moved_aside)
            existing = {}
    os.makedirs(data_dir, exist_ok=True)

    restored, kept, differs = 0, 0, []
    for rel, entry in sorted(files.items()):
        target = os.path.join(data_dir, rel)
        if rel in existing:
            if sha256(target) != entry.get('sha256'):
                differs.append(rel)
            kept += 1
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + '.part'
        if zf:
            with zf.open('data/' + rel) as fin, open(tmp, 'wb') as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
            try:
                os.utime(tmp, ns=(entry['mtime'], entry['mtime']))
            except (KeyError, OSError, TypeError):
                pass
        else:
            shutil.copy2(os.path.join(src, 'data', rel), tmp)
        if sha256(tmp) != entry.get('sha256'):
            os.unlink(tmp)
            raise BackupError('%s did not verify after restore; stopping' % rel)
        os.replace(tmp, target)
        restored += 1
        log('  restored %s' % rel)
    if zf:
        zf.close()

    # The backup location travels with the settings. Keep it only if it exists
    # on this machine, otherwise the app would report a backup it cannot make.
    st = read_settings(data_dir)
    bd = st.get('backup_dir')
    if bd and not os.path.isdir(os.path.dirname(os.path.expanduser(bd))):
        st.pop('backup_dir', None)
        _write_json(os.path.join(data_dir, 'settings.json'), st)
    if kind == 'dir' and not read_settings(data_dir).get('backup_dir'):
        # Restoring from a folder that is still reachable: the natural thing
        # is to keep backing up to it.
        set_backup_dir(src, data_dir)

    return {'data_dir': data_dir, 'restored': restored, 'kept': kept, 'differs': differs,
            'moved_aside': moved_aside, 'source': src, 'written_at': m.get('written_at'),
            'wrote': m.get('wrote') or {}, 'mode': mode}


# ----------------------------------------------------------------------------
# status: cheap, for the app footer and doctor
# ----------------------------------------------------------------------------

def status(data_dir=None, dest=None):
    """What the app footer needs, from stats alone (no hashing): whether a
    backup is set up, whether its folder is reachable right now, when it was
    last written, and how many files have changed since."""
    data_dir = os.path.abspath(data_dir or DATA)
    dest = dest or backup_dir(data_dir)
    out = {'configured': bool(dest), 'dir': dest, 'label': describe(dest),
           'suggested': default_dest(), 'reachable': False, 'last': None,
           'last_host': None, 'files': 0, 'pending': None}
    if not dest:
        return out
    dest = os.path.abspath(os.path.expanduser(dest))
    out['reachable'] = os.path.isdir(os.path.dirname(dest))
    m = read_manifest(dest) if os.path.isdir(dest) else None
    cur = walk(data_dir)[0]
    if not m:
        out['pending'] = len(cur)
        return out
    prev = m.get('files') or {}
    out['last'] = m.get('written_at')
    out['last_host'] = (m.get('wrote') or {}).get('host')
    out['files'] = len(prev)
    changed = sum(1 for rel, (size, mtime) in cur.items()
                  if not prev.get(rel) or prev[rel].get('size') != size
                  or prev[rel].get('mtime') != mtime)
    removed = sum(1 for rel in prev if rel not in cur)
    out['pending'] = changed + removed
    return out


# ----------------------------------------------------------------------------
# command line
# ----------------------------------------------------------------------------

def _fmt_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%d %s' if unit == 'B' else '%.1f %s') % (n, unit)
        n /= 1024.0


def _say(msg=''):
    print(msg)


def brief(s):
    """One line for `faceage doctor`."""
    if not s['configured']:
        return 'NOT SET UP  (faceage backup)'
    where = s['label'] or s['dir']
    if not s['reachable']:
        return '%s -- NOT REACHABLE (disk not connected?)' % where
    if not s['last']:
        return '%s -- never run yet' % where
    if s['pending']:
        return '%s, last %s, %d file(s) changed since' % (where, s['last'], s['pending'])
    return '%s, last %s, up to date' % (where, s['last'])


def cli_backup(args):
    dest, verify_only, status_only, archive_out, prune = None, False, False, None, False
    brief_only = False
    it = iter(args)
    for a in it:
        if a == '--verify':
            verify_only = True
        elif a == '--status':
            status_only = True
        elif a == '--brief':
            status_only = brief_only = True
        elif a == '--archive':
            archive_out = next(it, None) or ''
        elif a == '--prune-attic':
            prune = True
        elif a.startswith('-'):
            raise BackupError('unknown option %s' % a)
        else:
            dest = a

    if archive_out is not None:
        r = archive(DATA, archive_out or None, _say)
        _say('Archived %d file(s), %s, to %s' % (r['files'], _fmt_bytes(r['size']), r['out']))
        if r['evicted']:
            _say('WARNING: %d file(s) are evicted to iCloud and NOT in the archive' % len(r['evicted']))
            return 2
        return 0

    if status_only:
        s = status(DATA, dest)
        if brief_only:
            _say(brief(s))
            return 0
        if not s['configured']:
            _say('backup: not set up' + (' (suggested: %s)' % s['suggested'] if s['suggested'] else ''))
            return 1
        _say('backup location : %s' % s['dir'])
        if not s['reachable']:
            _say('reachable       : NO (disk not connected, or folder moved)')
            return 1
        _say('last backup     : %s%s' % (s['last'] or 'never',
                                         (' from %s' % s['last_host']) if s['last_host'] else ''))
        _say('files in backup : %d' % s['files'])
        _say('changed since   : %d' % (s['pending'] or 0))
        return 0 if s['last'] and not s['pending'] else 1

    if verify_only:
        d = dest or backup_dir(DATA)
        if not d:
            raise BackupError('no backup location set. faceage backup /path/to/folder first')
        v = verify(d, _say)
        _say('%s: %d of %d file(s) verified%s' % (
            'OK' if v['ok'] else 'FAILED', v['checked'], v['listed'],
            (', %d evicted to iCloud (not checkable here)' % len(v['evicted'])) if v['evicted'] else ''))
        if v['missing']:
            _say('missing : %d' % len(v['missing']))
        if v['mismatch']:
            _say('changed : %d' % len(v['mismatch']))
        if v['extra']:
            _say('not in manifest: %d (harmless; the next backup rewrites the manifest)' % len(v['extra']))
        return 0 if v['ok'] else 1

    if dest:
        dest = set_backup_dir(dest, DATA)
        _say('Backups will go to %s' % dest)
    elif not backup_dir(DATA):
        d = default_dest()
        if not d:
            raise BackupError('no backup location. This Mac has no iCloud Drive folder, so pass '
                              'one that leaves the machine:  faceage backup /Volumes/Disk/FaceAge')
        set_backup_dir(d, DATA)
        _say('Backups will go to iCloud Drive: %s' % d)

    if prune:
        att = os.path.join(backup_dir(DATA), 'attic')
        if os.path.isdir(att):
            shutil.rmtree(att)
            _say('Removed earlier versions under %s' % att)

    r = backup(DATA, None, _say)
    _say('Backed up %d file(s) to %s: %d new, %d changed, %d unchanged%s (%s copied)' % (
        r['files'], r['dest'], r['new'], r['changed'], r['unchanged'],
        (', %d removed (kept in attic)' % r['removed']) if r['removed'] else '',
        _fmt_bytes(r['bytes'])))
    if r['evicted']:
        _say('WARNING: %d file(s) are evicted to iCloud and could not be backed up:' % len(r['evicted']))
        for rel in r['evicted'][:10]:
            _say('  ' + rel)
        return 2
    return 0


def cli_restore(args):
    src, mode, force = None, 'fresh', False
    for a in args:
        if a == '--merge':
            mode = 'merge'
        elif a == '--replace':
            mode = 'replace'
        elif a == '--force':
            force = True
        elif a.startswith('-'):
            raise BackupError('unknown option %s' % a)
        else:
            src = a
    if not src:
        raise BackupError('usage: faceage restore <backup folder or .zip> [--merge|--replace]')
    r = restore(src, DATA, mode, force, _say)
    w = r['wrote']
    _say('Restored %d file(s) into %s%s' % (
        r['restored'], r['data_dir'],
        (' (%d already there, left alone)' % r['kept']) if r['kept'] else ''))
    _say('Backup written %s%s' % (r['written_at'] or '?',
                                  (' on %s' % w['host']) if w.get('host') else ''))
    if r['moved_aside']:
        _say('The previous data folder is at %s (nothing deleted)' % r['moved_aside'])
    if r['differs']:
        _say('%d file(s) exist here AND in the backup with different contents; the local '
             'copy was kept:' % len(r['differs']))
        for rel in r['differs'][:10]:
            _say('  ' + rel)
    _say('Next: faceage doctor, then faceage app')
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ''
    try:
        if cmd == 'backup':
            return cli_backup(argv[1:])
        if cmd == 'restore':
            return cli_restore(argv[1:])
        if cmd == 'status':
            return cli_backup(['--status'] + argv[1:])
        sys.stderr.write('usage: faceage_backup.py backup [DEST] [--verify|--status|'
                         '--archive FILE.zip|--prune-attic]\n'
                         '       faceage_backup.py restore SRC [--merge|--replace] [--force]\n')
        return 1
    except BackupError as exc:
        sys.stderr.write('error: %s\n' % exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
