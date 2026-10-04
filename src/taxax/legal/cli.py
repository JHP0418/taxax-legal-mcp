from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .demo import run_synthetic_demo
from .codex_config import CodexConfigError, unregister_codex
from .codex_hook import main as run_codex_stop_hook, unregister_stop_hook
from .installer import (
    InstallationError,
    default_claude_code_config,
    install_pip_application,
    remove_local_credential,
    running_mcp_servers,
    stop_mcp_servers,
    unregister_claude_desktop,
)
from .console import prepare_console_streams
from .maintenance import MaintenanceError, create_backup, renormalize_documents, restore_backup
from .service import LegalKnowledgeService, default_legal_data_dir

_LAW_GO_OC_URL = "https://open.law.go.kr"


def _invocation() -> str:
    """사용자가 이 CLI를 부른 방식 그대로 후속 명령을 안내한다.

    console script가 PATH에 없어 `python -m taxax.legal`로 들어온 사용자에게
    `taxax-legal ...`을 안내하면, 그 명령은 그 사람 환경에서 실행되지 않는다.
    안내문은 받는 사람이 그대로 붙여넣어 쓸 수 있어야 한다.
    """
    if Path(sys.argv[0]).name in {"__main__.py", "-m"} or not sys.argv[0]:
        return f"{Path(sys.executable).name} -m taxax.legal"
    return "taxax-legal"


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("JSON object가 필요합니다.")
    return parsed


def _json_array(value: str) -> list[dict[str, Any]]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise argparse.ArgumentTypeError("JSON object 배열이 필요합니다.")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="taxax-legal", description="TAXax 공개 법률 지식 CLI")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    knowledge = subparsers.add_parser("search-knowledge")
    knowledge.add_argument("query")
    knowledge.add_argument("--k-ar-id")
    knowledge.add_argument("--limit", type=int, default=10)

    search = subparsers.add_parser("search-legal-sources")
    search.add_argument("query")
    search.add_argument("--target")
    search.add_argument("--provider")
    search.add_argument("--document-type")
    search.add_argument("--jurisdiction")
    search.add_argument("--filters", type=_json_object)
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--cursor")
    search.add_argument("--upstream", action="store_true")
    search.add_argument("--response-type", choices=("JSON", "XML"), default="JSON")
    search.add_argument("--run-id")

    document = subparsers.add_parser("get-legal-document")
    document.add_argument("--document-id")
    document.add_argument("--version-id", help="검색 결과의 version_id(MST); 다수 시행본이 있으면 필수입니다.")
    document.add_argument("--target")
    document.add_argument("--provider")
    document.add_argument("--source-document-id")
    document.add_argument("--source-category")
    document.add_argument("--attachment", action="store_true")
    document.add_argument("--identifier-kind", default="ID")
    document.add_argument("--effective-on")
    document.add_argument("--article")
    document.add_argument("--section-cursor", default="0")
    document.add_argument("--max-chars", type=int, default=16 * 1024)
    document.add_argument("--refresh", action="store_true")
    document.add_argument("--response-type", choices=("JSON", "XML"), default="JSON")
    document.add_argument("--run-id")

    applicable = subparsers.add_parser("get-applicable-law")
    applicable.add_argument("effective_on")
    applicable.add_argument("--mst")
    applicable.add_argument("--law-id")
    applicable.add_argument("--document-id")
    applicable.add_argument("--version-id")
    applicable.add_argument("--refresh", action="store_true")

    citations = subparsers.add_parser("verify-legal-citations")
    citations.add_argument("citations", type=_json_array)

    subparsers.add_parser("doctor")
    subparsers.add_parser("get-source-status")
    subparsers.add_parser("demo")

    backup = subparsers.add_parser("backup")
    backup.add_argument("archive", type=Path)

    restore = subparsers.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("destination", type=Path)

    renormalize = subparsers.add_parser(
        "renormalize",
        help="저장된 문서를 현재 파서 기준으로 다시 정규화합니다. 기본은 검사만 합니다.",
    )
    renormalize.add_argument("--apply", action="store_true", help="검사에 그치지 않고 실제로 고칩니다.")

    collect = subparsers.add_parser("collect-seeds")
    collect.add_argument("--run-id-prefix")

    install = subparsers.add_parser("install", help="데이터 폴더·법제처 키·Claude Desktop 등록을 설정합니다.")
    install.add_argument("--oc", help="법제처 OC 인증키. 생략하면 대화형으로 묻습니다.")
    install.add_argument("--no-oc", action="store_true", help="키 입력을 건너뜁니다. 외부 조회는 비활성 상태로 남습니다.")
    install.add_argument("--config", type=Path, help="Claude Desktop 설정 파일 경로를 직접 지정합니다.")
    install.add_argument("--skip-claude", action="store_true", help="Claude Desktop 등록을 건너뜁니다.")
    install.add_argument("--force", action="store_true", help="기존 taxax-legal 등록을 갱신합니다.")
    install.add_argument("--codex", action="store_true", help="Codex MCP와 법률 답변 1회 재검토 Stop 훅을 등록합니다.")
    install.add_argument("--codex-config", type=Path, help="Codex 설정 파일 경로를 직접 지정합니다.")
    install.add_argument(
        "--nts",
        action="store_true",
        help="deprecated 호환 옵션입니다. NTS 조회는 기본 활성입니다.",
    )
    install.add_argument(
        "--nts-terms-confirmed",
        action="store_true",
        help="deprecated no-op 호환 옵션입니다.",
    )
    install.add_argument(
        "--olta",
        action="store_true",
        help="deprecated 호환 옵션입니다. OLTA 조회는 기본 활성입니다.",
    )
    install.add_argument(
        "--claude-code",
        action="store_true",
        help="Claude Code(~/.claude.json)에도 등록합니다.",
    )
    install.add_argument("--claude-code-config", type=Path, help="Claude Code 설정 파일 경로를 직접 지정합니다.")
    install.add_argument(
        "--olta-terms-confirmed",
        action="store_true",
        help="deprecated no-op 호환 옵션입니다.",
    )

    uninstall = subparsers.add_parser("uninstall", help="Claude Desktop 등록과 저장된 키를 제거합니다.")
    uninstall.add_argument("--config", type=Path, help="Claude Desktop 설정 파일 경로를 직접 지정합니다.")
    uninstall.add_argument("--keep-credential", action="store_true", help="저장된 법제처 키를 남겨 둡니다.")
    uninstall.add_argument("--yes", action="store_true", help="확인 프롬프트 없이 진행합니다.")
    uninstall.add_argument("--codex-config", type=Path, help="Codex 설정 파일 경로를 직접 지정합니다.")
    uninstall.add_argument("--claude-code-config", type=Path, help="Claude Code 설정 파일 경로를 직접 지정합니다.")

    stop = subparsers.add_parser(
        "stop-servers",
        help="실행 중인 taxax-legal MCP 서버를 종료합니다. 업그레이드가 파일 잠금으로 막힐 때 사용합니다.",
    )
    stop.add_argument("--yes", action="store_true", help="확인 프롬프트 없이 종료합니다.")
    hook = subparsers.add_parser("stop-hook", help=argparse.SUPPRESS)
    hook.add_argument("--settings", type=Path, required=True)
    return parser


def _prompt_credential() -> str | None:
    if not sys.stdin.isatty():
        return None
    sys.stdout.write(
        "법제처 OC 인증키를 입력하십시오.\n"
        "  아직 없으면 https://open.law.go.kr 에서 무료로 발급받을 수 있습니다.\n"
        "  지금 건너뛰려면 그냥 Enter를 누르십시오(외부 법령 조회만 비활성).\n"
        "OC 키: "
    )
    sys.stdout.flush()
    value = sys.stdin.readline().strip()
    return value or None


def run_install(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    credential = None if arguments.no_oc else (arguments.oc or _prompt_credential())
    # Claude Desktop이 없는 OS(리눅스)에서 기본 등록 실패가 Codex 등록까지 막지 않게 한다.
    if arguments.config is None and sys.platform not in ("win32", "darwin"):
        arguments.skip_claude = True
    try:
        result = install_pip_application(
            config_path=arguments.config,
            data_dir=arguments.data_dir,
            credential=credential,
            consent_to_configure_claude=not arguments.skip_claude,
            allow_config_update=arguments.force,
            register_claude=not arguments.skip_claude,
            register_codex_client=arguments.codex,
            codex_config_path=arguments.codex_config,
            register_claude_code_client=arguments.claude_code,
            claude_code_config_path=arguments.claude_code_config,
        )
    except InstallationError as exc:
        return {"status": "error", "error": {"code": "INSTALL_FAILED", "message": str(exc)}}, 2
    next_steps = []
    invocation = _invocation()
    if not arguments.skip_claude:
        next_steps.append("Claude Desktop을 재시작하면 taxax-legal 도구가 나타납니다.")
    if result.codex_registered:
        next_steps.append("로컬 Codex/ChatGPT Work를 재시작해 새 로컬 대화의 설정 > MCP 서버에서 taxax-legal을 확인하고 get_source_status를 호출하십시오. 등록만으로 기존 대화나 클라우드 Work에 도구가 나타나지는 않습니다.")
        next_steps.append("Work의 쓰기 제한 때문에 SQLite를 열 수 없으면 데이터 폴더를 허용된 비공개 경로로 선택해 --data-dir <경로> install --codex --force로 다시 등록하십시오. 법률 DB나 OC 키를 공개 저장소에 넣지 마십시오.")
        next_steps.append(f"로컬 Codex `/hooks`에서 **TAXax legal answer recheck** 항목만 직접 검토·신뢰하십시오(Ponytail 등 다른 훅과 혼동 금지). 기본 1회 재검토 설정은 {result.codex_hook_path.with_name('taxax-legal-hook.json')}에서 끄거나 0회로 설정할 수 있습니다. 자동 실행 여부는 실제 Stop 이벤트로 별도 확인하십시오.")
        next_steps.append("Claude Desktop·Claude Cowork는 릴리스의 TAXax 확장(.mcpb)을 설정 → 확장 프로그램에 설치해 쓰십시오. 클라우드 ChatGPT Work·claude.ai 웹은 이 로컬 설치에 연결되지 않으며 인증된 외부 HTTPS /mcp 서버가 따로 필요합니다.")
    elif not arguments.codex:
        next_steps.append("Codex에도 등록하려면 `--codex` 옵션을 함께 사용하십시오.")
    if result.claude_code_registered:
        next_steps.append("Claude Code를 재시작하면 taxax-legal 도구가 나타납니다.")
    elif not arguments.claude_code:
        next_steps.append("Claude Code에도 등록하려면 `--claude-code` 옵션을 함께 사용하십시오.")
    running = running_mcp_servers()
    if running:
        next_steps.append(
            f"이전 버전 MCP 서버 {len(running)}개가 아직 실행 중입니다. "
            f"MCP client를 재시작하거나 `{invocation} stop-servers`로 정리하십시오."
        )
    if not result.credential_configured:
        # 인증키가 없으면 색인도 채울 수 없으므로 발급처부터 순서대로 알린다.
        next_steps.append(
            f"법제처 외부 조회를 쓰려면 {_LAW_GO_OC_URL} 에서 OC 인증키를 무료로 발급받은 뒤 "
            f"`{invocation} install --oc <키>`로 등록하십시오."
        )
    next_steps.append(
        f"설치 직후에는 로컬 법률 색인이 비어 있습니다. 인증키를 등록한 뒤 "
        f"`{invocation} collect-seeds`로 채워야 이후 조회가 실제 문서를 돌려줍니다."
    )
    return {
        "status": "ok",
        "data_dir": str(result.data_dir),
        "mcp_executable_dir": str(result.install_dir),
        "claude_desktop_config": None if arguments.skip_claude else str(result.config_path),
        "config_backup": str(result.backup_path) if result.backup_path else None,
        "codex_config": str(result.codex_config_path) if result.codex_registered else None,
        "codex_hook_config": str(result.codex_hook_path) if result.codex_registered else None,
        "claude_code_config": str(result.claude_code_config_path) if result.claude_code_registered else None,
        "credential_configured": result.credential_configured,
        "nts_enabled": True,
        "olta_enabled": True,
        "doctor_status": result.doctor_status,
        "next_steps": next_steps,
    }, 0


def run_uninstall(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    confirmed = arguments.yes or not sys.stdin.isatty()
    if not confirmed:
        sys.stdout.write("Claude Desktop 등록과 저장된 법제처 키를 제거합니다. 계속하려면 y를 입력하십시오: ")
        sys.stdout.flush()
        confirmed = sys.stdin.readline().strip().lower() in {"y", "yes"}
    if not confirmed:
        return {"status": "error", "error": {"code": "NOT_CONFIRMED", "message": "사용자가 취소했습니다."}}, 2
    try:
        merged = (
            unregister_claude_desktop(arguments.config, confirmed=True)
            if arguments.config is not None or os.name == "nt" else None
        )
        codex = unregister_codex(arguments.codex_config, confirmed=True)
        codex_hook = unregister_stop_hook(path=arguments.codex_config.with_name("hooks.json") if arguments.codex_config else None, confirmed=True)
        claude_code = unregister_claude_desktop(
            arguments.claude_code_config or default_claude_code_config(),
            confirmed=True,
            client_label="Claude Code",
        )
        credential_removed = False
        if not arguments.keep_credential:
            credential_removed = remove_local_credential(confirmed=True)
    except (InstallationError, CodexConfigError) as exc:
        return {"status": "error", "error": {"code": "UNINSTALL_FAILED", "message": str(exc)}}, 2
    return {
        "status": "ok",
        "claude_desktop_config": str(merged.config_path) if merged else None,
        "registration_removed": merged.changed if merged else False,
        "codex_config": str(codex.config_path),
        "codex_registration_removed": codex.changed,
        "codex_hook_removed": codex_hook.changed,
        "claude_code_config": str(claude_code.config_path),
        "claude_code_registration_removed": claude_code.changed,
        "credential_removed": credential_removed,
        "data_dir_preserved": str(default_legal_data_dir()),
    }, 0


def run_stop_servers(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    running = running_mcp_servers()
    if not running:
        return {"status": "ok", "stopped": [], "message": "실행 중인 taxax-legal MCP 서버가 없습니다."}, 0
    confirmed = arguments.yes
    if not confirmed and sys.stdin.isatty():
        sys.stdout.write(
            f"실행 중인 taxax-legal MCP 서버 {len(running)}개를 종료합니다.\n"
            "  MCP client(Claude Desktop/Code, Codex)를 재시작하면 다시 연결됩니다.\n"
            "계속하려면 y를 입력하십시오: "
        )
        sys.stdout.flush()
        confirmed = sys.stdin.readline().strip().lower() in {"y", "yes"}
    if not confirmed:
        return {
            "status": "error",
            "error": {"code": "NOT_CONFIRMED", "message": "사용자가 취소했습니다."},
            "running": running,
        }, 2
    try:
        stopped = stop_mcp_servers(confirmed=True)
    except InstallationError as exc:
        return {"status": "error", "error": {"code": "STOP_FAILED", "message": str(exc)}}, 2
    return {
        "status": "ok",
        "stopped": stopped,
        "next_steps": ["이제 `pip install --upgrade ...`로 업그레이드한 뒤 MCP client를 재시작하십시오."],
    }, 0


def execute(arguments: argparse.Namespace, service: LegalKnowledgeService):
    if arguments.command == "search-knowledge":
        return service.search_knowledge(query=arguments.query, k_ar_id=arguments.k_ar_id, limit=arguments.limit)
    if arguments.command == "search-legal-sources":
        return service.search_legal_sources(
            query=arguments.query,
            target=arguments.target,
            provider=arguments.provider,
            document_type=arguments.document_type,
            jurisdiction=arguments.jurisdiction,
            filters=arguments.filters,
            page=arguments.page,
            limit=arguments.limit,
            cursor=arguments.cursor,
            upstream=arguments.upstream,
            response_type=arguments.response_type,
            run_id=arguments.run_id,
        )
    if arguments.command == "get-legal-document":
        return service.get_legal_document(
            document_id=arguments.document_id,
            version_id=arguments.version_id,
            target=arguments.target,
            provider=arguments.provider,
            source_document_id=arguments.source_document_id,
            source_category=arguments.source_category,
            attachment=arguments.attachment,
            identifier_kind=arguments.identifier_kind,
            effective_on=arguments.effective_on,
            article=arguments.article,
            section_cursor=arguments.section_cursor,
            max_chars=arguments.max_chars,
            refresh=arguments.refresh,
            response_type=arguments.response_type,
            run_id=arguments.run_id,
        )
    if arguments.command == "get-applicable-law":
        return service.get_applicable_law(
            effective_on=arguments.effective_on,
            mst=arguments.mst,
            law_id=arguments.law_id,
            document_id=arguments.document_id,
            version_id=arguments.version_id,
            refresh=arguments.refresh,
        )
    if arguments.command == "verify-legal-citations":
        return service.verify_legal_citations(citations=arguments.citations)
    if arguments.command == "doctor":
        return service.doctor()
    if arguments.command == "get-source-status":
        return service.get_source_status()
    if arguments.command == "demo":
        return run_synthetic_demo(service)
    if arguments.command == "collect-seeds":
        return service.collect_seed_profile(run_id_prefix=arguments.run_id_prefix)
    raise AssertionError("unreachable")


def main(argv: Sequence[str] | None = None) -> int:
    # 결과 JSON을 ensure_ascii=False로 내보내므로, 코드페이지가 한글을
    # 담지 못하는 환경에서는 출력 한 번에 UnicodeEncodeError로 죽는다.
    prepare_console_streams()
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "stop-hook":
        return run_codex_stop_hook(["--settings", str(arguments.settings)])
    if arguments.command == "stop-servers":
        result, code = run_stop_servers(arguments)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return code
    if arguments.command in {"install", "uninstall"}:
        result, code = run_install(arguments) if arguments.command == "install" else run_uninstall(arguments)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return code
    if arguments.command == "renormalize":
        configured = os.environ.get("TAXAX_LEGAL_DATA_DIR")
        data_dir = arguments.data_dir or (Path(configured) if configured else default_legal_data_dir())
        try:
            result = renormalize_documents(data_dir / "v1" / "legal.sqlite3", apply=arguments.apply)
            if not arguments.apply and result["changed"]:
                result["next_step"] = f"`{_invocation()} renormalize --apply`로 실제로 고칩니다."
        except (MaintenanceError, OSError, ValueError) as exc:
            result = {"status": "error", "error": {"code": "MAINTENANCE_FAILED", "message": str(exc)}}
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0 if result["status"] == "ok" else 2
    if arguments.command in {"backup", "restore"}:
        try:
            if arguments.command == "backup":
                configured = os.environ.get("TAXAX_LEGAL_DATA_DIR")
                data_dir = arguments.data_dir or (Path(configured) if configured else default_legal_data_dir())
                result = create_backup(data_dir, arguments.archive)
            else:
                result = restore_backup(arguments.archive, arguments.destination)
        except (MaintenanceError, OSError) as exc:
            result = {
                "status": "error",
                "error": {
                    "code": "MAINTENANCE_FAILED",
                    "message": str(exc),
                },
            }
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0 if result["status"] == "ok" else 2
    service = LegalKnowledgeService(arguments.project_root, data_dir=arguments.data_dir)
    response = execute(arguments, service)
    sys.stdout.write(response.model_dump_json(indent=2) + "\n")
    return 0 if response.status.value in {"ok", "partial"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
