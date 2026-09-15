from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from taxax.legal.models import ContentCompleteness
from taxax.legal.providers.law_go import PARSER_VERSION
from taxax.legal.repository import LegalRepository
from taxax.legal.snapshots import SnapshotStore, sha256_bytes, write_once
from taxax.legal.transport import HttpResponse


class SnapshotStoreTests(unittest.TestCase):
    def test_append_only_snapshot_and_parser_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SnapshotStore(root, parser_version=PARSER_VERSION)
            response = HttpResponse("https://www.law.go.kr/DRF/lawService.do", 200, {"content-type": "application/json"}, b'{"law":"complete"}')
            snapshot = store.save(
                provider="law.go.kr",
                document_type="law",
                source_document_id="12345",
                response=response,
                parsed={"law": "complete"},
                retrieved_at="2026-09-14T00:00:00Z",
                run_id="run-1",
                completeness=ContentCompleteness.COMPLETE,
            )
            raw_path = root / snapshot.snapshot_ref
            before = raw_path.read_bytes()
            second = store.save(
                provider="law.go.kr",
                document_type="law",
                source_document_id="12345",
                response=response,
                parsed={"law": "complete"},
                retrieved_at="2026-09-14T00:00:00Z",
                run_id="run-1",
                completeness=ContentCompleteness.COMPLETE,
            )
            extraction = store.reextract(snapshot.snapshot_ref, parsed={"law": "reparsed"}, parser_version="law-go-v2")
            self.assertEqual(snapshot.raw_sha256, second.raw_sha256)
            self.assertEqual(raw_path.read_bytes(), before)
            self.assertEqual(sha256_bytes(before), snapshot.raw_sha256)
            self.assertTrue((root / extraction).exists())
            self.assertTrue((root / "v1" / "extracted" / "law.go.kr" / "law" / "12345" / PARSER_VERSION).exists())

    def test_secret_reflection_is_redacted_before_write(self):
        # law.go.kr은 정상 응답에서도 다음 조회용 링크에 요청에 쓰인 OC를 그대로
        # 되돌려준다(예: 검색 결과의 법령상세링크). 그런 정상 응답까지 전부
        # 막아버리면 provider 전체가 못 쓰게 되므로, 저장 전 치환해서 보존한다.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SnapshotStore(root, parser_version=PARSER_VERSION)
            response = HttpResponse(
                "https://www.law.go.kr/DRF/lawService.do",
                200,
                {"content-type": "text/plain"},
                b"prefix-operator-secret-suffix",
            )
            snapshot = store.save(
                provider="law.go.kr",
                document_type="law",
                source_document_id="12345",
                response=response,
                parsed={},
                retrieved_at="2026-09-14T00:00:00Z",
                run_id="run-1",
                completeness=ContentCompleteness.EMPTY,
                secrets=("operator-secret",),
            )
            self.assertTrue(snapshot.redacted_secrets)
            raw_path = root / snapshot.snapshot_ref
            self.assertNotIn(b"operator-secret", raw_path.read_bytes())
            self.assertIn(b"[REDACTED]", raw_path.read_bytes())

    def test_concurrent_write_publishes_one_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.bin"
            payload = b"complete-payload" * 1000
            results: list[bool] = []
            failures: list[Exception] = []

            def writer():
                try:
                    results.append(write_once(path, payload))
                except Exception as exc:
                    failures.append(exc)

            threads = [threading.Thread(target=writer) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertFalse(failures)
            self.assertEqual(sum(results), 1)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory), parser_version=PARSER_VERSION)
            with self.assertRaises(ValueError):
                store.read_raw("../outside")


class RepositoryRunTests(unittest.TestCase):
    def test_run_is_idempotent_concurrent_safe_and_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = LegalRepository(Path(directory) / "legal.sqlite3")
            request = {"target": "law", "query": "법인세법", "page": 1}
            first = repository.start_run(provider="law.go.kr", action="search", request=request, started_at="2026-09-14T00:00:00Z", run_id="run-1", resume=True)
            concurrent = repository.start_run(provider="law.go.kr", action="search", request=request, started_at="2026-09-14T00:00:01Z", run_id="run-2", resume=True)
            self.assertTrue(first["claimed"])
            self.assertFalse(concurrent["claimed"])
            self.assertEqual(concurrent["run_id"], "run-1")
            repository.update_cursor("run-1", "2")
            repository.finish_run("run-1", status="failed", finished_at="2026-09-14T00:00:02Z", error_code="UPSTREAM_UNAVAILABLE")
            resumed = repository.start_run(provider="law.go.kr", action="search", request=request, started_at="2026-09-14T00:00:03Z", resume=True)
            self.assertTrue(resumed["claimed"])
            self.assertEqual(resumed["run_id"], "run-1")
            self.assertEqual(resumed["resume_cursor"], "2")
            repository.finish_run("run-1", status="completed", finished_at="2026-09-14T00:00:04Z")
            completed = repository.start_run(provider="law.go.kr", action="search", request=request, started_at="2026-09-14T00:00:05Z", resume=True)
            self.assertFalse(completed["claimed"])
            self.assertEqual(completed["status"], "completed")

    def test_stale_running_run_can_be_reclaimed_but_fresh_one_cannot(self):
        """running 상태로 멈춰 죽은 run은 재시도가 절대 못 벗어나던 문제의 재현/수정 확인.

        프로세스가 죽거나(크래시, 강제종료) 아직 못 잡은 예외로 finish_run을
        못 부르면 행이 영원히 'running'으로 남아, 같은 요청을 아무리 재시도해도
        매번 거부된다. 10분 넘게 지난 running 행은 죽은 것으로 보고 되찾을 수
        있어야 하고, 그보다 짧으면(진짜 동시 실행 중일 수 있으므로) 여전히
        거부돼야 한다.
        """
        with tempfile.TemporaryDirectory() as directory:
            repository = LegalRepository(Path(directory) / "legal.sqlite3")
            request = {"target": "law", "query": "부당행위계산부인"}

            # 599초 전 시작 -- 아직 죽었다고 볼 수 없으므로 재시도는 거부돼야 한다.
            repository.start_run(
                provider="law.go.kr", action="detail", request=request,
                started_at="2026-09-14T00:00:00Z", run_id="run-recent", resume=True,
            )
            still_running = repository.start_run(
                provider="law.go.kr", action="detail", request=request,
                started_at="2026-09-14T00:09:59Z", resume=True,
            )
            self.assertFalse(still_running["claimed"])

            # 601초 전 시작 -- 이제 죽은 것으로 보고 재시도가 되찾을 수 있어야 한다.
            reclaimed = repository.start_run(
                provider="law.go.kr", action="detail", request=request,
                started_at="2026-09-14T00:10:01Z", resume=True,
            )
            self.assertTrue(reclaimed["claimed"])
            self.assertEqual(reclaimed["run_id"], "run-recent")
            self.assertEqual(reclaimed["status"], "running")

            # 되찾은 뒤 정상 완료하면 이후 동일 요청은 캐시된 결과로 처리된다.
            repository.finish_run("run-recent", status="completed", finished_at="2026-09-14T00:10:02Z")
            cached = repository.start_run(
                provider="law.go.kr", action="detail", request=request,
                started_at="2026-09-14T00:10:03Z", resume=True,
            )
            self.assertFalse(cached["claimed"])
            self.assertEqual(cached["status"], "completed")

    def test_process_restart_marks_stale_running_runs_failed_but_leaves_recent_ones(self):
        """get_source_status가 죽은 작업을 계속 "실행 중"이라고 보여주던 문제.

        직전 프로세스가 크래시하거나 강제종료되면 'running' 행이 그대로
        남는다. 새 프로세스(=LegalRepository를 새로 만드는 것)가 뜰 때, 자신이
        시작하기 훨씬 전부터 'running'이던 행은 주인이 없는 게 확실하므로
        정리해야 한다. 다만 다른 프로세스가 방금 시작한 진짜 작업까지
        건드리면 안 된다.
        """
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legal.sqlite3"
            old = LegalRepository(db_path, clock=lambda: "2026-09-15T10:00:00Z")
            old.start_run(
                provider="law.go.kr", action="detail", request={"x": "stale"},
                started_at="2026-09-15T09:49:00Z", run_id="run-stale", resume=True,
            )
            old.start_run(
                provider="law.go.kr", action="detail", request={"x": "fresh"},
                started_at="2026-09-15T09:59:30Z", run_id="run-fresh", resume=True,
            )

            restarted = LegalRepository(db_path, clock=lambda: "2026-09-15T10:00:00Z")
            with restarted.connection() as connection:
                rows = {
                    row["run_id"]: (row["status"], row["error_code"])
                    for row in connection.execute("SELECT run_id, status, error_code FROM collection_runs")
                }
            self.assertEqual(rows["run-stale"], ("failed", "INTERRUPTED"))
            self.assertEqual(rows["run-fresh"], ("running", None))

    def test_manifest_payload_contains_no_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SnapshotStore(root, parser_version=PARSER_VERSION)
            reference = store.save_run_manifest("run-1", {"request": {"target": "law", "query": "민법"}})
            content = (root / reference).read_text(encoding="utf-8")
            self.assertNotIn("OC", content)
            self.assertEqual(json.loads(content)["request"]["query"], "민법")


if __name__ == "__main__":
    unittest.main()
