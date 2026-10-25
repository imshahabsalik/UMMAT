"""Manually link a verified owner to a legacy store that has no account."""
import getpass
import re
import sqlite3
from datetime import datetime

import app
from werkzeug.security import generate_password_hash


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def main():
    slug = input("Exact public store slug: ").strip()
    email = input("Verified supplier email: ").strip().lower()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise SystemExit("That email address is not valid.")
    password = getpass.getpass("Set a password (12–128 characters): ")
    if not 12 <= len(password) <= 128:
        raise SystemExit("Password must be between 12 and 128 characters.")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("Passwords did not match.")

    backup = f"{app.DATABASE_FILE}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak"
    source_db = sqlite3.connect(app.DATABASE_FILE)
    backup_db = sqlite3.connect(backup)
    try:
        source_db.backup(backup_db)
    finally:
        backup_db.close()
        source_db.close()

    with app.write_transaction():
        stores = app.load_stores()
        store = next((item for item in stores if item.get("slug") == slug), None)
        if not store:
            raise SystemExit("No store has that exact slug.")
        if store.get("owner_email") or store.get("password_hash"):
            raise SystemExit("That store already has account credentials; refusing to replace them.")
        if any(str(item.get("owner_email") or "").lower() == email for item in stores):
            raise SystemExit("An account already uses that email address.")

        store["owner_email"] = email
        store["password_hash"] = generate_password_hash(password)
        app.save_stores(stores)

    print(f"Linked {email} to {slug}. Backup: {backup}")


if __name__ == "__main__":
    main()
