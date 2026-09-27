"""Official worker process entry (SPEC §2 Phase 2 web/worker split)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


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


def run_once(*, database_url: str, worker_id: str, lease_seconds: int) -> dict | None:
    from support_platform.task_runtime import lease as lease_mod
    from support_platform.task_runtime import worker as worker_mod

    claimed = lease_mod.claim_next(
        database_url=database_url,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    if claimed is None:
        return None
    mode = worker_mod.default_worker_mode()
    return worker_mod.process_investigation(
        database_url=database_url,
        investigation_id=claimed["investigation_id"],
        worker_id=worker_id,
        mode=mode,
    )


def main(argv: list[str] | None = None) -> int:
    _ = argv
    root = Path(__file__).resolve().parents[2]
    _load_dotenv(root / ".env")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2
    worker_id = os.environ.get("WORKER_ID", "worker-local").strip() or "worker-local"
    lease_seconds = int(os.environ.get("WORKER_LEASE_SECONDS", "60") or "60")
    from support_platform.task_runtime.worker import default_worker_mode

    mode = default_worker_mode()
    once = "--once" in (argv or sys.argv[1:])
    if once:
        result = run_once(
            database_url=database_url,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        print("worker_once", "mode", mode, result)
        return 0
    print("worker_loop", worker_id, "lease_seconds", lease_seconds, "mode", mode)
    while True:
        run_once(
            database_url=database_url,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
