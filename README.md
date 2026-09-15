# taxax-legal-mcp

`taxax-legal-mcp`는 대한민국 세무 쟁점을 공식 공개 법률 자료와 함께 조사하는 독립 Python CLI/MCP 서버입니다. 법령 원문 cache, 출처 provenance, 시행시점 후보, 인용 검증, 근거별 점수 설명을 제공하지만 법적·세무 결론이나 장부 반영을 자동 확정하지 않습니다.

이 공개 package에는 TAXax 회계 engine, 고객 DB, `knowledge/`, 수집 원문, 사설 K-AR 자료가 포함되지 않습니다. GitHub 저장소는 source와 self-host 예제를 제공할 뿐 무료 hosted 운영 서버를 제공하지 않습니다.

## 설치 (Windows 기준, 4단계)

터미널 입력은 2줄뿐입니다.

### 1단계 — Python 설치 (한 번만)

[python.org/downloads](https://www.python.org/downloads/)에서 **3.11 이상**을 받아 설치합니다.

> **설치 첫 화면의 `Add python.exe to PATH` 체크박스를 반드시 켜십시오.**
> 이걸 놓치면 2단계에서 `'pip'은(는) 내부 또는 외부 명령... 이 아닙니다` 오류가 납니다.
> 이미 그렇게 설치했다면 Python을 다시 실행해 `Modify` → `Add python to environment variables`를 켜면 됩니다.
>
> 2단계 설치 후 `WARNING: The scripts ... is installed in '...' which is not on PATH`가 보이면
> 3단계의 `taxax-legal`도 인식되지 않습니다. 이때는 아래 형태로 대신 실행하십시오.
>
> ```powershell
> python -m taxax.legal.cli install
> ```

### 2단계 — 프로그램 설치

PowerShell을 열고 아래 한 줄을 붙여넣습니다. (git이 없어도 됩니다)

```powershell
pip install https://github.com/JHP0418/taxax-legal-mcp/archive/refs/heads/main.zip
```

### 3단계 — 설정

```powershell
taxax-legal install
```

`'taxax-legal' 용어가 ... 인식되지 않습니다` 오류가 나면 2단계 설치 로그의 `not on PATH` 경고 때문입니다. 아래처럼 같은 명령을 실행하십시오. 동작은 동일합니다.

```powershell
python -m taxax.legal.cli install
```

이 한 줄이 다음을 모두 처리합니다.

- 법제처 OC 인증키를 물어봅니다. 아직 없으면 [open.law.go.kr](https://open.law.go.kr)에서 무료로 발급받을 수 있고, 그냥 Enter로 건너뛰어도 됩니다(외부 법령 조회만 비활성).
- 데이터 폴더(`%LOCALAPPDATA%\TAXax\legal`)를 만듭니다.
- `%APPDATA%\Claude\claude_desktop_config.json`을 **백업한 뒤** `taxax-legal` 항목만 병합합니다. 다른 MCP 서버 설정은 그대로 보존됩니다.
- `doctor` 진단을 실행해 결과를 보여줍니다.

키를 나중에 넣으려면 `taxax-legal install --oc <키>`를 다시 실행하면 됩니다. 같은 명령을 여러 번 실행해도 안전합니다.

> **법제처 API는 자료 종류별로 신청해야 합니다.** OC 키가 있어도 신청하지 않은 종류를 조회하면 법제처가 "미신청된 목록/본문에 대한 접근입니다"라고 응답합니다(조회 결과 대신 이 안내가 오류 메시지에 그대로 표시됩니다). [open.law.go.kr](https://open.law.go.kr) 로그인 → **OPEN API → OPEN API 신청 → 등록된 API 선택** 에서 필요한 법령종류를 체크하십시오. 법령·행정규칙·판례·조세심판원 재결례는 기본 신청으로 대부분 조회되지만, **국세청 법령해석(`ntsCgmExpc`)은 별도 체크가 필요한 경우가 많습니다.**

**Claude Code에도 함께 등록하려면** `--claude-code`를 붙입니다. `~/.claude.json`의 `mcpServers`에 `taxax-legal` 항목만 더하고 프로젝트 기록 등 나머지 설정은 그대로 둡니다.

```powershell
taxax-legal install --claude-code
```

**Codex(ChatGPT 데스크톱 앱 · Codex CLI · IDE 확장)에도 함께 등록하려면** `--codex`를 붙입니다.

```powershell
taxax-legal install --codex
```

`~/.codex/config.toml`(또는 `CODEX_HOME`)에 `[mcp_servers.taxax-legal]` 항목만 덧붙이며, 기존 주석·설정·다른 MCP 서버는 그대로 둡니다. 변경 전 원본은 같은 폴더에 백업합니다.

> ChatGPT **웹/모바일**은 로컬 stdio 서버에 연결하지 못하고 HTTPS 원격 서버만 지원합니다. 이 경로는 아래 "Hosted HTTP" 절을 참고하십시오.

> **업그레이드가 `WinError 32`로 실패한다면** MCP client가 서버를 띄워 둔 상태라 실행파일이 잠긴 것입니다.
> `Access is denied` 또는 `다른 프로세스가 파일을 사용 중`이라는 메시지가 나오면 아래로 정리한 뒤 다시 설치하십시오.
>
> ```powershell
> taxax-legal stop-servers
> ```
>
> 서버는 상태를 DB에 두므로 종료해도 잃는 자료가 없고, MCP client를 재시작하면 다시 연결됩니다.

### 4단계 — Claude Desktop 재시작

완전히 종료한 뒤 다시 실행하면 도구 8개가 나타납니다. Claude에게 이렇게 물어보십시오.

```
법인세법 제19조의2 대손금 조문 찾아줘
```

### 업데이트

같은 명령을 다시 실행하면 최신 버전을 받습니다.

```powershell
pip install --upgrade https://github.com/JHP0418/taxax-legal-mcp/archive/refs/heads/main.zip
taxax-legal install --force
```

버전 번호가 같은데도 코드가 바뀐 경우에는 pip이 설치를 건너뜁니다. 그때는 `--force-reinstall --no-deps`를 함께 지정하십시오.

### 제거

```powershell
taxax-legal uninstall
```

Claude Desktop·Claude Code·Codex 등록과 저장된 인증키를 지웁니다. 받아둔 법령 데이터 폴더는 남겨 둡니다.

## 요구사항

- Python 3.11 이상
- 법제처 upstream 사용 시 운영자용 `TAXAX_LAW_GO_OC` 또는 `taxax-legal install`로 저장한 인증키
- NTS/OLTA는 운영자가 이용조건을 확인하고 각각 enabled + terms-confirmed를 모두 설정한 경우에만 사용
- K-AR와 `korean-law-mcp` bridge는 선택 기능이며 없어도 CLI, stdio MCP, 공개 법률 조회, research workflow가 기동

## Windows 로컬 설치 마법사

첫 공개 경로는 Windows 로컬 설치기입니다. 빌드된 bundle의 `SHA256SUMS.json`과 파일 hash를 먼저 대조한 뒤 `taxax-legal-setup-0.2.0-windows-<architecture>.exe`를 실행합니다. 최종 사용자는 별도 Python 설치가 필요하지 않습니다.

설치 마법사는 다음 순서로 동작합니다.

1. 법제처 Open API OC를 입력하거나 건너뜁니다. 키가 없어도 local MCP와 offline demo는 기동되고 공식 upstream 조회만 비활성화됩니다.
2. 실행파일을 `%LOCALAPPDATA%\TAXax\app\0.2.0`, 법률 data를 `%LOCALAPPDATA%\TAXax\legal`에 둡니다.
3. 사용자 동의를 받은 경우에만 `%APPDATA%\Claude\claude_desktop_config.json`을 백업하고 기존 root field와 다른 MCP server를 보존한 채 `taxax-legal` 항목을 원자적으로 병합합니다. 같은 이름의 다른 항목은 별도 교체 동의 없이는 거부합니다.
4. `doctor`를 실행하고 Claude Desktop을 완전히 종료한 뒤 다시 시작하도록 안내합니다.

OC는 Claude Desktop JSON이나 process argument에 넣지 않습니다. `%LOCALAPPDATA%\TAXax\config\provider-secrets.json`에 현재 Windows 사용자 SID만 `FullControl`인 상속 차단 ACL을 적용하고 실제 ACL 검증이 성공한 경우에만 평문으로 저장합니다. 이는 다른 일반 사용자 접근을 제한하지만 관리자 접근까지 막는 암호화 저장소는 아닙니다. 입력·교체·삭제는 명시적 사용자 동작으로만 수행합니다.

일반 legal backup에는 이 secret 파일이 포함되지 않습니다. 다른 PC나 복원 경로에서는 OC를 다시 설정해야 합니다. 현재 installer manifest는 `code_signed: false`, `smartscreen_reputation: not_established`로 표시하며, code signing과 SmartScreen 평판이 확인되기 전에는 서명된 배포물로 표현하지 않습니다.

source에서 Windows bundle을 검증하려면 새 output directory 이름을 지정합니다.

```powershell
py -3.11 -m pip install -e ".[dev,windows]"
py -3.11 scripts\build_windows_installer.py "$env:TEMP\taxax-legal-windows"
py -3.11 scripts\smoke_windows_executables.py "$env:TEMP\taxax-legal-windows"
```

## 개발자·TAXax 연동용 pip 빠른 시작

아직 게시 전 source checkout에서는 다음과 같이 wheel을 빌드해 설치합니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m build
.\.venv\Scripts\python -m pip install .\dist\taxax_legal_mcp-0.2.0-py3-none-any.whl
```

설정과 저장소를 진단합니다. `doctor`는 실 API를 호출하지 않습니다. `TAXAX_LEGAL_DATA_DIR`를 생략하면 실행 위치와 무관하게 Windows `%LOCALAPPDATA%\TAXax\legal`, macOS `~/Library/Application Support/TAXax/legal`, Linux `${XDG_DATA_HOME:-~/.local/share}/taxax/legal`을 사용합니다.

```powershell
taxax-legal doctor
```

credential 없이 합성 공개 fixture로 대표 세무 쟁점 조사와 원문 상세 조회를 시연합니다. 실제 운영 cache와 섞이지 않도록 별도의 scratch data directory를 사용하십시오.

```powershell
$env:TAXAX_LEGAL_DATA_DIR = "$HOME\taxax-legal-demo"
taxax-legal demo > demo-report.json
taxax-legal get-legal-document --document-id fixture:law:corporate-tax-bad-debt-v1
```

`demo` 결과의 문서 제목·metadata·경고는 모두 합성 자료임을 표시합니다. 실제 법령·판례·세무 결론으로 사용하면 안 됩니다.

stdio MCP를 기동합니다.

```powershell
taxax-legal-mcp --transport stdio
```

클라이언트 설정 예시는 [`examples/claude-code-stdio.json`](examples/claude-code-stdio.json)입니다. 실제 클라이언트 설정 변경은 사용자가 직접 수행합니다.

## 공개 MCP tools

공개 surface는 다음 8개로 고정합니다.

1. `search_knowledge`
2. `search_legal_sources`
3. `get_legal_document`
4. `get_applicable_law`
5. `verify_legal_citations`
6. `research_tax_issue`
7. `get_research_report`
8. `get_source_status`

`search_knowledge`는 `TAXAX_PRIVATE_KNOWLEDGE_DIR`를 명시한 경우에만 사설 읽기 전용 K-AR 디렉터리를 검색합니다. 현재 작업 디렉터리나 TAXax checkout의 `knowledge/`를 자동 탐색하지 않습니다.

## 조사 예시

실제 upstream은 운영자 credential과 각 provider 이용조건을 준비한 환경에서만 활성화합니다.

```powershell
$env:TAXAX_LAW_GO_OC = "<operator-managed-value>"
taxax-legal research-tax-issue "매출채권 대손금 손금산입 요건" `
  --tax-type 법인세 `
  --transaction-date 2025-06-30 `
  --research-as-of 2026-09-14
```

credential이 없거나 provider가 비활성이면 확인된 local 결과를 보존하고 `partial`과 미조사 범위를 반환합니다. 기본 remote budget은 실제 HTTP retry를 포함한 20회, 상세 8건, 60초이며 호출자가 상향할 수 없습니다.

## 데이터 분리

기본 `TAXAX_LEGAL_DATA_DIR` 아래에서 다음을 분리합니다.

```text
v1/legal.sqlite3              공개 법률 metadata/index
v1/raw/**                     append-only 원문 snapshot
v1/extracted/**               parser-version별 정규화 결과
v1/runs/**                    수집 run manifest
private/v1/reports.sqlite3    principal/org scope 조사 보고서
```

보고서 DB에는 raw principal/org 값 대신 SHA-256 scope key를 저장합니다. report ID와 scope가 모두 맞아야 조회되며 다른 scope의 ID는 `NOT_FOUND`입니다.

## Hosted HTTP

일반 직원은 법제처·NTS·OLTA credential을 발급받거나 입력하지 않습니다. 중앙 운영 서버가 provider credential을 보관하고 조직 IdP가 발급한 JWT만 직원 client에 사용합니다.

Hosted mode는 issuer, audience, resource, JWKS, Host allowlist, Origin allowlist가 모두 있어야 시작합니다. `iss`, `aud`, `exp`, `sub`, `client_id`/`azp`, scope, resource, organization claim을 검증하며 HS 계열 algorithm은 허용하지 않습니다.

```bash
export TAXAX_MCP_AUTH_ISSUER=https://issuer.example.invalid
export TAXAX_MCP_AUTH_AUDIENCE=taxax-legal
export TAXAX_MCP_AUTH_RESOURCE=https://legal.example.invalid
export TAXAX_MCP_AUTH_JWKS_URL=https://issuer.example.invalid/.well-known/jwks.json
export TAXAX_MCP_ALLOWED_HOSTS=legal.example.invalid
export TAXAX_MCP_ALLOWED_ORIGINS=https://client.example.invalid

taxax-legal-mcp --transport streamable-http --host 127.0.0.1 --port 8765
```

TLS reverse proxy는 원래 Host를 보존하고 `/mcp`로 전달해야 합니다. 인증 없는 HTTP는 `TAXAX_MCP_ALLOW_LOCAL_HTTP=1`을 명시한 loopback bind에서만 허용됩니다. 자세한 내용은 [`docs/legal-mcp-deployment.md`](docs/legal-mcp-deployment.md)를 확인하십시오.

## Backup과 복원

두 SQLite DB는 WAL mode이므로 `.sqlite3` 파일만 복사하면 안 됩니다. CLI는 SQLite online backup과 manifest SHA-256을 사용합니다.

```powershell
taxax-legal --data-dir "$HOME\taxax-legal-data" backup .\backup-20260914.zip
taxax-legal restore .\backup-20260914.zip "$HOME\taxax-legal-restored"
```

복원은 기존 경로를 덮어쓰지 않습니다. 새 경로에서 `doctor`, source count, 원문 hash, 동일/타 scope report 조회를 검증한 후 환경변수를 전환하십시오.

## 개발과 검증

```bash
python -m pip install -e .[dev]
python -m unittest discover -s tests -p "test_legal_*.py" -v
python -m build
python scripts/validate_legal_distribution.py dist
```

공개 source bundle은 기존 TAXax 저장소 전체를 복사하지 않고 allowlist exporter로 새 빈 디렉터리에 만듭니다.

```bash
python scripts/export_legal_public.py /path/to/new-empty-destination
```

포함·제외 경계는 [`PUBLIC_ALLOWLIST.md`](PUBLIC_ALLOWLIST.md)에 설명합니다.

## 운영 검증 상태

- L1 법제처 adapter: 합성 fixture 검증 완료
- L2 NTS/OLTA adapter: 합성 fixture 검증 완료
- L3 결정론 research workflow: local/합성 테스트 완료
- JWT/JWKS와 server fail-closed: 합성 RSA token 및 설정 테스트 완료
- 실제 공식 API 호출: 승인 credential과 이용조건이 없는 환경에서는 미실시
- 실제 IdP/reverse proxy/DNS/직원 client 연결: 운영 인프라가 없는 환경에서는 미실시

공식 자료의 수록 범위, 최신성, 재이용조건은 provider별 정책을 별도로 확인해야 합니다. 소프트웨어의 MIT license가 수집 원문의 재배포 권한을 의미하지 않습니다.
