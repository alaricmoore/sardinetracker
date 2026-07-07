"""
create_user.py
--------------
CLI tool for creating user accounts in the biotracking database.
Run on the server (Pi) to add family members.

Usage:
    python create_user.py                  # interactive prompts
    python create_user.py --admin          # create an admin user
    python create_user.py --list           # list existing users
"""

import argparse
import getpass
import sys

import bcrypt
import db
import setup


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def create():
    parser = argparse.ArgumentParser(description="Create a sardinetracker user account")
    parser.add_argument("--admin", action="store_true", help="Grant admin privileges")
    parser.add_argument("--list", action="store_true", help="List existing users and exit")
    args = parser.parse_args()

    # Ensure the users table exists
    setup.create_database()

    if args.list:
        users = db.get_all_users()
        if not users:
            print("No users yet.")
        else:
            print(f"\n{'ID':<4} {'Username':<16} {'Display Name':<24} {'Admin':<6} {'Created'}")
            print("-" * 70)
            for u in users:
                admin_str = "yes" if u['is_admin'] else ""
                print(f"{u['id']:<4} {u['username']:<16} {u['display_name']:<24} {admin_str:<6} {u['created_at']}")
        return

    print("\n--- Create sardinetracker user account ---\n")

    username = input("Username (lowercase, no spaces): ").strip().lower()
    if not username or ' ' in username:
        print("Error: username must be non-empty and contain no spaces.")
        sys.exit(1)

    existing = db.get_user_by_username(username)
    if existing:
        print(f"Error: username '{username}' already exists.")
        sys.exit(1)

    display_name = input("Display name (e.g. 'Alaric'): ").strip()
    if not display_name:
        print("Error: display name is required.")
        sys.exit(1)

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Error: passwords don't match.")
        sys.exit(1)
    if len(password) < 4:
        print("Error: password must be at least 4 characters.")
        sys.exit(1)

    password_hash = hash_password(password)
    user_id = db.create_user(username, display_name, password_hash, is_admin=args.admin)

    role = "admin " if args.admin else ""
    print(f"\nCreated {role}user '{username}' (id={user_id}, display='{display_name}')")
    print("They can now log in at the sardinetracker app.")


if __name__ == "__main__":
    create()
