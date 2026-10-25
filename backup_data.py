"""Create or verify a restorable UMMAT SQLite + uploads backup archive."""
import argparse
import os
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

import app


def backup(destination_dir=None):
    destination = Path(destination_dir or os.environ.get("UMMAT_BACKUP_DIR", Path(app.BASE_DIR) / "backups"))
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    archive_path = destination / f"ummat-backup-{timestamp}.zip"
    with tempfile.TemporaryDirectory(dir=destination) as temp_dir:
        snapshot = Path(temp_dir) / "ummat.sqlite3"
        source = sqlite3.connect(app.DATABASE_FILE, timeout=30)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        check = sqlite3.connect(snapshot)
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {result}")
        finally:
            check.close()
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(snapshot, "ummat.sqlite3")
            upload_root = Path(app.UPLOAD_DIR)
            if upload_root.exists():
                for path in upload_root.rglob("*"):
                    if path.is_file():
                        archive.write(path, str(Path("static/uploads") / path.relative_to(upload_root)))
    verify(archive_path)
    retention_days = int(os.environ.get("UMMAT_BACKUP_RETENTION_DAYS", "30"))
    cutoff = time.time() - retention_days * 86400
    for old in destination.glob("ummat-backup-*.zip"):
        if old != archive_path and old.stat().st_mtime < cutoff:
            old.unlink()
    return archive_path


def verify(archive_path):
    archive_path = Path(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise RuntimeError(f"Corrupt archive member: {bad_member}")
        if "ummat.sqlite3" not in archive.namelist():
            raise RuntimeError("Archive does not contain ummat.sqlite3")
        with tempfile.TemporaryDirectory(dir=archive_path.parent) as temp_dir:
            archive.extract("ummat.sqlite3", temp_dir)
            conn = sqlite3.connect(Path(temp_dir) / "ummat.sqlite3")
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError(f"Restored database integrity check failed: {result}")
                counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                          for table in ("stores", "products", "enquiries")}
            finally:
                conn.close()
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", help="verify an existing backup without changing live data")
    parser.add_argument("--destination", help="backup folder; defaults to UMMAT_BACKUP_DIR or ./backups")
    args = parser.parse_args()
    if args.verify:
        print(f"Verified {args.verify}: {verify(args.verify)}")
    else:
        path = backup(args.destination)
        print(f"Created and verified {path}; {verify(path)}")


if __name__ == "__main__":
    main()
