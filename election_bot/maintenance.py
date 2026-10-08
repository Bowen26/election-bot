"""Lossless, bounded archival of old high-volume measurement events."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

HOT_DAYS = 7
BATCH_LIMIT = 2000
KINDS = ('quote_snapshot','shadow_decision','exit_shadow','scan_visit','scan_quote','decision')


def archive_events(journal, runtime, now=None, limit=BATCH_LIMIT):
    now = time.time() if now is None else now
    if type(limit) is not int or not 1 <= limit <= BATCH_LIMIT:
        raise ValueError('Invalid archive batch limit')
    db=journal.db
    if db.in_transaction:
        raise RuntimeError('Event archive requires its own transaction')
    cutoff=now-HOT_DAYS*86400
    rows=[tuple(r) for r in db.execute('SELECT rowid,at,kind,detail FROM events WHERE at<? AND kind IN ('+
         ','.join('?' for _ in KINDS)+') ORDER BY at,rowid LIMIT ?', (cutoff,*KINDS,limit))]
    if not rows:
        return None
    directory=Path(runtime)/'event-archive'
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    name='events-'+uuid.uuid4().hex+'.jsonl.gz'
    target=directory/name; temporary=directory/(name+'.tmp')
    body=''.join(json.dumps({'rowid':r[0],'at':r[1],'kind':r[2],'detail':r[3]},ensure_ascii=True)+'\n' for r in rows).encode()
    digest=hashlib.sha256(body).hexdigest()
    try:
        with temporary.open('xb') as raw:
            os.chmod(temporary,0o600)
            with gzip.GzipFile(fileobj=raw,mode='wb',filename='',mtime=0) as compressed:
                compressed.write(body)
            raw.flush();os.fsync(raw.fileno())
        temporary.replace(target)
        fd=os.open(directory,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        with gzip.open(target,'rb') as stored:
            if hashlib.sha256(stored.read()).hexdigest()!=digest:
                raise RuntimeError('Archive verification failed; original events retained')
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS event_archives (
                file TEXT PRIMARY KEY, created REAL NOT NULL, rows INTEGER NOT NULL,
                first_at REAL NOT NULL, last_at REAL NOT NULL, sha256 TEXT NOT NULL)''')
            removed=0
            for row in rows:
                removed+=db.execute('DELETE FROM events WHERE rowid=? AND at=? AND kind=? AND detail=?',row).rowcount
            if removed!=len(rows):
                raise RuntimeError('Events changed while archiving; deletion rolled back')
            db.execute('INSERT INTO event_archives VALUES (?,?,?,?,?,?)',
                       (name,now,len(rows),rows[0][1],rows[-1][1],digest))
    finally:
        temporary.unlink(missing_ok=True)
    return {'file':'event-archive/'+name,'rows':len(rows),'hot_days':HOT_DAYS,'sha256':digest}


def retention_info(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_archives'").fetchone():
        return {'archived_rows':0,'hot_days':HOT_DAYS}
    row=db.execute('SELECT COUNT(*),COALESCE(SUM(rows),0),MIN(first_at),MAX(last_at) FROM event_archives').fetchone()
    return {'archive_files':row[0],'archived_rows':row[1],'first_archived_at':row[2],
            'last_archived_at':row[3],'hot_days':HOT_DAYS,
            'note':'Older measurement events are preserved in event-archive/*.jsonl.gz. '
                   'This report reads the hot SQLite journal only; longer lookbacks exclude archived measurements. '
                   'Executions, order records and order-linked signal events are retained in SQLite.'}
