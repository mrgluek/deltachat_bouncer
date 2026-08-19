import os
import sqlite3
import threading
import time

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
                monitored_since REAL,
                autokick_days INTEGER DEFAULT 0
            )
        ''')

        cursor.execute("PRAGMA table_info(chats)")
        columns = [info[1] for info in cursor.fetchall()]
        if "autokick_days" not in columns:
            cursor.execute("ALTER TABLE chats ADD COLUMN autokick_days INTEGER DEFAULT 0")

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
                invite_msg_id INTEGER,
                welcome_enabled INTEGER DEFAULT 0,
                welcome_text TEXT
            )
        ''')

        # Upgrade existing table schema if necessary
        cursor.execute("PRAGMA table_info(catalog_chats)")
        columns = [info[1] for info in cursor.fetchall()]
        if "invite_msg_id" not in columns:
            cursor.execute("ALTER TABLE catalog_chats ADD COLUMN invite_msg_id INTEGER")
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

        # Contact first-seen tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contact_first_seen (
                contact_id INTEGER PRIMARY KEY,
                first_seen_at REAL
            )
        ''')

        # CMPing monitoring: tracked server domains
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cmping_monitors (
                domain TEXT PRIMARY KEY,
                added_at REAL
            )
        ''')

        # CMPing monitoring: chats subscribed to alerts
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cmping_report_chats (
                chat_id INTEGER PRIMARY KEY,
                enabled_at REAL
            )
        ''')

        # CMPing monitoring: results persistence
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cmping_results (
                src TEXT,
                dst TEXT,
                success INTEGER,
                error TEXT,
                avg REAL,
                checked_at REAL,
                PRIMARY KEY (src, dst)
            )
        ''')

        # CMPing monitoring: history of pings
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cmping_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src TEXT,
                dst TEXT,
                avg REAL,
                checked_at REAL
            )
        ''')

        # CMPing monitoring: incidents
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cmping_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'ongoing',
                started_at INTEGER NOT NULL,
                resolved_at INTEGER,
                summary TEXT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cmping_incidents_status ON cmping_incidents(status)')

        # CMPing monitoring: incident message IDs per chat
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cmping_incident_messages (
                incident_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                msg_id INTEGER,
                PRIMARY KEY (incident_id, chat_id)
            )
        ''')

        # CMPing monitoring: downtime events history
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cmping_downtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server TEXT NOT NULL,
                went_down_at INTEGER NOT NULL,
                went_up_at INTEGER,
                error_msg TEXT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cmping_downtime_server ON cmping_downtime_events(server)')

        # Away status tracking table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS away_status (
                contact_id INTEGER PRIMARY KEY,
                away_text TEXT,
                updated_at REAL
            )
        ''')

        # Away notifications tracking table (debounce)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS away_notifications (
                away_user_id INTEGER,
                recipient_id INTEGER,
                away_updated_at REAL,
                PRIMARY KEY (away_user_id, recipient_id, away_updated_at)
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
        cursor.execute("SELECT 1 FROM chats WHERE chat_id = ?", (chat_id,))
        if cursor.fetchone():
            cursor.execute("UPDATE chats SET monitored_since = ? WHERE chat_id = ?", (timestamp, chat_id))
        else:
            cursor.execute("INSERT INTO chats (chat_id, monitored_since, autokick_days) VALUES (?, ?, 0)", (chat_id, timestamp))
        conn.commit()
        conn.close()

def set_chat_autokick(chat_id: int, days: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM chats WHERE chat_id = ?", (chat_id,))
        if cursor.fetchone():
            cursor.execute("UPDATE chats SET autokick_days = ? WHERE chat_id = ?", (days, chat_id))
        else:
            cursor.execute("INSERT INTO chats (chat_id, monitored_since, autokick_days) VALUES (?, ?, ?)", (chat_id, time.time(), days))
        conn.commit()
        conn.close()

def get_chat_autokick(chat_id: int) -> int:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT autokick_days FROM chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if (row and row[0] is not None) else 0

def get_all_autokick_chats() -> list[tuple[int, int]]:
    """Return list of (chat_id, autokick_days) where autokick_days > 0."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, autokick_days FROM chats WHERE autokick_days > 0")
        rows = cursor.fetchall()
        conn.close()
        return [(r[0], r[1]) for r in rows]

def get_admin_fingerprint():
    """Get the saved admin DC fingerprint."""
    return get_config("admin_dc_fingerprint")

def set_admin_fingerprint(fp):
    """Set the admin DC fingerprint."""
    set_config("admin_dc_fingerprint", fp)

def get_contact_first_seen(contact_id: int) -> float:
    """Get the timestamp when the bot first saw this contact. Returns None if unknown."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT first_seen_at FROM contact_first_seen WHERE contact_id = ?", (contact_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def ensure_contact_first_seen(contact_id: int, timestamp: float):
    """Record first-seen time for a contact if not already known (INSERT OR IGNORE)."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO contact_first_seen (contact_id, first_seen_at) VALUES (?, ?)",
            (contact_id, timestamp)
        )
        conn.commit()
        conn.close()

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

def update_catalog_chat_invite_link(chat_id: int, invite_link: str, invite_msg_id: int = None):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE catalog_chats SET invite_link = ?, invite_msg_id = ? WHERE chat_id = ?", (invite_link, invite_msg_id, chat_id))
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

def update_catalog_channel_description(catalog_id: int, description: str):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE catalog_channels SET description = ? WHERE id = ?", (description, catalog_id))
        conn.commit()
        conn.close()

def update_catalog_chat_description(catalog_id: int, description: str):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE catalog_chats SET description = ? WHERE id = ?", (description, catalog_id))
        conn.commit()
        conn.close()

# --- CMPing monitoring functions ---

def add_cmping_monitor(domain: str):
    """Add a server domain to cmping monitoring."""
    import time
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO cmping_monitors (domain, added_at) VALUES (?, ?)",
            (domain.strip().lower(), time.time())
        )
        conn.commit()
        conn.close()

def remove_cmping_monitor(domain: str) -> bool:
    """Remove a server domain from cmping monitoring. Returns True if removed."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cmping_monitors WHERE domain = ?", (domain.strip().lower(),))
        removed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return removed

def get_all_cmping_monitors() -> list[str]:
    """Get all monitored server domains."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT domain FROM cmping_monitors ORDER BY added_at ASC")
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in rows]

def is_cmping_monitor(domain: str) -> bool:
    """Check if a domain is in cmping monitoring."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM cmping_monitors WHERE domain = ?", (domain.strip().lower(),))
        row = cursor.fetchone()
        conn.close()
        return row is not None

def add_cmping_report_chat(chat_id: int):
    """Subscribe a chat to cmping monitoring alerts."""
    import time
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO cmping_report_chats (chat_id, enabled_at) VALUES (?, ?)",
            (chat_id, time.time())
        )
        conn.commit()
        conn.close()

def remove_cmping_report_chat(chat_id: int) -> bool:
    """Unsubscribe a chat from cmping monitoring alerts. Returns True if removed."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cmping_report_chats WHERE chat_id = ?", (chat_id,))
        removed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return removed

def get_all_cmping_report_chats() -> list[int]:
    """Get all chat IDs subscribed to cmping monitoring alerts."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM cmping_report_chats")
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in rows]

def is_cmping_report_chat(chat_id: int) -> bool:
    """Check if a chat is subscribed to cmping monitoring alerts."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM cmping_report_chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
        return row is not None

def get_cmping_report_chat_enabled_at(chat_id: int) -> float:
    """Get the timestamp when a chat was subscribed to cmping monitoring."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT enabled_at FROM cmping_report_chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

# --- CMPing results persistence ---

def save_cmping_result(src: str, dst: str, success: bool, error: str, avg: float, checked_at: float):
    """Save or update a cmping test result in the database."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO cmping_results (src, dst, success, error, avg, checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (src.strip().lower(), dst.strip().lower(), 1 if success else 0, error, avg, checked_at)
        )
        conn.commit()
        conn.close()

def get_all_cmping_results() -> dict:
    """Load all cmping results from the database."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT src, dst, success, error, avg, checked_at FROM cmping_results")
        rows = cursor.fetchall()
        conn.close()

        results = {}
        for r in rows:
            src, dst, success, error, avg, checked_at = r
            results[(src, dst)] = {
                "success": bool(success),
                "error": error,
                "avg": avg,
                "checked_at": checked_at
            }
        return results

def delete_cmping_results_for_domain(domain: str):
    """Delete all results involving a specific domain."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM cmping_results WHERE src = ? OR dst = ?",
            (domain.strip().lower(), domain.strip().lower())
        )
        conn.commit()
        conn.close()

# --- CMPing history persistence ---

def add_cmping_history(src: str, dst: str, avg: float, checked_at: float):
    """Add a successful cmping measurement to history and keep the database small."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO cmping_history (src, dst, avg, checked_at) VALUES (?, ?, ?, ?)",
            (src.strip().lower(), dst.strip().lower(), avg, checked_at)
        )
        # Keep table limited to the last 5000 records
        cursor.execute(
            """
            DELETE FROM cmping_history WHERE id NOT IN (
                SELECT id FROM cmping_history ORDER BY checked_at DESC LIMIT 5000
            )
            """
        )
        conn.commit()
        conn.close()

def get_average_ping_for_server(domain: str, limit: int = 100) -> tuple:
    """Calculate the average ping in ms for a server based on its last N measurements.
    Returns a tuple (avg_ping_ms, count). If no measurements, returns (None, 0).
    """
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Find latest N measurements where server was src or dst
        cursor.execute(
            """
            SELECT avg FROM cmping_history
            WHERE src = ? OR dst = ?
            ORDER BY checked_at DESC
            LIMIT ?
            """,
            (domain.strip().lower(), domain.strip().lower(), limit)
        )
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return None, 0
            
        pings = [r[0] for r in rows]
        return sum(pings) / len(pings), len(pings)

def delete_cmping_history_for_domain(domain: str):
    """Delete all history records involving a specific domain."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM cmping_history WHERE src = ? OR dst = ?",
            (domain.strip().lower(), domain.strip().lower())
        )
        conn.commit()
        conn.close()

# --- CMPing Incidents & Downtime History ---

def create_cmping_incident(started_at: int = None) -> int:
    if started_at is None:
        started_at = int(time.time())
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO cmping_incidents (status, started_at) VALUES ('ongoing', ?)",
            (started_at,)
        )
        incident_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return incident_id

def get_active_cmping_incident() -> dict | None:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM cmping_incidents WHERE status = 'ongoing' ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def get_cmping_incident_by_id(incident_id: int) -> dict | None:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM cmping_incidents WHERE id = ?",
            (incident_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def get_recent_cmping_incidents(limit: int = 10) -> list[dict]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM cmping_incidents ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def resolve_cmping_incident(incident_id: int, resolved_at: int = None, summary: str = ""):
    if resolved_at is None:
        resolved_at = int(time.time())
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cmping_incidents SET status = 'resolved', resolved_at = ?, summary = ? WHERE id = ?",
            (resolved_at, summary, incident_id)
        )
        conn.commit()
        conn.close()

def set_cmping_incident_msg_id(incident_id: int, chat_id: int, msg_id: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO cmping_incident_messages (incident_id, chat_id, msg_id) VALUES (?, ?, ?)",
            (incident_id, chat_id, msg_id)
        )
        conn.commit()
        conn.close()

def get_cmping_incident_msg_ids(incident_id: int) -> dict[int, int]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, msg_id FROM cmping_incident_messages WHERE incident_id = ?",
            (incident_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}

def record_cmping_server_down(server: str, went_down_at: int = None, error_msg: str = ""):
    if went_down_at is None:
        went_down_at = int(time.time())
    server_norm = server.strip().lower()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM cmping_downtime_events WHERE server = ? AND went_up_at IS NULL",
            (server_norm,)
        )
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "INSERT INTO cmping_downtime_events (server, went_down_at, went_up_at, error_msg) VALUES (?, ?, NULL, ?)",
                (server_norm, went_down_at, error_msg)
            )
        else:
            cursor.execute(
                "UPDATE cmping_downtime_events SET error_msg = ? WHERE id = ?",
                (error_msg, row[0])
            )
        conn.commit()
        conn.close()

def record_cmping_server_up(server: str, went_up_at: int = None):
    if went_up_at is None:
        went_up_at = int(time.time())
    server_norm = server.strip().lower()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cmping_downtime_events SET went_up_at = ? WHERE server = ? AND went_up_at IS NULL",
            (went_up_at, server_norm)
        )
        conn.commit()
        conn.close()

def get_server_cmping_downtime_events(server: str, limit: int = 10) -> list[dict]:
    server_norm = server.strip().lower()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM cmping_downtime_events WHERE server = ? ORDER BY went_down_at DESC LIMIT ?",
            (server_norm, limit)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def get_all_cmping_downtime_events(limit: int = 10) -> list[dict]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM cmping_downtime_events ORDER BY went_down_at DESC LIMIT ?",
            (limit,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def set_away_status(contact_id: int, away_text: str):
    """Set the away status text for a contact."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO away_status (contact_id, away_text, updated_at) VALUES (?, ?, ?)",
            (contact_id, away_text, time.time())
        )
        # Clear debounce notifications for this user when status updates/resets
        cursor.execute("DELETE FROM away_notifications WHERE away_user_id = ?", (contact_id,))
        conn.commit()
        conn.close()

def remove_away_status(contact_id: int):
    """Remove the away status for a contact."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM away_status WHERE contact_id = ?",
            (contact_id,)
        )
        # Clear notifications for this user
        cursor.execute("DELETE FROM away_notifications WHERE away_user_id = ?", (contact_id,))
        conn.commit()
        conn.close()

def get_away_status(contact_id: int) -> str | None:
    """Get the away status text for a contact, or None if not away."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT away_text FROM away_status WHERE contact_id = ?",
            (contact_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_away_status_details(contact_id: int) -> tuple[str, float] | None:
    """Get the away status text and updated_at timestamp for a contact, or None if not away."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT away_text, updated_at FROM away_status WHERE contact_id = ?",
            (contact_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return (row[0], row[1]) if row else None

def has_notified_away(away_user_id: int, recipient_id: int, away_updated_at: float) -> bool:
    """Check if a recipient has already been notified about this specific away status."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM away_notifications WHERE away_user_id = ? AND recipient_id = ? AND away_updated_at = ?",
            (away_user_id, recipient_id, away_updated_at)
        )
        row = cursor.fetchone()
        conn.close()
        return row is not None

def mark_notified_away(away_user_id: int, recipient_id: int, away_updated_at: float):
    """Mark that a recipient has been notified about this specific away status."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO away_notifications (away_user_id, recipient_id, away_updated_at) VALUES (?, ?, ?)",
            (away_user_id, recipient_id, away_updated_at)
        )
        conn.commit()
        conn.close()

def get_notified_recipients(away_user_id: int) -> list[int]:
    """Get all recipient IDs who were notified about this user's away status."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT recipient_id FROM away_notifications WHERE away_user_id = ?",
            (away_user_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in rows]

def delete_cmping_result(src: str, dst: str):
    """Delete a specific cmping result from the database."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM cmping_results WHERE src = ? AND dst = ?",
            (src.strip().lower(), dst.strip().lower())
        )
        conn.commit()
        conn.close()

init_db()




