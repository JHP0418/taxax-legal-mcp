from __future__ import annotations

import json

from .models import (
    ContentCompleteness,
    LegalDocument,
    ResearchReport,
    ReviewState,
    TemporalStatus,
    TextSection,
)
from .service import LegalKnowledgeService, utc_now
from .transport import HttpResponse

DEMO_RUN_ID = "legal-synthetic-demo-v1"
DEMO_PROVIDER = "synthetic.fixture"
DEMO_DOCUMENT_IDS = (
    "fixture:law:corporate-tax-bad-debt-v1",
    "fixture:precedent:bad-debt-v1",
)


def install_synthetic_demo(service: LegalKnowledgeService) -> list[LegalDocument]:
    request = {"fixture": "synthetic-tax-research-v1", "network": False}
    started = utc_now()
    run = service.repository.start_run(
        provider=DEMO_PROVIDER,
        action="install-demo-fixture",
        request=request,
        started_at=started,
        run_id=DEMO_RUN_ID,
        resume=True,
    )
    if run["status"] == "completed":
        return service.repository.documents_for_run(run["run_id"])
    if not run["claimed"]:
        raise RuntimeError("같은 합성 demo fixture 설치가 이미 실행 중입니다.")

    payload = {
        "synthetic": True,
        "official_source": False,
        "notice": "제품 동작 시연용 합성 자료이며 실제 법령·판례가 아닙니다.",
        "documents": [
            {
                "document_id": DEMO_DOCUMENT_IDS[0],
                "title": "[합성 예시] 법인세 대손금 조문",
            },
            {
                "document_id": DEMO_DOCUMENT_IDS[1],
                "title": "[합성 예시] 대손금 손금산입 판결",
            },
        ],
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    snapshot = service.snapshots.save(
        provider=DEMO_PROVIDER,
        document_type="synthetic_demo_bundle",
        source_document_id="synthetic-tax-research-v1",
        response=HttpResponse(
            url="https://fixture.invalid/taxax-legal-demo.json",
            status=200,
            headers={"content-type": "application/json"},
            body=body,
        ),
        parsed=payload,
        retrieved_at=started,
        run_id=run["run_id"],
        completeness=ContentCompleteness.COMPLETE,
        parser_version="synthetic-demo-v1",
    )
    common = {
        "provider": DEMO_PROVIDER,
        "retrieved_at": started,
        "raw_sha256": snapshot.raw_sha256,
        "normalized_sha256": snapshot.normalized_sha256,
        "snapshot_ref": snapshot.snapshot_ref,
        "parser_version": snapshot.parser_version,
        "collection_run_id": run["run_id"],
        "content_completeness": ContentCompleteness.COMPLETE,
        "review_state": ReviewState.NEEDS_REVIEW,
        "metadata": {
            "synthetic_fixture": True,
            "official_source": False,
            "demo_notice": payload["notice"],
        },
    }
    documents = [
        LegalDocument(
            **common,
            document_type="law",
            source_document_id="synthetic-law-1",
            document_id=DEMO_DOCUMENT_IDS[0],
            version_id="synthetic-v1",
            title="[합성 예시] 법인세 대손금 조문",
            issuer="합성 fixture",
            tax_type="법인세",
            effective_from="2025-01-01",
            temporal_status=TemporalStatus.CANDIDATE,
            sections=[
                TextSection(
                    section_id="article-1",
                    kind="article",
                    locator="합성 제1조",
                    text="법인세 대손금 손금산입 합성 요건을 설명하는 제품 시연 문구입니다.",
                ),
                TextSection(
                    section_id="addenda-1",
                    kind="addenda",
                    locator="합성 부칙",
                    text="이 합성 예시는 2025년 1월 1일부터 적용되는 것으로 가정합니다.",
                ),
            ],
        ),
        LegalDocument(
            **common,
            document_type="precedent",
            source_document_id="synthetic-case-1",
            document_id=DEMO_DOCUMENT_IDS[1],
            version_id="synthetic-v1",
            title="[합성 예시] 대손금 손금산입 판결",
            issuer="합성 fixture",
            court="합성 법원",
            case_no="SYNTH-2026-1",
            tax_type="법인세",
            decided_on="2026-06-01",
            temporal_status=TemporalStatus.UNRESOLVED,
            sections=[
                TextSection(
                    section_id="facts-1",
                    kind="facts",
                    locator="합성 사실관계",
                    text="회수불능 매출채권과 대손금 손금산입을 가정한 합성 사실관계입니다.",
                ),
                TextSection(
                    section_id="reasoning-1",
                    kind="reasoning",
                    locator="합성 판단",
                    text="실제 세무 판단에 사용할 수 없는 합성 판결 이유입니다.",
                ),
            ],
        ),
    ]
    service.repository.save_result(snapshot, documents)
    service.repository.save_collection_result(
        run["run_id"],
        {
            "synthetic": True,
            "official_source": False,
            "document_ids": list(DEMO_DOCUMENT_IDS),
        },
    )
    service.repository.finish_run(run["run_id"], status="completed", finished_at=utc_now())
    service.snapshots.save_run_manifest(
        run["run_id"],
        {
            "run_id": run["run_id"],
            "status": "completed",
            "synthetic": True,
            "official_source": False,
            "document_ids": list(DEMO_DOCUMENT_IDS),
        },
    )
    return documents


def run_synthetic_demo(service: LegalKnowledgeService):
    documents = install_synthetic_demo(service)
    response = service.research_tax_issue(
        issue="법인세 대손금 손금산입",
        tax_type="법인세",
        transaction_date="2025-06-30",
        research_as_of="2026-09-14",
        knowledge_cutoff="2025-12-31",
        upstream=False,
    )
    notice = "제품 동작 시연용 합성 자료이며 실제 법령·판례·세무 결론이 아닙니다."
    report = ResearchReport.model_validate(response.data)
    report.assumptions.append("합성 fixture 문서만 사용한 offline 제품 시연입니다.")
    report.limitations.append(notice)
    service.report_repository.save(report, principal_id="local-stdio", org_id="local")
    response.data = report.model_dump(mode="json")
    response.data["demo"] = {
        "synthetic": True,
        "official_source": False,
        "document_ids": [document.document_id for document in documents],
        "notice": notice,
    }
    response.warnings.insert(0, notice)
    return response
