#!/usr/bin/env python3
"""
One-time migration: local sessions/*.session → MongoDB GridFS + update stock.gridfs_id

Usage:
  1. Ensure .env has MONGO_URI
  2. python migrate_sessions_to_gridfs.py
"""
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from database import stock_col
from utils.gridfs_sessions import store_session_file, session_exists

def main():
    sessions_dir = "sessions"
    if not os.path.isdir(sessions_dir):
        print("No sessions/ directory found — nothing to migrate.")
        return

    migrated = 0
    skipped = 0
    errors = 0

    for name in os.listdir(sessions_dir):
        if not name.endswith(".session"):
            continue
        phone = name[:-8]  # strip .session
        path = os.path.join(sessions_dir, name)
        doc = stock_col.find_one({"phone": phone})
        if not doc:
            print(f"  skip {phone}: not in stock collection")
            skipped += 1
            continue
        if doc.get("gridfs_id") and session_exists(doc["gridfs_id"]):
            print(f"  skip {phone}: already in GridFS")
            skipped += 1
            continue
        try:
            fid = store_session_file(phone, path)
            stock_col.update_one({"phone": phone}, {"$set": {"gridfs_id": fid}})
            print(f"  OK  {phone} → {fid}")
            migrated += 1
        except Exception as e:
            print(f"  ERR {phone}: {e}")
            errors += 1

    print(f"\nDone. migrated={migrated} skipped={skipped} errors={errors}")

if __name__ == "__main__":
    main()
