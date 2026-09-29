"""Persist alert state across restarts; never include exception URLs or credentials."""
from contextlib import closing
import sqlite3
from datetime import datetime, timedelta, timezone

from .app import notifier_from_config


class SourceHealth:
    def __init__(self, config):
        self.config = config
        with closing(sqlite3.connect(config['database'])) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS source_health (source TEXT PRIMARY KEY, failed INTEGER NOT NULL, alerted_at TEXT)')

    def failure(self, source, error):
        try:
            with closing(sqlite3.connect(self.config['database'])) as db, db:
                row = db.execute('SELECT failed, alerted_at FROM source_health WHERE source=?', (source,)).fetchone()
                now = datetime.now(timezone.utc)
                if row and row[0] and row[1] and now - datetime.fromisoformat(row[1]) < timedelta(hours=6):
                    return
                notifier = notifier_from_config(self.config, required=True)
                login = 'not logged in' in str(error) or 'checkpoint' in str(error).lower()
                message = ('⚠️ Facebook: потерян вход в аккаунт. Новые объявления не проверяются; нужно восстановить сессию.' if login else
                           f'⚠️ FlatWatch: проверка {source} завершилась ошибкой ({type(error).__name__}). Требуется проверка журнала на VPS.')
                notifier.send_status(message)
                db.execute('INSERT INTO source_health VALUES (?,1,?) ON CONFLICT(source) DO UPDATE SET failed=1, alerted_at=excluded.alerted_at', (source, now.isoformat()))
        except Exception as exc:
            print(f'Health alert could not be delivered: {type(exc).__name__}', flush=True)

    def success(self, source):
        try:
            with closing(sqlite3.connect(self.config['database'])) as db, db:
                row = db.execute('SELECT failed FROM source_health WHERE source=?', (source,)).fetchone()
                if row and row[0]:
                    notifier_from_config(self.config, required=True).send_status(f'✅ FlatWatch: проверка {source} восстановлена, обход завершён успешно.')
                    db.execute('UPDATE source_health SET failed=0 WHERE source=?', (source,))
        except Exception as exc:
            print(f'Health recovery alert could not be delivered: {type(exc).__name__}', flush=True)
