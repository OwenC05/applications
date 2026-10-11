"""Construct synthetic pre-upgrade history in an owned test database copy only."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def preupgrade_copy(store, target):
    """Yield legacy SQL before the caller opens Store(target) for actual upgrade.

    The source is never modified. This is a fixture builder, not an application
    runtime switch or an admission bypass on an active store.
    """
    target = Path(target).resolve()
    if target == store.root or target.exists():
        raise ValueError('Legacy fixture requires a new, separate owned directory')
    target.mkdir(parents=True)
    with sqlite3.connect(store.db) as source:
        db = sqlite3.connect(target / 'evidence.sqlite3')
        try:
            source.backup(db)
            db.execute('BEGIN IMMEDIATE')
            db.execute('DROP TABLE IF EXISTS drafting_admission_bindings')
            db.execute('DROP TABLE IF EXISTS drafting_admission_heads')
            db.execute('PRAGMA user_version=0')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
