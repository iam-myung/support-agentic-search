"""CLI entry: knowledge (OP-05), investigate (OP-00), evaluate (OP-06), config (OP-13)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from support_platform.knowledge.importer import ImportError_, import_knowledge_file
from support_platform.knowledge.service import KnowledgeService

_SERVICE = KnowledgeService()
_REGISTRY: dict[str, dict] = {}
_CONFIG_SVC = None


def _config_service():
    global _CONFIG_SVC
    if _CONFIG_SVC is None:
        root = os.environ.get("CONFIG_ROOT") or "config_templates"
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if database_url:
            from support_platform.config_mgmt.pg_store import PgConfigVersionService

            _CONFIG_SVC = PgConfigVersionService(
                database_url=database_url,
                config_root=Path(root),
            )
        else:
            from support_platform.config_mgmt import ConfigVersionService

            _CONFIG_SVC = ConfigVersionService(config_root=Path(root))
    return _CONFIG_SVC


def _cmd_config_list(_: argparse.Namespace) -> int:
    svc = _config_service()
    rows = svc.list_versions()
    print(json.dumps({"ok": True, "versions": rows}, ensure_ascii=False, default=str))
    return 0


def _cmd_config_register(args: argparse.Namespace) -> int:
    svc = _config_service()
    try:
        if args.path:
            result = svc.register(
                kind=args.kind,
                path=Path(args.path),
                created_by=getattr(args, "created_by", None) or "cli@local",
            )
        else:
            payload = json.loads(args.payload) if args.payload else {}
            result = svc.register(
                kind=args.kind,
                payload=payload,
                created_by=getattr(args, "created_by", None) or "cli@local",
            )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
    return 0


def _cmd_config_activate(args: argparse.Namespace) -> int:
    svc = _config_service()
    try:
        result = svc.activate(args.version_id)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    try:
        if getattr(args, "persist", False):
            from support_platform.knowledge.pg_import import persist_import_file

            result = persist_import_file(
                path=Path(args.path),
                source_type=args.type,
                title=args.title,
            )
        else:
            result = import_knowledge_file(
                path=Path(args.path),
                source_type=args.type,
                title=args.title,
                service=_SERVICE,
            )
    except (ImportError_, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    _REGISTRY[result["source_id"]] = result
    print(json.dumps({"ok": True, **result}))
    return 0


def _cmd_list(_: argparse.Namespace) -> int:
    print(json.dumps({"ok": True, "sources": list(_REGISTRY.values())}))
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    from uuid import UUID

    try:
        source_id = UUID(args.source_id)
        _SERVICE.disable_source(source_id)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "source_id": args.source_id, "status": "DISABLED"}))
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    # Minimal update = re-import new file under same title/type metadata.
    return _cmd_import(args)


def _cmd_investigate(args: argparse.Namespace) -> int:
    """OP-00: invoke the same Investigation Service as unit/SMOKE paths."""
    from uuid import uuid4

    from support_platform.investigation import fail_stale_running, run_investigation

    if getattr(args, "real", False):
        # Approved real path: delegate to smoke module entry for provenance.
        from support_platform.investigation.smoke_s4 import main as smoke_main

        return int(smoke_main())

    if getattr(args, "persist_snapshot", False):
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            print(json.dumps({"ok": False, "error": "DATABASE_URL required for --persist-snapshot"}), file=sys.stderr)
            return 1
        config_root = os.environ.get("CONFIG_ROOT") or "config_templates"
        try:
            from support_platform.investigation.snapshot import (
                create_pg_investigation_with_snapshot,
                get_frozen,
            )

            created = create_pg_investigation_with_snapshot(
                database_url=database_url,
                question=args.question,
                config_root=config_root,
            )
            frozen = get_frozen(created["store"], created["id"])
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {"ok": True, "id": created["id"], "frozen": frozen},
                ensure_ascii=False,
                default=str,
            )
        )
        return 0

    from support_platform.infrastructure.chat import FakeChatAdapter

    class _Mem:
        def __init__(self) -> None:
            self.rows: dict[str, dict] = {}

        def create_running(self, *, question: str, frozen: dict | None = None) -> str:
            iid = str(uuid4())
            self.rows[iid] = {
                "id": iid,
                "question": question,
                "task_status": "RUNNING",
                "result_status": None,
                "public_steps": [],
                "error_code": None,
                "output": None,
                **(frozen or {}),
            }
            return iid

        def save(self, row: dict) -> None:
            self.rows[row["id"]] = dict(row)

        def get(self, investigation_id: str) -> dict:
            return dict(self.rows[investigation_id])

        def list_running(self) -> list[dict]:
            return [dict(r) for r in self.rows.values() if r["task_status"] == "RUNNING"]

    store = _Mem()
    fail_stale_running(store)

    class _Retrieve:
        def __call__(self, query: str, **_: object) -> list:
            return []

    chat = FakeChatAdapter()
    try:
        result = run_investigation(
            question=args.question,
            retrieve=_Retrieve(),
            chat=chat,
            store=store,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    """OP-06: run fixed dataset; --real uses approved smoke_s8 path (paid)."""
    if getattr(args, "real", False):
        from support_platform.evaluation.smoke_s8 import main as smoke_main

        return int(smoke_main())

    from support_platform.evaluation import run_evaluation

    dataset_path = Path(args.dataset)
    try:
        raw = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    if isinstance(raw, dict) and "cases" in raw:
        cases = raw["cases"]
    elif isinstance(raw, list):
        cases = raw
    else:
        print(
            json.dumps({"ok": False, "error": "dataset must be a list or {cases: [...]}"}),
            file=sys.stderr,
        )
        return 1

    def _investigate(
        *,
        question: str,
        context: dict | None = None,  # noqa: ARG001 — accepted for runner contract
        baseline: bool = False,
        case_id: str | None = None,  # noqa: ARG001
        **_: object,
    ) -> dict:
        from uuid import uuid4

        from support_platform.infrastructure.chat import FakeChatAdapter
        from support_platform.investigation import fail_stale_running, run_investigation

        class _Mem:
            def __init__(self) -> None:
                self.rows: dict[str, dict] = {}

            def create_running(self, *, question: str) -> str:
                iid = str(uuid4())
                self.rows[iid] = {
                    "id": iid,
                    "question": question,
                    "task_status": "RUNNING",
                    "result_status": None,
                    "public_steps": [],
                    "error_code": None,
                    "output": None,
                }
                return iid

            def save(self, row: dict) -> None:
                self.rows[row["id"]] = dict(row)

            def get(self, investigation_id: str) -> dict:
                return dict(self.rows[investigation_id])

            def list_running(self) -> list[dict]:
                return [
                    dict(r) for r in self.rows.values() if r["task_status"] == "RUNNING"
                ]

        store = _Mem()
        fail_stale_running(store)
        # baseline forces single-pass assess (no resolvable_gap → no re-query)
        chat = FakeChatAdapter(assess_actions=["sufficient"])
        _ = baseline  # explicit: callers must pass baseline into investigate contract

        def retrieve(_query: str, **__: object) -> list:
            return []

        return run_investigation(
            question=question,
            retrieve=retrieve,
            chat=chat,
            store=store,
        )

    try:
        report = run_evaluation(
            dataset=cases,
            investigate=_investigate,
            baseline=bool(args.baseline),
            knowledge_snapshot_hash=args.knowledge_snapshot_hash,
            prompt_version=args.prompt_version,
            model_config_version=args.model_config_version,
            retrieval_config_version=args.retrieval_config_version,
            output_dir=Path(args.output_dir),
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "report": report}, ensure_ascii=False, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="support_platform.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    knowledge = sub.add_parser("knowledge", help="OP-05 knowledge maintenance")
    ksub = knowledge.add_subparsers(dest="knowledge_command", required=True)

    imp = ksub.add_parser("import", help="Import a corpus file")
    imp.add_argument("--path", required=True)
    imp.add_argument("--type", default="DOC")
    imp.add_argument("--title", required=True)
    imp.add_argument(
        "--persist",
        action="store_true",
        help="Persist to PostgreSQL with real Embedding (S2 SMOKE path)",
    )
    imp.set_defaults(func=_cmd_import)

    upd = ksub.add_parser("update", help="Update from a new file")
    upd.add_argument("--path", required=True)
    upd.add_argument("--type", default="DOC")
    upd.add_argument("--title", required=True)
    upd.add_argument("--source-id", required=False)
    upd.set_defaults(func=_cmd_update)

    dis = ksub.add_parser("disable", help="Disable a source")
    dis.add_argument("--source-id", required=True)
    dis.set_defaults(func=_cmd_disable)

    lst = ksub.add_parser("list", help="List imported sources (session registry)")
    lst.set_defaults(func=_cmd_list)

    inv = sub.add_parser("investigate", help="OP-00 local investigation")
    inv.add_argument("--question", required=True)
    inv.add_argument(
        "--real",
        action="store_true",
        help="Approved real Chat+PG+Embedding path (same as smoke_s4)",
    )
    inv.add_argument(
        "--persist-snapshot",
        action="store_true",
        help="OP-00: freeze knowledge+config into real PostgreSQL (S10; no LLM)",
    )
    inv.set_defaults(func=_cmd_investigate)

    ev = sub.add_parser("evaluate", help="OP-06 fixed-dataset evaluation")
    ev.add_argument("--dataset", required=True, help="Path to annotated JSON dataset")
    ev.add_argument(
        "--baseline",
        action="store_true",
        help="Single-retrieval baseline (no re-query)",
    )
    ev.add_argument(
        "--real",
        action="store_true",
        help="Approved real Chat+Embedding+PG path (smoke_s8; paid)",
    )
    ev.add_argument(
        "--output-dir",
        default="artifacts/evaluation",
        help="Directory for JSON report output",
    )
    ev.add_argument("--knowledge-snapshot-hash", default="unset")
    ev.add_argument("--prompt-version", default="unset")
    ev.add_argument("--model-config-version", default="unset")
    ev.add_argument("--retrieval-config-version", default="unset")
    ev.set_defaults(func=_cmd_evaluate)

    cfg = sub.add_parser("config", help="OP-13 config register/activate/list")
    csub = cfg.add_subparsers(dest="config_command", required=True)

    c_reg = csub.add_parser("register", help="Register immutable config version")
    c_reg.add_argument("--kind", required=True, choices=["prompt", "model", "retrieval"])
    c_reg.add_argument("--path", required=False, help="Prompt template under CONFIG_ROOT")
    c_reg.add_argument("--payload", required=False, help="JSON payload for model/retrieval")
    c_reg.add_argument("--created-by", default="cli@local")
    c_reg.set_defaults(func=_cmd_config_register)

    c_act = csub.add_parser("activate", help="Atomically activate a registered version")
    c_act.add_argument("--version-id", required=True)
    c_act.set_defaults(func=_cmd_config_activate)

    c_list = csub.add_parser("list", help="List registered config versions")
    c_list.set_defaults(func=_cmd_config_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
