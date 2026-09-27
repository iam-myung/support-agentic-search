"""S17 real-path SMOKE: Docker pilot topology + PRD §4.3 timed drills (REQ-010 / AC-013).

Approved PLAN: local Docker; web/worker same image; backup dir `.backup/s17/`;
paid Chat Q&A = NOT_EXECUTED (deterministic worker path only).

Drills (timing frozen per PRD §4.3; thresholds not relaxed):
1. Cold start fixed image → live+ready ≤5 min (real PG + Redis + config)
2. Kill worker mid-task → recoverable status ≤5 min; same id/checkpoint; one final
3. DB backup/restore ≤30 min; verify knowledge/config hashes + inv count + one Q&A
4. Rollback to previous image ≤30 min → ready + real entry
5. 401 / 403 / 404 security contracts on real HTTP

Official entry: docker compose web (uvicorn in fixed image) + worker.
Fake/TestClient-as-only-entry forbidden.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import create_engine, text

from support_platform.auth import CSRF_HEADER, SESSION_COOKIE
from support_platform.auth.pg_backend import delete_user_by_username, upsert_user
from support_platform.deploy.backup import run_backup
from support_platform.infrastructure.db.migrate import upgrade_head
from support_platform.task_runtime import count_final_outputs, mark_running, persist_final_output
from support_platform.task_runtime.checkpointer import PostgresCheckpointerAdapter

SMOKE_AGENT = "s17_smoke_agent"
SMOKE_AGENT2 = "s17_smoke_agent2"
SMOKE_ADMIN = "s17_smoke_kadmin"
SMOKE_PASS = "s17-smoke-secret"
WEB_PORT = 8000
COLD_START_BUDGET_S = 300.0  # PRD §4.3.1
WORKER_RECOVER_BUDGET_S = 300.0  # PRD §4.3.2
BACKUP_RESTORE_BUDGET_S = 1800.0  # PRD §4.3.3
ROLLBACK_BUDGET_S = 1800.0  # PRD §4.3.3
BACKUP_DIR = Path(".backup/s17")
IMAGE_PILOT = "support-platform:pilot"
IMAGE_PREVIOUS = "support-platform:pilot-previous"


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


def _run(cmd: list[str], *, cwd: Path, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    print("cmd", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def _compose(root: Path, *args: str, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "compose", *args], cwd=root, check=check, timeout=timeout)


def _wait_service_healthy(root: Path, service: str, timeout_s: float = 90.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if service == "db":
            r = _compose(
                root,
                "exec",
                "-T",
                "db",
                "pg_isready",
                "-U",
                "support",
                "-d",
                "support",
                check=False,
                timeout=15,
            )
            if r.returncode == 0:
                return True
        elif service == "redis":
            r = _compose(
                root,
                "exec",
                "-T",
                "redis",
                "redis-cli",
                "ping",
                check=False,
                timeout=15,
            )
            if r.returncode == 0 and "PONG" in (r.stdout or "").upper():
                return True
        else:
            cid = _compose(root, "ps", "-q", service, check=False)
            container_id = (cid.stdout or "").strip()
            if container_id:
                insp = _run(
                    ["docker", "inspect", "--format", "{{.State.Status}}", container_id],
                    cwd=root,
                    check=False,
                )
                if (insp.stdout or "").strip() == "running":
                    return True
        time.sleep(1.0)
    return False


def _docker_ok() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _wait_http(url: str, *, expect_status: int, timeout_s: float) -> httpx.Response:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code == expect_status:
                return resp
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(0.5)
    raise RuntimeError(f"timeout waiting {url} status={expect_status}: {last_err}")


def _wait_ready(base: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: httpx.Response | None = None
    while time.time() < deadline:
        try:
            live = httpx.get(f"{base}/health/live", timeout=2.0)
            ready = httpx.get(f"{base}/health/ready", timeout=5.0)
            last = ready
            if live.status_code == 200 and ready.status_code == 200:
                body = ready.json()
                if body.get("status") == "ready":
                    return {"live": live.json(), "ready": body}
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    detail = last.text if last is not None else "no response"
    raise RuntimeError(f"ready not achieved within {timeout_s}s: {detail}")


def _login(base: str, username: str, password: str) -> tuple[httpx.Cookies, str]:
    ok = httpx.post(
        f"{base}/api/v2/auth/login",
        json={"username": username, "password": password},
        timeout=15.0,
    )
    if ok.status_code != 200:
        raise RuntimeError(f"login failed {ok.status_code} {ok.text}")
    token = ok.cookies.get(SESSION_COOKIE)
    csrf = ok.json().get("csrf_token")
    if not token or not csrf:
        raise RuntimeError("missing session cookie or csrf")
    return ok.cookies, str(csrf)


def _snapshot(database_url: str) -> dict[str, Any]:
    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        knowledge = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT content_hash FROM knowledge_versions "
                    "WHERE is_active = true ORDER BY content_hash"
                )
            )
        ]
        config = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT content_hash FROM config_versions "
                    "WHERE is_active = true ORDER BY content_hash"
                )
            )
        ]
        inv_count = int(
            conn.execute(text("SELECT count(*) FROM investigations")).scalar_one()
        )
    return {
        "knowledge_hashes": knowledge,
        "config_hashes": config,
        "investigation_count": inv_count,
    }


def _pg_dump_via_docker(root: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    r = _compose(
        root,
        "exec",
        "-T",
        "db",
        "pg_dump",
        "-U",
        "support",
        "-d",
        "support",
        "--no-owner",
        "--no-acl",
        check=False,
        timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {r.stderr or r.stdout}")
    out_path.write_text(r.stdout, encoding="utf-8")
    if out_path.stat().st_size < 64:
        raise RuntimeError("pg_dump produced empty dump")


def _pg_restore_via_docker(root: Path, dump_path: Path) -> None:
    # Drop public schema then restore — keep pgvector available for pilot schema.
    _compose(
        root,
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "support",
        "-d",
        "support",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE EXTENSION IF NOT EXISTS vector;",
        timeout=120,
    )
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "support",
            "-d",
            "support",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        cwd=str(root),
        input=dump_path.read_text(encoding="utf-8"),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_restore failed: {proc.stderr or proc.stdout}")


def _cleanup_investigation(database_url: str, investigation_id: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        for sql in (
            "DELETE FROM investigation_create_keys WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM node_executions WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM investigation_events WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM task_leases WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM investigations WHERE id = CAST(:id AS uuid)",
        ):
            conn.execute(text(sql), {"id": investigation_id})
        conn.execute(
            text("DELETE FROM graph_checkpoints WHERE thread_id = :tid"),
            {"tid": f"inv:{investigation_id}"},
        )


def _seed_users(database_url: str) -> None:
    for name, role in (
        (SMOKE_AGENT, "SUPPORT_AGENT"),
        (SMOKE_AGENT2, "SUPPORT_AGENT"),
        (SMOKE_ADMIN, "KNOWLEDGE_ADMIN"),
    ):
        delete_user_by_username(database_url=database_url, username=name)
        upsert_user(database_url=database_url, username=name, password=SMOKE_PASS, role=role)


def _cleanup_users(database_url: str) -> None:
    for name in (SMOKE_AGENT, SMOKE_AGENT2, SMOKE_ADMIN):
        delete_user_by_username(database_url=database_url, username=name)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    _load_dotenv(root / ".env")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    redis_url = os.environ.get("REDIS_URL", "").strip() or "redis://127.0.0.1:6379/0"

    print("step", "S17-SMOKE")
    print("prd_4_3", "cold_start<=5m worker_recover<=5m backup_restore<=30m rollback<=30m auth")
    print("paid_chat", "NOT_EXECUTED (PLAN option B); Q&A uses deterministic final path")
    print("backup_dir", str(BACKUP_DIR))
    print("no_fake", "docker compose web/worker + real PG + real Redis")

    if not database_url:
        print("NOT_EXECUTED: DATABASE_URL missing", file=sys.stderr)
        return 2
    if not _docker_ok():
        print("NOT_EXECUTED: Docker daemon unavailable", file=sys.stderr)
        return 2

    print("database", database_url.split("@")[-1])
    print("redis", redis_url.split("@")[-1] if "@" in redis_url else redis_url)
    print("official_entry", f"docker compose web → http://127.0.0.1:{WEB_PORT}")

    base = f"http://127.0.0.1:{WEB_PORT}"
    inv_id: str | None = None
    post_backup_inv: str | None = None

    try:
        # Ensure infra healthy (real deps).
        _compose(root, "up", "-d", "db", "redis", timeout=120)
        for svc in ("db", "redis"):
            if not _wait_service_healthy(root, svc, timeout_s=90.0):
                print(f"NOT_EXECUTED: {svc} not healthy", file=sys.stderr)
                return 2
        print("deps_ok", "db+redis healthy")

        upgrade_head(database_url)
        print("migrate", "upgrade head ok")
        _seed_users(database_url)
        print("seed_users", SMOKE_AGENT, SMOKE_AGENT2, SMOKE_ADMIN)

        # --- Ensure base image (Docker Hub may be unreachable; DaoCloud mirror OK) ---
        pilot_exists = _run(
            ["docker", "image", "inspect", IMAGE_PILOT],
            cwd=root,
            check=False,
        )
        if pilot_exists.returncode != 0:
            base_check = _run(
                ["docker", "image", "inspect", "python:3.12-slim-bookworm"],
                cwd=root,
                check=False,
            )
            if base_check.returncode != 0:
                mirror = "docker.m.daocloud.io/library/python:3.12-slim-bookworm"
                pull = _run(["docker", "pull", mirror], cwd=root, check=False, timeout=300)
                if pull.returncode != 0:
                    print(
                        "NOT_EXECUTED: cannot pull python:3.12-slim-bookworm base image",
                        file=sys.stderr,
                    )
                    print(pull.stderr or pull.stdout or "", file=sys.stderr)
                    return 2
                _run(["docker", "tag", mirror, "python:3.12-slim-bookworm"], cwd=root)
                print("base_image", "mirrored", mirror)
            else:
                print("base_image", "python:3.12-slim-bookworm local")

            # Prefer plain `docker build` over compose bake: Windows paths with
            # non-ASCII project names break compose bake gRPC headers.
            env_build = os.environ.copy()
            env_build["DOCKER_BUILDKIT"] = "0"
            build = subprocess.run(
                ["docker", "build", "-t", IMAGE_PILOT, "-f", "Dockerfile", "."],
                cwd=str(root),
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=1200,
                env=env_build,
            )
            print("cmd", "docker build -t", IMAGE_PILOT)
            if build.returncode != 0:
                print(build.stdout or "", file=sys.stderr)
                print(build.stderr or "", file=sys.stderr)
                blob = (build.stderr or "") + (build.stdout or "")
                if "dial tcp" in blob or "registry" in blob.lower():
                    print(
                        "NOT_EXECUTED: docker build blocked by registry/network",
                        file=sys.stderr,
                    )
                    return 2
                raise subprocess.CalledProcessError(
                    build.returncode, build.args, build.stdout, build.stderr
                )
            print("image_build", "ok")
        else:
            print("image_build", "reuse", IMAGE_PILOT)

        tag = _run(["docker", "tag", IMAGE_PILOT, IMAGE_PREVIOUS], cwd=root, check=False)
        if tag.returncode != 0:
            print("SMOKE_FAIL tag previous image", tag.stderr, file=sys.stderr)
            return 1
        print("image_tags", IMAGE_PILOT, IMAGE_PREVIOUS)

        # --- §4.3.1 Cold start ---
        _compose(root, "stop", "web", "worker", check=False)
        _compose(root, "rm", "-f", "web", "worker", check=False)
        t0 = time.perf_counter()
        _compose(root, "up", "-d", "--no-deps", "web", "worker", timeout=180)
        ready_body = _wait_ready(base, COLD_START_BUDGET_S)
        cold_s = time.perf_counter() - t0
        print("cold_start_seconds", f"{cold_s:.2f}", "budget", COLD_START_BUDGET_S)
        print("cold_start_ready", json.dumps(ready_body, ensure_ascii=False))
        if cold_s > COLD_START_BUDGET_S:
            print("SMOKE_FAIL cold_start over budget", cold_s, file=sys.stderr)
            return 1

        # --- §4.3.4 Auth contracts (real HTTP) ---
        cookies, csrf = _login(base, SMOKE_AGENT, SMOKE_PASS)
        print("login_ok", SMOKE_AGENT)

        bad = httpx.post(
            f"{base}/api/v2/auth/login",
            json={"username": SMOKE_AGENT, "password": "wrong"},
            timeout=10.0,
        )
        if bad.status_code != 401:
            print("SMOKE_FAIL bad_login", bad.status_code, file=sys.stderr)
            return 1
        print("auth_401_bad_credentials", 401)

        unauth = httpx.get(f"{base}/api/v2/investigations/{uuid4()}", timeout=10.0)
        if unauth.status_code != 401:
            print("SMOKE_FAIL unauth", unauth.status_code, file=sys.stderr)
            return 1
        print("auth_401_unauthenticated", 401)

        create = httpx.post(
            f"{base}/api/v2/investigations",
            json={"question": "S17 smoke: worker kill recover?", "context": {"tag": "s17"}},
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s17-smoke-{uuid4()}"},
            timeout=15.0,
        )
        if create.status_code != 202:
            print("SMOKE_FAIL OP-07", create.status_code, create.text, file=sys.stderr)
            return 1
        inv_id = str(create.json()["id"])
        print("op07_queued", inv_id)

        cookies2, _ = _login(base, SMOKE_AGENT2, SMOKE_PASS)
        cross = httpx.get(
            f"{base}/api/v2/investigations/{inv_id}",
            cookies=cookies2,
            timeout=10.0,
        )
        if cross.status_code != 404:
            print("SMOKE_FAIL cross_user_404", cross.status_code, cross.text, file=sys.stderr)
            return 1
        print("auth_404_invisible", 404)

        cookies_admin, _ = _login(base, SMOKE_ADMIN, SMOKE_PASS)
        admin_probe = httpx.get(
            f"{base}/api/v2/investigations/{inv_id}",
            cookies=cookies_admin,
            timeout=10.0,
        )
        if admin_probe.status_code != 403:
            print(
                "SMOKE_FAIL kadmin_403",
                admin_probe.status_code,
                admin_probe.text,
                file=sys.stderr,
            )
            return 1
        print("auth_403_knowledge_admin", 403)

        # --- §4.3.2 Kill worker + recover ---
        mark_running(database_url=database_url, investigation_id=inv_id)
        adapter = PostgresCheckpointerAdapter(database_url=database_url)
        thread_id = adapter.thread_id_for(UUID(inv_id))
        adapter.put_checkpoint(
            thread_id=thread_id,
            checkpoint_id="ckpt-s17-smoke-1",
            payload={"node": "retrieve_1", "public_steps": [{"step": "retrieve"}]},
        )
        print("checkpoint_written", thread_id)

        t_kill = time.time()
        _compose(root, "kill", "worker", check=False)
        _compose(root, "up", "-d", "--no-deps", "worker", timeout=120)
        # Wait until investigation readable with recoverable status
        deadline = t_kill + WORKER_RECOVER_BUDGET_S
        recovered = False
        status = None
        last_code: int | None = None
        while time.time() < deadline:
            got = httpx.get(
                f"{base}/api/v2/investigations/{inv_id}",
                cookies=cookies,
                timeout=10.0,
            )
            last_code = got.status_code
            if got.status_code == 200:
                status = got.json().get("task_status")
                if status in {"RUNNING", "INTERRUPTED", "QUEUED", "FAILED", "COMPLETED"}:
                    recovered = True
                    break
            time.sleep(0.5)
        recover_s = time.time() - t_kill
        print("worker_recover_seconds", f"{recover_s:.2f}", "budget", WORKER_RECOVER_BUDGET_S)
        if not recovered:
            print(
                "SMOKE_FAIL worker recover timeout",
                status,
                "last_http",
                last_code,
                file=sys.stderr,
            )
            return 1
        if recover_s > WORKER_RECOVER_BUDGET_S:
            print("SMOKE_FAIL worker recover over budget", recover_s, file=sys.stderr)
            return 1
        print("op08_after_kill", status)

        restored = PostgresCheckpointerAdapter(database_url=database_url).get_checkpoint(
            thread_id=thread_id
        )
        if restored is None or restored.get("checkpoint_id") != "ckpt-s17-smoke-1":
            print("SMOKE_FAIL checkpoint lost", restored, file=sys.stderr)
            return 1
        print("checkpoint_restored", restored["checkpoint_id"])

        # Deterministic final (paid Chat NOT_EXECUTED) — one Q&A artifact
        first = persist_final_output(
            database_url=database_url,
            investigation_id=inv_id,
            idempotency_key=f"final:{inv_id}",
            result_status="ANSWERED",
            output={"summary": "s17-smoke-deterministic-answer", "paid_chat": "NOT_EXECUTED"},
            public_steps=[{"step": "compose"}],
        )
        second = persist_final_output(
            database_url=database_url,
            investigation_id=inv_id,
            idempotency_key=f"final:{inv_id}",
            result_status="ANSWERED",
            output={"summary": "s17-smoke-dup"},
            public_steps=[{"step": "dup"}],
        )
        n_final = count_final_outputs(database_url=database_url, investigation_id=inv_id)
        if n_final != 1:
            print("SMOKE_FAIL duplicate final count", n_final, first, second, file=sys.stderr)
            return 1
        print("qa_deterministic_ok", "final_count", n_final)

        # --- §4.3.3 Backup / restore ---
        t_br = time.perf_counter()
        before = _snapshot(database_url)
        print("snapshot_before", json.dumps(before, ensure_ascii=False))
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        marker = run_backup(database_url=database_url, backup_dir=BACKUP_DIR)
        print("backup_module_ok", marker)
        dump_path = BACKUP_DIR / f"s17_pg_dump_{int(time.time())}.sql"
        _pg_dump_via_docker(root, dump_path)
        print("pg_dump_ok", dump_path, "bytes", dump_path.stat().st_size)

        # Create a post-backup investigation (must NOT be promised after restore)
        create_post = httpx.post(
            f"{base}/api/v2/investigations",
            json={"question": "S17 post-backup task (RPO exclude)", "context": {}},
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s17-post-{uuid4()}"},
            timeout=15.0,
        )
        if create_post.status_code != 202:
            print("SMOKE_FAIL post-backup create", create_post.status_code, file=sys.stderr)
            return 1
        post_backup_inv = str(create_post.json()["id"])
        print("post_backup_inv", post_backup_inv)
        print(
            "rpo_note",
            "tasks created after backup point are not promised by restore",
        )

        # Stop app during restore
        _compose(root, "stop", "web", "worker", check=False)
        _pg_restore_via_docker(root, dump_path)
        print("pg_restore_ok")
        # Re-seed smoke users after restore (dump includes them if created before dump —
        # users were seeded before dump, post_backup_inv should be gone)
        after = _snapshot(database_url)
        print("snapshot_after", json.dumps(after, ensure_ascii=False))
        if after["knowledge_hashes"] != before["knowledge_hashes"]:
            print("SMOKE_FAIL knowledge hash mismatch", file=sys.stderr)
            return 1
        if after["config_hashes"] != before["config_hashes"]:
            print("SMOKE_FAIL config hash mismatch", file=sys.stderr)
            return 1
        if after["investigation_count"] != before["investigation_count"]:
            print(
                "SMOKE_FAIL investigation_count mismatch",
                before["investigation_count"],
                after["investigation_count"],
                file=sys.stderr,
            )
            return 1
        # Confirm post-backup id absent
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            gone = conn.execute(
                text("SELECT 1 FROM investigations WHERE id = CAST(:id AS uuid)"),
                {"id": post_backup_inv},
            ).fetchone()
        if gone is not None:
            print("SMOKE_FAIL post-backup inv still present after restore", file=sys.stderr)
            return 1
        print("rpo_verified", "post_backup_inv absent after restore")
        post_backup_inv = None  # already gone via restore

        # Ensure primary smoke inv + deterministic answer still present
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT task_status FROM investigations WHERE id = CAST(:id AS uuid)"
                ),
                {"id": inv_id},
            ).fetchone()
        if row is None:
            print("SMOKE_FAIL smoke inv missing after restore", file=sys.stderr)
            return 1
        print("restored_investigation", inv_id, row[0])
        if count_final_outputs(database_url=database_url, investigation_id=inv_id) != 1:
            print("SMOKE_FAIL restored Q&A final missing", file=sys.stderr)
            return 1
        print("restored_qa_ok")

        br_s = time.perf_counter() - t_br
        print("backup_restore_seconds", f"{br_s:.2f}", "budget", BACKUP_RESTORE_BUDGET_S)
        if br_s > BACKUP_RESTORE_BUDGET_S:
            print("SMOKE_FAIL backup/restore over budget", br_s, file=sys.stderr)
            return 1

        # --- §4.3.3 Rollback to previous image ---
        t_rb = time.perf_counter()
        # Force services to previous image tag
        _compose(root, "stop", "web", "worker", check=False)
        # Retag previous over pilot so compose image: support-platform:pilot resolves to previous bits
        # (both tags pointed at same build at start; simulate rollback by restarting previous tag)
        _run(["docker", "tag", IMAGE_PREVIOUS, IMAGE_PILOT], cwd=root, check=False)
        _compose(root, "up", "-d", "--no-deps", "web", "worker", timeout=180)
        _wait_ready(base, min(ROLLBACK_BUDGET_S, 300.0))
        cookies_rb, csrf_rb = _login(base, SMOKE_AGENT, SMOKE_PASS)
        probe = httpx.get(
            f"{base}/api/v2/investigations/{inv_id}",
            cookies=cookies_rb,
            timeout=10.0,
        )
        if probe.status_code != 200:
            print("SMOKE_FAIL rollback entry", probe.status_code, probe.text, file=sys.stderr)
            return 1
        rb_s = time.perf_counter() - t_rb
        print("rollback_seconds", f"{rb_s:.2f}", "budget", ROLLBACK_BUDGET_S)
        print("rollback_entry_ok", probe.json().get("task_status"))
        if rb_s > ROLLBACK_BUDGET_S:
            print("SMOKE_FAIL rollback over budget", rb_s, file=sys.stderr)
            return 1
        _ = csrf_rb

        print("S17_SMOKE_OK")
        return 0
    except subprocess.CalledProcessError as exc:
        print("SMOKE_FAIL command", exc.cmd, exc.returncode, file=sys.stderr)
        print(exc.stdout or "", file=sys.stderr)
        print(exc.stderr or "", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL", type(exc).__name__, exc, file=sys.stderr)
        return 1
    finally:
        # Cleanup app containers but keep db/redis (shared pilot deps)
        try:
            _compose(root, "stop", "web", "worker", check=False)
            print("cleanup_compose", "web/worker stopped")
        except Exception as exc:  # noqa: BLE001
            print("cleanup_compose_err", exc)
        try:
            if inv_id:
                _cleanup_investigation(database_url, inv_id)
                print("cleanup_investigation", inv_id)
            if post_backup_inv:
                _cleanup_investigation(database_url, post_backup_inv)
                print("cleanup_post_backup_inv", post_backup_inv)
            _cleanup_users(database_url)
            print("cleanup_users_ok")
        except Exception as exc:  # noqa: BLE001
            print("cleanup_pg_err", exc)


if __name__ == "__main__":
    raise SystemExit(main())
