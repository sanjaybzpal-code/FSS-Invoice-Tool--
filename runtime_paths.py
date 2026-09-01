"""Runtime paths — local Windows vs Vercel serverless (/tmp)."""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))


def is_vercel() -> bool:
    """True only on Vercel Linux. Never treat a Windows office PC as Vercel."""
    if os.name == "nt":
        return False
    return bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))


IS_VERCEL = is_vercel()


def data_root() -> str:
    override = os.environ.get("FSS_DATA_DIR", "").strip()
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    if IS_VERCEL:
        root = os.environ.get("FSS_DATA_DIR", "/tmp/fss-invoice")
        os.makedirs(root, exist_ok=True)
        return root
    return HERE


def invoices_dir() -> str:
    path = os.path.join(data_root(), "Invoices")
    os.makedirs(path, exist_ok=True)
    return path
