import os
import sqlite3
import threading

DB_PATH = os.getenv("DB_PATH", "bouncer.db")
_lock = threading.Lock()

def init_db():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Config table for admin_dc_email, admin_dc_fingerprint, last_run, etc.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        conn.commit()
        conn.close()

def set_config(key: str, value: str):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()

def get_config(key: str) -> str:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_admin_fingerprint():
    """Get the saved admin DC fingerprint."""
    return get_config("admin_dc_fingerprint")

def set_admin_fingerprint(fp):
    """Set the admin DC fingerprint."""
    set_config("admin_dc_fingerprint", fp)

init_db()
