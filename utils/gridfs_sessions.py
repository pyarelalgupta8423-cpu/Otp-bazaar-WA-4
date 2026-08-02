"""
MongoDB GridFS helpers for storing Telegram .session files.
Sessions survive redeploys — no local sessions/ folder required in production.
"""
import os
import tempfile
import shutil
import logging
from bson import ObjectId
from gridfs import GridFS
from database import _db

logger = logging.getLogger(__name__)

# Custom bucket so session binaries are isolated from other GridFS usage
fs = GridFS(_db, collection="tg_sessions")


def store_session_file(phone: str, session_path: str) -> ObjectId:
    """
    Upload a local .session file (and optional -wal/-shm sidecars are ignored)
    into GridFS. Replaces any existing file for the same phone.
    Returns the GridFS file_id.
    """
    phone = str(phone).replace("+", "").replace(" ", "")
    path = session_path
    if not path.endswith(".session"):
        path = path + ".session"
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Session file not found: {path}")

    # Remove previous GridFS entry for this phone
    for old in fs.find({"filename": phone}):
        try:
            fs.delete(old._id)
        except Exception as e:
            logger.warning("Failed to delete old GridFS session %s: %s", phone, e)

    with open(path, "rb") as f:
        file_id = fs.put(
            f,
            filename=phone,
            content_type="application/octet-stream",
            metadata={"phone": phone},
        )
    return file_id


def store_session_bytes(phone: str, data: bytes) -> ObjectId:
    """Store raw session bytes directly (useful when already in memory)."""
    phone = str(phone).replace("+", "").replace(" ", "")
    for old in fs.find({"filename": phone}):
        try:
            fs.delete(old._id)
        except Exception:
            pass
    return fs.put(
        data,
        filename=phone,
        content_type="application/octet-stream",
        metadata={"phone": phone},
    )


def download_session(gridfs_id, phone: str = None) -> str:
    """
    Download session binary from GridFS into a temporary directory.
    Returns the *base path* (without .session) suitable for TelegramClient(base, ...).
    Caller must call cleanup_temp_session(base) when done.
    """
    if isinstance(gridfs_id, str):
        gridfs_id = ObjectId(gridfs_id)

    grid_file = fs.get(gridfs_id)
    phone = phone or (grid_file.filename or str(gridfs_id))
    phone = str(phone).replace("+", "").replace(" ", "")

    tmp_dir = tempfile.mkdtemp(prefix=f"sess_{phone}_")
    base = os.path.join(tmp_dir, phone)
    with open(base + ".session", "wb") as f:
        f.write(grid_file.read())
    return base


def get_session_bytes(gridfs_id) -> bytes:
    """Return raw session bytes (for ZIP packaging)."""
    if isinstance(gridfs_id, str):
        gridfs_id = ObjectId(gridfs_id)
    return fs.get(gridfs_id).read()


def delete_session_file(gridfs_id) -> None:
    """Delete a session binary from GridFS."""
    if gridfs_id is None:
        return
    try:
        if isinstance(gridfs_id, str):
            gridfs_id = ObjectId(gridfs_id)
        fs.delete(gridfs_id)
    except Exception as e:
        logger.warning("GridFS delete failed for %s: %s", gridfs_id, e)


def cleanup_temp_session(base_path: str) -> None:
    """Remove temporary directory created by download_session."""
    if not base_path:
        return
    tmp_dir = os.path.dirname(base_path)
    if tmp_dir and os.path.isdir(tmp_dir) and "sess_" in os.path.basename(tmp_dir):
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            logger.warning("Temp session cleanup failed: %s", e)


def session_exists(gridfs_id) -> bool:
    if gridfs_id is None:
        return False
    try:
        if isinstance(gridfs_id, str):
            gridfs_id = ObjectId(gridfs_id)
        return fs.exists(gridfs_id)
    except Exception:
        return False
