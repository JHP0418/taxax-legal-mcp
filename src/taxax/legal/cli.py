from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .demo import run_synthetic_demo
from .codex_config import CodexConfigError, default_codex_config, unregister_codex
from .installer import (
    InstallationError,
    default_claude_code_config,
    install_pip_application,
    remove_local_credential,
    running_mcp_servers,
    stop_mcp_servers,
    unregister_claude_desktop,
)
from .maintenance import MaintenanceError, create_backup, restore_backup
from .service import LegalKnowledgeService, default_legal_data_dir

_NTS_URL = "https://taxlaw.nts.go.kr"
_OLTA_URL = "https://olta.re.kr"


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
    applicable.add_argument("--refresh", action="store_true")

    citations = subparsers.add_parser("verify-legal-citations")
    citations.add_argument("citations", type=_json_array)

    research = subparsers.add_parser("research-tax-issue")
    research.add_argument("issue")
    research.add_argument("--tax-type")
    research.add_argument("--transaction-date")
    research.add_argument("--tax-period")
    research.add_argument("--jurisdiction", default="KR")
    research.add_argument("--research-as-of")
    research.add_argument("--knowledge-cutoff")
    research.add_argument("--budget", type=_json_object)
    research.add_argument("--offline", action="store_true")

    report = subparsers.add_parser("get-research-report")
    report.add_argument("report_id")
    report.add_argument("--cursor")
    report.add_argument("--limit", type=int, default=10)

    subparsers.add_parser("doctor")
    subparsers.add_parser("get-source-status")
    subparsers.add_parser("demo")

    backup = subparsers.add_parser("backup")
    backup.add_argument("archive", type=Path)

    restore = subparsers.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("destination", type=Path)

    collect = subparsers.add_parser("collect-seeds")
    collect.add_argument("--run-id-prefix")

    install = subparsers.add_parser("install", help="데이터 폴더·법제처 키·Claude Desktop 등록을 설정합니다.")
    install.add_argument("--oc", help="법제처 OC 인증키. 생략하면 대화형으로 묻습니다.")
    install.add_argument("--no-oc", action="store_true", help="키 입력을 건너뜁니다. 외부 조회는 비활성 상태로 남습니다.")
    install.add_argument("--config", type=Path, help="Claude Desktop 설정 파일 경로를 직접 지정합니다.")
    install.add_argument("--skip-claude", action="store_true", help="Claude Desktop 등록을 건너뜁니다.")
    install.add_argument("--force", action="store_true", help="기존 taxax-legal 등록을 갱신합니다.")
    install.add_argument("--codex", action="store_true", help="Codex(~/.codex/config.toml)에도 등록합니다.")
    install.add_argument("--codex-config", type=Path, help="Codex 설정 파일 경로를 직접 지정합니다.")
    install.add_argument(
        "--nts",
        action="store_true",
        help="국세청 세법해석 사전(taxlaw.nts.go.kr) 조회를 켭니다. 이용약관 확인이 필요합니다.",
    )
    install.add_argument(
        "--nts-terms-confirmed",
        action="store_true",
        help=f"{_NTS_URL} 이용약관을 이미 확인했으므로 대화형 확인 없이 --nts를 켭니다.",
    )
    install.add_argument(
        "--olta",
        action="store_true",
        help="지방세 조세심판원 결정례(olta.re.kr) 조회를 켭니다. 이용약관 확인이 필요합니다.",
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
        help=f"{_OLTA_URL} 이용약관을 이미 확인했으므로 대화형 확인 없이 --olta를 켭니다.",
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


def _prompt_terms_confirmation(name: str, url: str) -> bool:
    """NTS/OLTA는 공식 API가 없어 사이트를 직접 읽어온다. 그래서 대신 동의하거나
    자동으로 켤 수 없고, 운영자가 그 사이트에 직접 들어가 이용약관을 확인했는지
    매번 물어봐야 한다. 비대화형 환경에서는 판단할 수 없으므로 켜지 않는다.
    """
    if not sys.stdin.isatty():
        return False
    sys.stdout.write(
        f"{name} 조회를 켜려면 사이트 이용약관을 직접 확인해야 합니다.\n"
        f"  {url} 에서 이용약관(자동 수집 관련 조항 포함)을 확인하십시오.\n"
        "확인하셨으면 y, 아니면 그냥 Enter를 누르십시오.\n"
        "동의하십니까? [y/N]: "
    )
    sys.stdout.flush()
    return sys.stdin.readline().strip().lower() in {"y", "yes"}


def run_install(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    credential = None if arguments.no_oc else (arguments.oc or _prompt_credential())
    nts_consent = bool(arguments.nts) and (
        arguments.nts_terms_confirmed or _prompt_terms_confirmation("국세청 세법해석 사전(NTS)", _NTS_URL)
    )
    olta_consent = bool(arguments.olta) and (
        arguments.olta_terms_confirmed or _prompt_terms_confirmation("지방세 조세심판원 결정례(OLTA)", _OLTA_URL)
    )
    try:
        result = install_pip_application(
            config_path=arguments.config,
            credential=credential,
            consent_to_configure_claude=not arguments.skip_claude,
            allow_config_update=arguments.force,
            register_claude=not arguments.skip_claude,
            register_codex_client=arguments.codex,
            codex_config_path=arguments.codex_config,
            register_claude_code_client=arguments.claude_code,
            claude_code_config_path=arguments.claude_code_config,
            nts_consent=nts_consent,
            olta_consent=olta_consent,
        )
    except InstallationError as exc:
        return {"status": "error", "error": {"code": "INSTALL_FAILED", "message": str(exc)}}, 2
    next_steps = []
    if not arguments.skip_claude:
        next_steps.append("Claude Desktop을 재시작하면 taxax-legal 도구가 나타납니다.")
    if result.codex_registered:
        next_steps.append("Codex를 재시작하면 taxax-legal 도구가 나타납니다.")
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
            "MCP client를 재시작하거나 `taxax-legal stop-servers`로 정리하십시오."
        )
    if not result.credential_configured:
        next_steps.append("법제처 외부 조회를 쓰려면 `taxax-legal install --oc <키>`로 인증키를 등록하십시오.")
    if arguments.nts and not nts_consent:
        next_steps.append(
            f"NTS 조회를 켜려면 {_NTS_URL} 이용약관을 확인한 뒤 `--nts --nts-terms-confirmed`로 다시 설치하십시오."
        )
    if arguments.olta and not olta_consent:
        next_steps.append(
            f"OLTA 조회를 켜려면 {_OLTA_URL} 이용약관을 확인한 뒤 `--olta --olta-terms-confirmed`로 다시 설치하십시오."
        )
    return {
        "status": "ok",
        "data_dir": str(result.data_dir),
        "mcp_executable_dir": str(result.install_dir),
        "claude_desktop_config": None if arguments.skip_claude else str(result.config_path),
        "config_backup": str(result.backup_path) if result.backup_path else None,
        "codex_config": str(result.codex_config_path) if result.codex_registered else None,
        "claude_code_config": str(result.claude_code_config_path) if result.claude_code_registered else None,
        "credential_configured": result.credential_configured,
        "nts_enabled": nts_consent,
        "olta_enabled": olta_consent,
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
        merged = unregister_claude_desktop(arguments.config, confirmed=True)
        codex = unregister_codex(arguments.codex_config, confirmed=True)
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
        "claude_desktop_config": str(merged.config_path),
        "registration_removed": merged.changed,
        "codex_config": str(codex.config_path),
        "codex_registration_removed": codex.changed,
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
            refresh=arguments.refresh,
        )
    if arguments.command == "verify-legal-citations":
        return service.verify_legal_citations(citations=arguments.citations)
    if arguments.command == "research-tax-issue":
        return service.research_tax_issue(
            issue=arguments.issue,
            tax_type=arguments.tax_type,
            transaction_date=arguments.transaction_date,
            tax_period=arguments.tax_period,
            jurisdiction=arguments.jurisdiction,
            research_as_of=arguments.research_as_of,
            knowledge_cutoff=arguments.knowledge_cutoff,
            budget=arguments.budget,
            upstream=not arguments.offline,
        )
    if arguments.command == "get-research-report":
        return service.get_research_report(
            report_id=arguments.report_id,
            cursor=arguments.cursor,
            limit=arguments.limit,
        )
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
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "stop-servers":
        result, code = run_stop_servers(arguments)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return code
    if arguments.command in {"install", "uninstall"}:
        result, code = run_install(arguments) if arguments.command == "install" else run_uninstall(arguments)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return code
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
