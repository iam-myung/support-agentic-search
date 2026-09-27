"""S9 real-path SMOKE: official uvicorn → OP-12 login → real PostgreSQL; v1 bypass disabled."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text

from support_platform.auth import SESSION_COOKIE
from support_platform.auth.pg_backend import count_sessions_for_user, delete_user_by_username, upsert_user
from support_platform.infrastructure.db.migrate import upgrade_head

REQUIRED_AUTH_TABLES = {"users", "sessions"}
SMOKE_USER = "s9_smoke_agent"
SMOKE_PASS = "s9-smoke-secret"
PORT = 8791


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _wait_alive(base: str, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/health/live", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    raise RuntimeError("uvicorn health/live not ready")


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    _load_dotenv(root / ".env")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("NOT_EXECUTED: DATABASE_URL missing", file=sys.stderr)
        return 2

    prev_auth = os.environ.get("AUTH_BACKEND")
    prev_phase1 = os.environ.get("PHASE1_API_MODE")
    os.environ["AUTH_BACKEND"] = "postgres"
    os.environ["PHASE1_API_MODE"] = "disabled"
    os.environ.setdefault("LOG_LEVEL", "INFO")

    print("official_entry", f"uvicorn support_platform.main:app --host 127.0.0.1 --port {PORT} --ws none")
    print("database", database_url.split("@")[-1])
    print("auth_backend", "postgres")
    print("phase1_api_mode", "disabled")
    print("no_fake", "PostgreSQL users/sessions + real HTTP login")

    try:
        try:
            upgrade_head(database_url)
            print("migrate", "upgrade head ok")
        except Exception as exc:  # noqa: BLE001
            print("SMOKE_FAIL migrate", exc, file=sys.stderr)
            return 1

        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            with engine.connect() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    )
                }
                missing = REQUIRED_AUTH_TABLES - tables
                if missing:
                    print(f"SMOKE_FAIL missing tables: {sorted(missing)}", file=sys.stderr)
                    return 1
                owner_col = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'investigations' AND column_name = 'owner_user_id'"
                    )
                ).fetchone()
                if owner_col is None:
                    print("SMOKE_FAIL investigations.owner_user_id missing", file=sys.stderr)
                    return 1
            print("tables_ok", sorted(REQUIRED_AUTH_TABLES | {"investigations.owner_user_id"}))

            delete_user_by_username(database_url=database_url, username=SMOKE_USER)
            user = upsert_user(
                database_url=database_url,
                username=SMOKE_USER,
                password=SMOKE_PASS,
                role="SUPPORT_AGENT",
            )
            print("seed_user", SMOKE_USER, str(user.id))

            env = os.environ.copy()
            env["AUTH_BACKEND"] = "postgres"
            env["PHASE1_API_MODE"] = "disabled"
            env["DATABASE_URL"] = database_url
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "support_platform.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(PORT),
                    "--ws",
                    "none",
                ],
                cwd=str(root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            base = f"http://127.0.0.1:{PORT}"
            try:
                _wait_alive(base)
                print("uvicorn_ready", base)

                bad = httpx.post(
                    f"{base}/api/v2/auth/login",
                    json={"username": SMOKE_USER, "password": "wrong"},
                    timeout=10.0,
                )
                if bad.status_code != 401:
                    print("SMOKE_FAIL bad_login", bad.status_code, bad.text, file=sys.stderr)
                    return 1
                print("login_bad_credentials", 401)

                ok = httpx.post(
                    f"{base}/api/v2/auth/login",
                    json={"username": SMOKE_USER, "password": SMOKE_PASS},
                    timeout=10.0,
                )
                if ok.status_code != 200:
                    print("SMOKE_FAIL login", ok.status_code, ok.text, file=sys.stderr)
                    return 1
                set_cookie = ok.headers.get("set-cookie") or ""
                if "httponly" not in set_cookie.lower():
                    print("SMOKE_FAIL cookie not HttpOnly", set_cookie, file=sys.stderr)
                    return 1
                if "samesite" not in set_cookie.lower():
                    print("SMOKE_FAIL cookie missing SameSite", set_cookie, file=sys.stderr)
                    return 1
                token = ok.cookies.get(SESSION_COOKIE)
                csrf = ok.json().get("csrf_token")
                if not token or not csrf:
                    print("SMOKE_FAIL missing session or csrf", file=sys.stderr)
                    return 1
                sessions_n = count_sessions_for_user(database_url=database_url, user_id=user.id)
                if sessions_n < 1:
                    print("SMOKE_FAIL session not persisted in PostgreSQL", file=sys.stderr)
                    return 1
                print("login_ok", "HttpOnly+SameSite", "sessions_in_pg", sessions_n)

                unauth = httpx.get(f"{base}/api/v2/investigations/{uuid4()}", timeout=10.0)
                if unauth.status_code != 401:
                    print("SMOKE_FAIL unauth v2", unauth.status_code, file=sys.stderr)
                    return 1
                print("unauthenticated_v2", 401)

                v1 = httpx.post(
                    f"{base}/api/investigations",
                    json={"question": "s9 smoke must not bypass"},
                    timeout=10.0,
                )
                if v1.status_code not in (401, 403, 404):
                    print("SMOKE_FAIL v1 bypass still open", v1.status_code, v1.text, file=sys.stderr)
                    return 1
                print("phase1_disabled_blocks_v1", v1.status_code)

                out = httpx.post(
                    f"{base}/api/v2/auth/logout",
                    cookies={SESSION_COOKIE: token},
                    headers={"X-CSRF-Token": csrf},
                    timeout=10.0,
                )
                if out.status_code != 200:
                    print("SMOKE_FAIL logout", out.status_code, out.text, file=sys.stderr)
                    return 1
                after = count_sessions_for_user(database_url=database_url, user_id=user.id)
                if after != 0:
                    print("SMOKE_FAIL session remains after logout", after, file=sys.stderr)
                    return 1
                print("logout_ok", "sessions_cleared")

                print("S9_SMOKE_OK")
                return 0
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                print("cleanup_uvicorn", "terminated")
        finally:
            delete_user_by_username(database_url=database_url, username=SMOKE_USER)
            print("cleanup_pg_user", SMOKE_USER)
    finally:
        if prev_auth is None:
            os.environ.pop("AUTH_BACKEND", None)
        else:
            os.environ["AUTH_BACKEND"] = prev_auth
        if prev_phase1 is None:
            os.environ.pop("PHASE1_API_MODE", None)
        else:
            os.environ["PHASE1_API_MODE"] = prev_phase1


if __name__ == "__main__":
    raise SystemExit(main())
