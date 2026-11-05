import os
import sqlite3
import datetime
import shutil
import uuid
import zipfile

from cryptography.fernet import Fernet

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
ATTACH_DIR = os.path.join(INSTANCE_DIR, "attachments")
DB_PATH = os.path.join(INSTANCE_DIR, "personal_database.db")
KEY_PATH = os.path.join(INSTANCE_DIR, "secret.key")

os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(ATTACH_DIR, exist_ok=True)


def _load_or_create_key():
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(KEY_PATH, "wb") as f:
        f.write(key)
    return key


_fernet = Fernet(_load_or_create_key())


def encrypt(text):
    if text is None:
        text = ""
    return _fernet.encrypt(text.encode("utf-8"))


def decrypt(token):
    if not token:
        return ""
    try:
        return _fernet.decrypt(bytes(token)).decode("utf-8")
    except Exception:
        return "[unable to decrypt]"


def encrypt_bytes(data):
    return _fernet.encrypt(data)


def decrypt_bytes(token):
    return _fernet.decrypt(bytes(token))


# Max size for a single uploaded file/image (25 MB). Files are stored encrypted
# on disk (not as SQLite blobs) so large attachments don't bloat the database.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# File types this app understands, grouped so the UI can show a sensible icon
# and so unexpected/unsafe file types are rejected up front.
ALLOWED_EXTENSIONS = {
    # Documents
    ".pdf": "doc", ".doc": "doc", ".docx": "doc", ".odt": "doc", ".rtf": "doc", ".txt": "doc",
    ".xls": "sheet", ".xlsx": "sheet", ".csv": "sheet",
    ".ppt": "slide", ".pptx": "slide",
    # Images
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".heic": "image", ".heif": "image", ".svg": "image",
}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            name_enc BLOB NOT NULL,
            email_enc BLOB,
            phone_enc BLOB,
            notes_enc BLOB,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id INTEGER NOT NULL,
            filename_enc BLOB NOT NULL,
            stored_name TEXT NOT NULL,
            content_type TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (record_id) REFERENCES records (id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()


def add_record(category, name, email, phone, notes):
    now = datetime.datetime.utcnow().isoformat()
    conn = get_connection()
    conn.execute(
        """INSERT INTO records
           (category, name_enc, email_enc, phone_enc, notes_enc, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (category, encrypt(name), encrypt(email), encrypt(phone), encrypt(notes), now, now),
    )
    conn.commit()
    conn.close()


def update_record(record_id, category, name, email, phone, notes):
    now = datetime.datetime.utcnow().isoformat()
    conn = get_connection()
    conn.execute(
        """UPDATE records SET category=?, name_enc=?, email_enc=?, phone_enc=?, notes_enc=?, updated_at=?
           WHERE id=?""",
        (category, encrypt(name), encrypt(email), encrypt(phone), encrypt(notes), now, record_id),
    )
    conn.commit()
    conn.close()


def delete_record(record_id):
    for att in get_attachments(record_id):
        delete_attachment(att["id"])
    conn = get_connection()
    conn.execute("DELETE FROM records WHERE id=?", (record_id,))
    conn.commit()
    conn.close()


def get_record(record_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    conn.close()
    if not row:
        return None
    record = _decrypt_row(row)
    record["attachments"] = get_attachments(record_id)
    return record


def add_attachment(record_id, filename, content_type, data):
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("File is too large (max 25 MB).")
    stored_name = f"{uuid.uuid4().hex}.enc"
    with open(os.path.join(ATTACH_DIR, stored_name), "wb") as f:
        f.write(encrypt_bytes(data))
    now = datetime.datetime.utcnow().isoformat()
    conn = get_connection()
    conn.execute(
        """INSERT INTO attachments
           (record_id, filename_enc, stored_name, content_type, size_bytes, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (record_id, encrypt(filename), stored_name, content_type or "application/octet-stream", len(data), now),
    )
    conn.commit()
    conn.close()


def get_attachments(record_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM attachments WHERE record_id=? ORDER BY id DESC", (record_id,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append(
            {
                "id": r["id"],
                "filename": decrypt(r["filename_enc"]),
                "content_type": r["content_type"],
                "size_kb": round(r["size_bytes"] / 1024, 1),
                "created_at": r["created_at"],
            }
        )
    return result


def get_attachment_file(attachment_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    conn.close()
    if not row:
        return None
    path = os.path.join(ATTACH_DIR, row["stored_name"])
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        encrypted_data = f.read()
    data = decrypt_bytes(encrypted_data)
    return {
        "filename": decrypt(row["filename_enc"]),
        "content_type": row["content_type"],
        "data": data,
    }


def delete_attachment(attachment_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if row:
        path = os.path.join(ATTACH_DIR, row["stored_name"])
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        conn.commit()
    conn.close()


def get_all_records():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM records ORDER BY id DESC").fetchall()
    counts = conn.execute(
        "SELECT record_id, COUNT(*) as c FROM attachments GROUP BY record_id"
    ).fetchall()
    conn.close()
    count_map = {c["record_id"]: c["c"] for c in counts}
    records = [_decrypt_row(r) for r in rows]
    for rec in records:
        rec["attachment_count"] = count_map.get(rec["id"], 0)
    return records


def _decrypt_row(row):
    return {
        "id": row["id"],
        "category": row["category"],
        "name": decrypt(row["name_enc"]),
        "email": decrypt(row["email_enc"]),
        "phone": decrypt(row["phone_enc"]),
        "notes": decrypt(row["notes_enc"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_backup():
    if not os.path.exists(DB_PATH):
        return None
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_name = f"personal_database_{timestamp}.zip"
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(DB_PATH, arcname="personal_database.db")
        for fname in os.listdir(ATTACH_DIR):
            fpath = os.path.join(ATTACH_DIR, fname)
            if os.path.isfile(fpath):
                zf.write(fpath, arcname=os.path.join("attachments", fname))
    _prune_backups()
    return backup_name


def _prune_backups(keep=10):
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".zip") or f.endswith(".db")],
        key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)),
        reverse=True,
    )
    for old in files[keep:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass


def list_backups():
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".zip") or f.endswith(".db")],
        key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)),
        reverse=True,
    )
    result = []
    for f in files:
        path = os.path.join(BACKUP_DIR, f)
        result.append(
            {
                "name": f,
                "size_kb": round(os.path.getsize(path) / 1024, 1),
                "created": datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )
    return result
