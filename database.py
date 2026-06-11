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

        # Chats table to track when the bot started monitoring a group
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                monitored_since REAL
            )
        ''')

        # Transport statistics
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transport_stats (
                addr TEXT PRIMARY KEY,
                msgs_sent INTEGER DEFAULT 0,
                msgs_received INTEGER DEFAULT 0,
                last_sent_at INTEGER,
                last_received_at INTEGER
            )
        ''')

        # Catalog chats table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS catalog_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE,
                name TEXT,
                description TEXT,
                is_private INTEGER DEFAULT 0,
                member_count INTEGER DEFAULT 0,
                invite_link TEXT,
                welcome_enabled INTEGER DEFAULT 0,
                welcome_text TEXT
            )
        ''')

        # Upgrade existing table schema if necessary
        cursor.execute("PRAGMA table_info(catalog_chats)")
        columns = [info[1] for info in cursor.fetchall()]
        if "welcome_enabled" not in columns:
            cursor.execute("ALTER TABLE catalog_chats ADD COLUMN welcome_enabled INTEGER DEFAULT 0")
        if "welcome_text" not in columns:
            cursor.execute("ALTER TABLE catalog_chats ADD COLUMN welcome_text TEXT")

        # Catalog channels table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS catalog_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE,
                name TEXT,
                description TEXT,
                member_count INTEGER DEFAULT 0,
                invite_link TEXT
            )
        ''')

        # Pending join requests table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                catalog_id INTEGER,
                chat_id INTEGER,
                requester_contact_id INTEGER,
                requester_name TEXT,
                message TEXT,
                created_at REAL,
                approved INTEGER DEFAULT 0
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

def get_chat_monitored_since(chat_id: int) -> float:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT monitored_since FROM chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def set_chat_monitored_since(chat_id: int, timestamp: float):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO chats (chat_id, monitored_since) VALUES (?, ?)", (chat_id, timestamp))
        conn.commit()
        conn.close()

def get_admin_fingerprint():
    """Get the saved admin DC fingerprint."""
    return get_config("admin_dc_fingerprint")

def set_admin_fingerprint(fp):
    """Set the admin DC fingerprint."""
    set_config("admin_dc_fingerprint", fp)

def increment_transport_sent(addr: str):
    """Increment the sent counter for a transport address."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transport_stats (addr, msgs_sent, msgs_received, last_sent_at)
            VALUES (?, 1, 0, CAST(strftime('%s','now') AS INTEGER))
            ON CONFLICT(addr) DO UPDATE SET
                msgs_sent = msgs_sent + 1,
                last_sent_at = CAST(strftime('%s','now') AS INTEGER)
        ''', (addr,))
        conn.commit()
        conn.close()

def increment_transport_received(addr: str):
    """Increment the received counter for a transport address."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transport_stats (addr, msgs_sent, msgs_received, last_received_at)
            VALUES (?, 0, 1, CAST(strftime('%s','now') AS INTEGER))
            ON CONFLICT(addr) DO UPDATE SET
                msgs_received = msgs_received + 1,
                last_received_at = CAST(strftime('%s','now') AS INTEGER)
        ''', (addr,))
        conn.commit()
        conn.close()

def get_all_transport_stats() -> list[dict]:
    """Get statistics for all tracked transports."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transport_stats ORDER BY msgs_sent + msgs_received DESC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def add_catalog_chat(chat_id: int, name: str, description: str, member_count: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO catalog_chats (chat_id, name, description, member_count)
            VALUES (?, ?, ?, ?)
        ''', (chat_id, name, description, member_count))
        conn.commit()
        conn.close()

def remove_catalog_chat(chat_id: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM catalog_chats WHERE chat_id = ?", (chat_id,))
        conn.commit()
        conn.close()

def get_all_catalog_chats() -> list[dict]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM catalog_chats ORDER BY id ASC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def get_catalog_chat_by_chat_id(chat_id: int) -> dict:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM catalog_chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def get_catalog_chat_by_id(catalog_id: int) -> dict:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM catalog_chats WHERE id = ?", (catalog_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def update_catalog_chat_privacy(chat_id: int, is_private: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE catalog_chats SET is_private = ? WHERE chat_id = ?", (is_private, chat_id))
        conn.commit()
        conn.close()

def update_catalog_chat_member_count(chat_id: int, member_count: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE catalog_chats SET member_count = ? WHERE chat_id = ?", (member_count, chat_id))
        conn.commit()
        conn.close()

def update_catalog_chat_invite_link(chat_id: int, invite_link: str):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE catalog_chats SET invite_link = ? WHERE chat_id = ?", (invite_link, chat_id))
        conn.commit()
        conn.close()

def get_all_monitored_chats() -> list[int]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM chats")
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in rows]

def add_pending_request(catalog_id: int, chat_id: int, requester_contact_id: int, requester_name: str, message: str) -> int:
    import time
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO pending_requests (catalog_id, chat_id, requester_contact_id, requester_name, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (catalog_id, chat_id, requester_contact_id, requester_name, message, time.time()))
        request_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return request_id

def get_pending_request(request_id: int) -> dict:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pending_requests WHERE id = ?", (request_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def approve_pending_request(request_id: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE pending_requests SET approved = 1 WHERE id = ?", (request_id,))
        conn.commit()
        conn.close()

def decline_pending_request(request_id: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE pending_requests SET approved = 2 WHERE id = ?", (request_id,))
        conn.commit()
        conn.close()

def update_catalog_chat_welcome(chat_id: int, welcome_enabled: int, welcome_text: str):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE catalog_chats SET welcome_enabled = ?, welcome_text = ? WHERE chat_id = ?", 
                       (welcome_enabled, welcome_text, chat_id))
        conn.commit()
        conn.close()

def add_catalog_channel(chat_id: int, name: str, description: str, member_count: int, invite_link: str):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO catalog_channels (chat_id, name, description, member_count, invite_link)
            VALUES (?, ?, ?, ?, ?)
        ''', (chat_id, name, description, member_count, invite_link))
        conn.commit()
        conn.close()

def remove_catalog_channel(chat_id: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM catalog_channels WHERE chat_id = ?", (chat_id,))
        conn.commit()
        conn.close()

def get_all_catalog_channels() -> list[dict]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM catalog_channels ORDER BY id ASC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def get_catalog_channel_by_chat_id(chat_id: int) -> dict:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM catalog_channels WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def get_catalog_channel_by_id(catalog_id: int) -> dict:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM catalog_channels WHERE id = ?", (catalog_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def update_catalog_channel_member_count(chat_id: int, member_count: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE catalog_channels SET member_count = ? WHERE chat_id = ?", (member_count, chat_id))
        conn.commit()
        conn.close()

init_db()

