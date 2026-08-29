"""Username/password authentication with roles, segments, and permissions."""

from __future__ import annotations

import functools
import json
import os
import secrets

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import runtime_paths as rp

HERE = os.path.dirname(os.path.abspath(__file__))
AUTH_FILE = os.path.join(rp.data_root(), "auth.json")
SECRET_FILE = os.path.join(rp.data_root(), ".secret_key")

ROLE_ADMIN = "admin"
ROLE_ACCOUNTS = "accounts"
ROLE_MANAGEMENT = "management"
ROLE_VIEWER = "viewer"
ROLE_USER = "user"  # legacy = accounts
ROLE_SEGMENT_CALC = "segment_calc"
ROLE_SEGMENT_CONSULT = "segment_consult"
ROLE_SEGMENT_NEXTGEN = "segment_nextgen"

# Maps segment roles to default BusinessSegmentId (see BusinessSegments table)
SEGMENT_ROLE_IDS = {
    ROLE_SEGMENT_CALC: 1,
    ROLE_SEGMENT_CONSULT: 2,
    ROLE_SEGMENT_NEXTGEN: 3,
}

ROLE_CHOICES = [
    (ROLE_ADMIN, "Admin — full access (Sanjay)"),
    (ROLE_SEGMENT_CALC, "FSS Calculation User — segment only"),
    (ROLE_SEGMENT_CONSULT, "FSS Consultancy User — segment only"),
    (ROLE_SEGMENT_NEXTGEN, "Next Gen User — segment only"),
    (ROLE_ACCOUNTS, "Accounts — invoices, receipts, ledger (no profit/expenses)"),
    (ROLE_MANAGEMENT, "Management — dashboards & reports"),
    (ROLE_VIEWER, "Viewer — read only"),
]

_PERM_ALL = {"*"}
_PERM_ACCOUNTS = {
    "invoices", "receipts", "ledger", "clients", "reminders", "whatsapp",
    "tds", "gst", "export", "segments_view",
}
_PERM_MGMT = {
    "dashboard", "executive", "profitability", "reports", "ageing",
    "outstanding", "view", "segments_view",
}
_PERM_VIEW = {"view", "segments_view"}
_PERM_SEGMENT = {"invoices", "ledger", "clients", "view", "segments_view"}

ROLE_PERMS = {
    ROLE_ADMIN: _PERM_ALL,
    ROLE_ACCOUNTS: _PERM_ACCOUNTS,
    ROLE_USER: _PERM_ACCOUNTS,
    ROLE_MANAGEMENT: _PERM_MGMT | {"view"},
    ROLE_VIEWER: _PERM_VIEW,
    ROLE_SEGMENT_CALC: _PERM_SEGMENT,
    ROLE_SEGMENT_CONSULT: _PERM_SEGMENT,
    ROLE_SEGMENT_NEXTGEN: _PERM_SEGMENT,
}


def normalize_role(role: str) -> str:
    r = (role or ROLE_VIEWER).lower()
    if r == ROLE_USER:
        return ROLE_ACCOUNTS
    return r if r in ROLE_PERMS else ROLE_VIEWER


def user_segment_id(username: str | None) -> int | None:
    """None = all segments; int = restricted to one segment."""
    if not username:
        return None
    role = normalize_role(get_role(username))
    if role == ROLE_ADMIN or role in (ROLE_ACCOUNTS, ROLE_MANAGEMENT, ROLE_VIEWER):
        return None
    return SEGMENT_ROLE_IDS.get(role)


def can_view_segment(username: str | None, segment_id: int | None) -> bool:
    if not username or segment_id is None:
        return True
    allowed = user_segment_id(username)
    if allowed is None:
        return True
    return allowed == segment_id


def can(username: str | None, permission: str) -> bool:
    if not username:
        return False
    role = normalize_role(get_role(username))
    perms = ROLE_PERMS.get(role, _PERM_VIEW)
    if "*" in perms:
        return True
    if permission in perms:
        return True
    if permission.startswith("view") and "view" in perms:
        return True
    return False


def can_expenses(username: str | None) -> bool:
    return is_admin(username)


def can_profit(username: str | None) -> bool:
    if not username:
        return False
    role = normalize_role(get_role(username))
    if role == ROLE_ADMIN:
        return True
    if role == ROLE_MANAGEMENT:
        return True
    return False


def can_management_dashboard(username: str | None) -> bool:
    return is_admin(username)


def role_required(*roles):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            nr = normalize_role(get_role(user))
            if nr != ROLE_ADMIN and nr not in roles:
                return redirect(url_for("index"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def get_secret_key() -> str:
    env_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if env_key:
        return env_key.strip()
    if os.path.exists(SECRET_FILE):
        try:
            with open(SECRET_FILE, "r", encoding="utf-8") as fh:
                key = fh.read().strip()
            if key:
                return key
        except OSError:
            pass
    key = secrets.token_hex(32)
    try:
        with open(SECRET_FILE, "w", encoding="utf-8") as fh:
            fh.write(key)
    except OSError:
        pass
    return key


def _migrate(data: dict) -> dict:
    users = data.get("users", {})
    changed = False
    for name, val in list(users.items()):
        if isinstance(val, str):
            users[name] = {"hash": val, "role": ROLE_ADMIN}
            changed = True
        elif "role" not in val:
            val["role"] = ROLE_USER
            changed = True
    if changed:
        data["users"] = users
        _save(data)
    return data


def _load() -> dict:
    if os.environ.get("AUTH_JSON"):
        try:
            return _migrate(json.loads(os.environ["AUTH_JSON"]))
        except (json.JSONDecodeError, TypeError):
            pass
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as fh:
                return _migrate(json.load(fh))
        except (OSError, json.JSONDecodeError):
            pass
    # Vercel bootstrap — admin from environment variables
    admin_user = (os.environ.get("ADMIN_USERNAME") or "").strip()
    admin_pass = os.environ.get("ADMIN_PASSWORD") or ""
    if admin_user and admin_pass:
        return _migrate({
            "users": {
                admin_user: {
                    "hash": generate_password_hash(admin_pass),
                    "role": ROLE_ADMIN,
                }
            }
        })
    return {}


def _save(data: dict) -> None:
    with open(AUTH_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _users() -> dict:
    return _load().get("users", {})


def needs_setup() -> bool:
    return not _users()


def _valid_username(name: str) -> bool:
    return bool(name) and len(name) <= 40


def create_user(username: str, password: str, confirm: str,
                role: str = ROLE_ADMIN) -> tuple[bool, str]:
    username = (username or "").strip()
    if not _valid_username(username):
        return False, "Please choose a valid username."
    if username in _users():
        return False, f"User '{username}' already exists."
    if len(password or "") < 6:
        return False, "Password must be at least 6 characters."
    if password != confirm:
        return False, "Passwords do not match."
    allowed = {r[0] for r in ROLE_CHOICES} | {ROLE_USER}
    data = _load()
    data.setdefault("users", {})[username] = {
        "hash": generate_password_hash(password),
        "role": normalize_role(role) if role in allowed else ROLE_ACCOUNTS,
    }
    _save(data)
    return True, f"User '{username}' created."


def delete_user(target: str, acting_user: str) -> tuple[bool, str]:
    target = (target or "").strip()
    users = _users()
    if target not in users:
        return False, "User not found."
    if target == acting_user:
        return False, "You cannot delete your own account."
    admins = [u for u, v in users.items() if v.get("role") == ROLE_ADMIN]
    if users[target].get("role") == ROLE_ADMIN and len(admins) <= 1:
        return False, "Cannot delete the last admin."
    data = _load()
    data["users"].pop(target, None)
    _save(data)
    return True, f"User '{target}' removed."


def list_users() -> list[dict]:
    return [{"username": u, "role": v.get("role", ROLE_USER)}
            for u, v in sorted(_users().items())]


def verify(username: str, password: str) -> bool:
    rec = _users().get((username or "").strip())
    if not rec:
        return False
    return check_password_hash(rec.get("hash", ""), password or "")


def get_role(username: str) -> str:
    return normalize_role(_users().get(username, {}).get("role", ROLE_VIEWER))


def is_admin(username: str) -> bool:
    return get_role(username) == ROLE_ADMIN


def change_password(username: str, old_password: str,
                    new_password: str) -> tuple[bool, str]:
    if not verify(username, old_password):
        return False, "Current password is incorrect."
    if len(new_password or "") < 6:
        return False, "New password must be at least 6 characters."
    data = _load()
    data["users"][username]["hash"] = generate_password_hash(new_password)
    _save(data)
    return True, "Password updated successfully."


def current_user():
    return session.get("user")


def _wants_json() -> bool:
    path = request.path or ""
    if path.startswith("/api/"):
        return True
    accept = (request.headers.get("Accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept.split(",")[0]


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if needs_setup():
            if _wants_json():
                return jsonify(ok=False, message="First-time setup is required."), 401
            return redirect(url_for("setup"))
        if not current_user():
            if _wants_json():
                return jsonify(ok=False, message="Session expired. Please log in again."), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if needs_setup():
            return redirect(url_for("setup"))
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if not is_admin(user):
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped
