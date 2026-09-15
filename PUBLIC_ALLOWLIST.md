# Public release allowlist

외부 공개 전 `scripts/export_legal_public.py`가 새 빈 디렉터리에 아래 항목만 복사하고 SHA-256 보고서를 생성합니다.

## 포함

- `src/taxax/legal/**/*.py`
- `src/taxax/mcp/**/*.py`
- `tests/test_legal_*.py`
- `tests/legal_fixtures/**`
- `pyproject.toml`, `requirements.lock`, `MANIFEST.in`
- `README.md`, `CHANGELOG.md`, `LICENSE`, `THIRD_PARTY_NOTICES.md`
- `.env.example`, `.gitignore`
- `Dockerfile`, `compose.yaml`
- `docs/legal-mcp-deployment.md`
- `examples/claude-code-stdio.json`, `examples/hosted-mcp.json`
- `.github/workflows/ci.yml`
- `scripts/build_windows_installer.py`, `scripts/smoke_windows_executables.py`
- `scripts/export_legal_public.py`, `scripts/smoke_legal_install.py`, `scripts/validate_legal_distribution.py`

## 제외

- `knowledge/**`, `data/**`, `var/**`
- 고객 DB, `*.sqlite*`, `.env`, credential, 수집 원문/cache
- 회계 `engine.py`, `storage.py`, `orchestration.py`, `canonical.py`, `ingest.py`, `export.py`
- `src/taxax/adapters/**`, root `adapters/**`
- 회계 tests와 fixtures
- private roadmap/status/development log/강의 문서
- 실제 TAXax `src/taxax/__init__.py`
- 기존 대량 수집 script

wheel은 `taxax.legal`, `taxax.legal.providers`, `taxax.mcp`만 명시 등록합니다. `taxax` parent는 implicit namespace로 동작하므로 직하 회계 module이나 기존 `__init__.py`가 wheel에 포함되지 않습니다. CI는 wheel/sdist member를 exact allowlist와 대조합니다.
