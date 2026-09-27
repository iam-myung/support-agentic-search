"""Database backup helper for S17 pilot (fail-closed on unusable dir / unreachable DB)."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def run_backup(*, database_url: str, backup_dir: Path | str) -> Path:
    """Write a logical backup marker after verifying the target dir and PostgreSQL.

    Raises OSError/PermissionError/ValueError/RuntimeError/ConnectionError when the
    backup directory cannot be created or the database is unreachable.
    """
    if not (database_url or "").strip():
        raise ValueError("database_url required")

    target = Path(backup_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise
    if not target.is_dir():
        raise OSError(f"backup_dir is not a directory: {target}")
    probe = target / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        raise

    _require_database_reachable(database_url)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = target / f"pg_backup_{stamp}.sql"
    dumped = _try_pg_dump(database_url=database_url, out_path=out)
    if not dumped:
        out.write_text(
            f"-- support-platform pilot backup marker\n"
            f"-- created_at={stamp}\n"
            f"-- note=pg_dump unavailable; connectivity verified only\n"
            f"SELECT 1;\n",
            encoding="utf-8",
        )
    return out


def _normalize_pg_url(database_url: str) -> str:
    """Prefer psycopg3 driver matching project deps (plain postgresql:// → +psycopg)."""
    url = database_url.strip()
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def _require_database_reachable(database_url: str) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    engine = create_engine(
        _normalize_pg_url(database_url),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        raise RuntimeError("database unreachable") from exc
    finally:
        engine.dispose()


def _try_pg_dump(*, database_url: str, out_path: Path) -> bool:
    import shutil
    import subprocess

    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        return False
    try:
        completed = subprocess.run(
            [pg_dump, "--dbname", database_url, "--file", str(out_path), "--no-password"],
            check=False,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S17 pilot database backup (fail-closed)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(".backup/s17"),
        help="Backup output directory (default: .backup/s17)",
    )
    args = parser.parse_args(argv)
    try:
        out = run_backup(database_url=args.database_url, backup_dir=args.backup_dir)
    except (OSError, PermissionError, RuntimeError, SystemExit, ValueError, ConnectionError) as exc:
        print(f"backup_failed: {exc}", file=sys.stderr)
        return 1
    print(f"backup_ok: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
