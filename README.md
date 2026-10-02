# taxax-legal-mcp

`taxax-legal-mcp`는 대한민국 세무 쟁점에 필요한 공식 공개 법률 자료를 조회하는 독립 Python CLI/MCP 서버입니다. AI 클라이언트가 기관·검색어·원문 추가조회·중단을 선택하고, 서버는 원문 보존·출처·시행시점 후보·인용문 대조를 제공합니다. 법적·세무 결론이나 장부 반영을 자동 확정하지 않습니다.

이 공개 package에는 TAXax 회계 engine, 고객 DB, `knowledge/`, 수집 원문, 사설 K-AR 자료가 포함되지 않습니다. GitHub 저장소는 source와 self-host 예제를 제공할 뿐 무료 hosted 운영 서버를 제공하지 않습니다.

## 가장 쉬운 설치 — 에이전트에게 맡기기

ChatGPT 데스크톱(Codex·로컬 Work) 또는 Claude 데스크톱의 Code 탭처럼 **명령을 실행할 수 있는 에이전트**에게 아래 문장을 그대로 붙여넣으십시오.

```text
https://github.com/JHP0418/taxax-legal-mcp 의 README "에이전트 설치 절차"를 그대로 따라 TAXax 법률 MCP를 설치(이미 있으면 업데이트)해 줘.
```

설치 중 에이전트가 **법제처 OC 인증키**를 물으면 알려주십시오(없으면 "없음"이라고 답하면 됩니다. [open.law.go.kr](https://open.law.go.kr)에서 무료로 발급). 설치가 끝나면 사람이 할 일은 두 가지뿐입니다: **앱 완전 종료 후 재시작**, Codex/Work에서 **설정 → Hook에서 `TAXax legal answer recheck` 신뢰**.

> Claude **Cowork**와 ChatGPT **웹·클라우드 Work**는 내 PC에 설치한 MCP를 쓰지 못합니다. 이 설치는 ChatGPT 데스크톱의 Codex·로컬 Work, Claude 데스크톱 일반 대화, Claude Code에서 동작합니다.

### 에이전트 설치 절차

에이전트는 아래 순서를 그대로 실행하고, 각 단계의 실제 출력으로 성공을 판정한다. 실패를 추측으로 넘기지 않는다.

1. **Python 3.11 이상 확인.** `python --version`(Windows에서 `python`이 없으면 `py -3 --version`). 없거나 3.11 미만이면 Windows는 `winget install -e --id Python.Python.3.12 --scope user`, macOS는 `brew install python@3.12`로 설치한 뒤 새 셸에서 다시 확인한다. 이후 단계의 `python`은 확인된 실행 파일로 바꿔 쓴다.
2. **설치·업데이트.** `python -m pip install --upgrade https://github.com/JHP0418/taxax-legal-mcp/archive/refs/heads/main.zip`
3. **OC 키 확인.** 사용자에게 법제처 OC 인증키를 묻는다. 키를 대신 만들거나 추측하지 않는다.
4. **등록.** 키가 있으면 `python -m taxax.legal install --codex --force --oc <키>`, 없으면 `python -m taxax.legal install --codex --force --no-oc`. Claude Desktop과 Codex(ChatGPT 데스크톱 앱 공용 설정)에 함께 등록되고 기존 설정은 백업 후 TAXax 항목만 바뀐다. Claude Code도 쓰면 `--claude-code`를 덧붙인다. 샌드박스가 홈 폴더 설정 쓰기를 막으면 사용자 승인을 요청한다.
5. **진단.** `python -m taxax.legal doctor`의 결과를 그대로 보여준다.
6. **사용자에게 안내.** (a) 앱을 완전히 종료 후 재시작, (b) Codex/Work는 **설정 → Hook → 사용자 구성**에서 `TAXax legal answer recheck` 검토·신뢰, (c) 재시작 후 새 대화에서 "taxax-legal의 get_source_status를 1회 호출해 줘"로 실제 연결 확인. 이 세 가지는 에이전트가 대신 완료했다고 보고하지 않는다.

## 설치 (Windows 기준, 5단계)

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
> python -m taxax.legal install
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
python -m taxax.legal install
```

모듈 실행은 PATH를 타지 않으므로 항상 통합니다. 아래 안내에 나오는 `taxax-legal ...` 명령은 모두 `python -m taxax.legal ...`로 바꿔 쓸 수 있습니다.

이 한 줄이 다음을 모두 처리합니다.

- 법제처 OC 인증키를 물어봅니다. 아직 없으면 [open.law.go.kr](https://open.law.go.kr)에서 무료로 발급받을 수 있고, 그냥 Enter로 건너뛰어도 됩니다(외부 법령 조회만 비활성).
- 데이터 폴더(`%LOCALAPPDATA%\TAXax\legal`)를 만듭니다.
- `%APPDATA%\Claude\claude_desktop_config.json`을 **백업한 뒤** `taxax-legal` 항목만 병합합니다. 다른 MCP 서버 설정은 그대로 보존됩니다.
- `doctor` 진단을 실행해 결과를 보여줍니다.

별도 저장소를 쓰려면 설치 전에 `TAXAX_LEGAL_DATA_DIR`를 설정하거나 `taxax-legal --data-dir <경로> install --codex`처럼 명시하십시오. 선택한 경로가 MCP 등록과 `doctor`에 함께 사용됩니다.

키를 나중에 넣으려면 `taxax-legal install --oc <키>`를 다시 실행하면 됩니다. 같은 명령을 여러 번 실행해도 안전합니다.

> **법제처 API는 자료 종류별 목록·본문과 응답 형식별 이용 신청을 확인해야 합니다.** OC 키가 있어도 미승인 종류를 조회하면 "미신청된 목록/본문에 대한 접근입니다"라는 안내가 올 수 있습니다. [공동활용 신청 화면](https://open.law.go.kr/LSO/usrJoin.do)의 선택 체크만으로 실제 승인·조회 성공을 증명하지 않습니다. `ntsCgmExpc` 목록은 법제처 색인이며 국세청 해석 본문은 국세청 원본에서 확인합니다.

**Claude Code에도 함께 등록하려면** `--claude-code`를 붙입니다. `~/.claude.json`의 `mcpServers`에 `taxax-legal` 항목만 더하고 프로젝트 기록 등 나머지 설정은 그대로 둡니다.

```powershell
taxax-legal install --claude-code
```

**Codex(ChatGPT 데스크톱 앱 · Codex CLI · IDE 확장)에도 함께 등록하려면** `--codex`를 붙입니다.

```powershell
taxax-legal install --codex
```

`~/.codex/config.toml`(또는 `CODEX_HOME`)에 `[mcp_servers.taxax-legal]` 항목만 덧붙이며, 기존 주석·설정·다른 MCP 서버는 그대로 둡니다. 변경 전 원본은 같은 폴더에 백업합니다.
법령 도구는 읽기 전용으로 표시되고, Codex에는 해당 서버에 한해 `default_tools_approval_mode = "writes"`가 설정됩니다. 읽기 요청에도 공식 기관 네트워크 조회(`upstream=true` 또는 상세 `refresh=true`)가 포함될 수 있으므로 기관 이용정책과 호출 비용을 확인하십시오. 등록은 **설치 단계**일 뿐 클라이언트 노출·기관 접근·자동 훅 실행을 보증하지 않습니다. 아래 로컬 Work 점검을 별도로 수행하십시오.
이전 버전에서 이미 `taxax-legal`을 Codex에 등록했다면, 기존 항목 변경을 명시적으로 허용하는 `taxax-legal install --codex --force`를 사용하십시오.

**Codex 재검토 훅도 `--codex` 설치에 포함됩니다.** 기존 `hooks.json`의 다른 훅은 보존하고 원본을 백업한 뒤, TAXax `Stop` 훅을 추가합니다. 로컬 Codex에서 `/hooks` 또는 **설정 → Hook → 사용자 구성**을 열어 **TAXax legal answer recheck**라는 정확한 항목을 직접 검토·신뢰해야 실행됩니다. 화면의 **Ponytail** 등 다른 플러그인 훅에 뜬 `검토 필요`는 TAXax 훅 승인이 아닙니다. `사용자 구성: 훅 1개` 표시만으로 그 항목의 이름·신뢰 상태 또는 자동 실행을 알 수 없습니다. 훅은 법령·조문·판례 등 법적 내용이 있는 답변에 한해 **모델에게 답변 직전 원문·시점·공식 링크를 한 번 더 확인하라고 요청**합니다. 이미 한 번 이어진 턴(`stop_hook_active`)은 다시 막지 않으므로 무한 루프가 없습니다. 추가 모델 호출·기관 조회로 사용량과 지연이 늘 수 있습니다.

설치할 때 만들어지는 Codex 설정 폴더의 `taxax-legal-hook.json`을 편집해 횟수와 켜짐 상태를 직접 바꿀 수 있습니다(재설치해도 기존 선택을 덮어쓰지 않습니다).

```json
{"enabled": true, "max_rechecks": 1}
```

`max_rechecks`는 **0(추가 재검토 없음) 또는 1(최대 한 번)**만 허용합니다. `"enabled": false`로 끌 수 있습니다. 설정 파일을 바꾸는 것은 훅 정의를 바꾸지 않으므로 재신뢰가 필요 없습니다. 설치기에서 `taxax-legal uninstall`로 훅 등록을 제거해도 사용자가 바꾼 설정 파일은 보존합니다. **이 훅은 클라이언트의 독립적인 법률 검증기나 출력 차단 장치가 아닙니다.** Codex가 훅을 신뢰하지 않거나 MCP가 꺼진 경우, 또는 모델이 근거를 확인하지 못한 경우에는 실무자가 최종 원문·시점을 검토해야 합니다.

> ChatGPT **웹/모바일**은 로컬 stdio 서버에 연결하지 못하고 HTTPS 원격 서버만 지원합니다. 이 경로는 아래 "Hosted HTTP" 절을 참고하십시오.

### ChatGPT 데스크톱 Work(로컬 실행) 적용

1. 해당 프로젝트에서 쓰는 Codex 설정 홈(`~/.codex` 또는 실제 `CODEX_HOME`)에 `taxax-legal install --codex --skip-claude`로 등록합니다. `codex mcp get taxax-legal`과 `taxax-legal doctor`로 **설정과 비샌드박스 진단**을 확인합니다. 두 명령의 성공은 Work 대화에서의 MCP 연결 성공이 아닙니다.
2. 데스크톱 앱을 완전히 종료·재시작하고 해당 프로젝트의 **새 로컬 Work/Codex 대화**를 엽니다. **설정 → MCP 서버**에서 `taxax-legal`이 활성인지 확인하고, 지원되는 대화에서 `/mcp`를 확인합니다. `/mcp`가 비어 있거나 메뉴가 없으면 그 대화의 도구가 연결됐다고 간주하지 마십시오. ChatGPT 웹/클라우드 Work는 로컬 설정을 읽지 않습니다.
3. 새 대화에 “`taxax-legal`의 `get_source_status`만 1회 호출해 status와 문서 수를 알려줘”라고 요청하고 **실제 도구 호출**과 `status: ok`를 확인합니다. `doctor`는 공식 API를 호출하지 않습니다. 데이터 폴더가 비어 있으면 0건은 정상입니다. OC가 승인된 종류에만 제한된 공개 검색 1회를 따로 실행해 기관 연결을 점검하십시오.
4. `sqlite3.OperationalError: unable to open database file`이면 Work의 쓰기 제한이 홈의 기본 데이터 폴더를 막았는지 확인합니다. 데이터 폴더를 **신뢰된 프로젝트 안의 비공개·버전관리 제외 경로**로 지정해 `taxax-legal --data-dir <절대경로> install --codex --skip-claude --force`로 다시 등록합니다. 예를 들어 프로젝트의 `.local/taxax-legal`을 쓸 때는 **먼저** `.git/info/exclude`에 `/.local/taxax-legal/`을 추가하고, `git status --short`로 데이터가 스테이징되지 않았음을 확인하십시오. 프로젝트의 `.codex/config.toml`에 `TAXAX_LEGAL_DATA_DIR` 재정의가 있으면 전역 설치 설정보다 우선하므로 두 경로가 일치하는지 확인하십시오. 설치기는 다른 프로젝트의 Git 제외 규칙이나 Work 보안정책을 자동 변경하지 않습니다. 기존 데이터/OC를 자동 복사하거나 설정 범위를 넓히지 않습니다. 새 경로는 빈 색인이므로 필요할 때만 `collect-seeds`로 채우고 기존 법령 캐시는 별도 백업·복원 절차를 사용합니다. `--force`는 기존 TAXax 항목 교체에만 사용합니다.
5. **설정 → Hook → 사용자 구성**에서 정확히 `TAXax legal answer recheck` 훅을 검토·신뢰합니다. 로컬 대화의 법률 답변에서 실제 `Stop` 실행과 모델 재개 기록이 보일 때만 자동 실행됐다고 판정합니다. 훅 직접 실행이나 `get_source_status` 성공으로 대체하지 않습니다. [Codex 훅 문서](https://learn.chatgpt.com/docs/hooks)는 클라우드 오케스트레이션 Work에서 로컬 command 훅이 실행되지 않는다고 설명합니다.

**웹 재확인 허용은 별개입니다.** **설정 → 컴퓨터 사용 → Google Chrome → 설치하기** 화면은 브라우저 확장 설치 상태이지 `law.go.kr`/`taxlaw.nts.go.kr`의 열람 허용이나 MCP 권한 설정이 아닙니다. Chrome 확장 없이 앱의 내장 브라우저를 쓸 수 있는 환경도 있습니다. 브라우저가 특정 URL 접근 허가를 요청할 때는 **정확한 공식 도메인·요청 범위만 확인해 사용자 본인이 승인**하십시오. 사이트별 승인은 TAXax MCP의 공식 API 권한·원문 품질·훅 신뢰와 무관하며, 운영체제/조직 정책의 차단을 설치기가 대신 해제할 수 없습니다. 전체 웹 접근이나 다른 훅까지 일괄 허용할 필요는 없습니다.

### Claude Cowork와 클라우드 ChatGPT Work 적용

**로컬 `pip install`·Claude Desktop 설정·Codex `config.toml`만으로는 두 클라우드 환경에 연결되지 않습니다.** 이 저장소는 인증된 공개 hosted 서버나 ChatGPT용 설치 플러그인을 제공하지 않으므로 지금 단계에서 두 클라우드 제품을 원클릭 설치했다고 안내하지 않습니다.

1. 조직 운영자가 별도 호스트에 `taxax-legal-mcp --transport streamable-http`를 배포하고 공개 인터넷에서 접근할 수 있는 **HTTPS `/mcp`** 주소를 제공합니다. 법제처 OC는 그 서버에만 보관합니다. 조직 IdP의 JWT 발급·JWKS·필요 claim/scope와 TLS·Host/Origin 허용 목록을 먼저 구성해야 서버가 기동합니다. `localhost` 또는 개인 PC 파일 경로를 커넥터 URL로 넣지 마십시오. [Hosted HTTP 설정](#hosted-http)과 [운영 절차](docs/legal-mcp-deployment.md#중앙-hosted-http)를 참조하십시오.
2. **Claude Cowork:** [Customize → Connectors](https://claude.ai/customize/connectors)에서 Pro/Max 사용자는 **Add custom connector**에 운영 HTTPS URL을 추가하고 인증 방법을 확인합니다. Team/Enterprise는 먼저 관리자가 **Organization settings → Connectors → Add → Custom → Web**에 서버를 등록하고, 구성원이 각각 **Connect**로 인증합니다. OAuth 또는 요청 헤더 인증을 쓰는 경우 서버의 JWT 계약과 호환되는지 운영자가 확인해야 합니다. 대화의 **+ → Connectors**에서 이 커넥터를 켜고 새 합성 질문으로 실제 호출을 확인합니다. 연결 실패 시 URL의 외부 접근성·TLS·인증·조직 승인 상태를 각각 점검합니다. [Anthropic 공식 안내](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).
3. **클라우드 ChatGPT Work:** 조직 관리자가 이 HTTPS MCP에 연결하는 **ChatGPT 호환 플러그인**을 별도 제작·게시·승인해야 합니다(현재 저장소에는 없음). Work의 **Plugins**에서 해당 플러그인을 설치하고 요청 시 인증을 연결한 뒤 **새 대화**에서 `get_source_status`의 실제 호출을 확인합니다. [ChatGPT 플러그인 안내](https://learn.chatgpt.com/docs/plugins). 앱의 로컬 Codex MCP 서버 목록이나 `/mcp`가 보이지 않는 것만으로 호스팅 플러그인 연결을 판정하지 마십시오.
4. 이 클라우드 경로들에서는 **TAXax 로컬 Stop 훅을 자동 적용하지 않습니다.** 두 환경 모두 원문·시점·인용을 사람과 실제 도구 기록으로 재검토해야 하며, 연결 성공만으로 법률 답변 품질이 검증되지는 않습니다.

> **업그레이드가 `WinError 32`로 실패한다면** MCP client가 서버를 띄워 둔 상태라 실행파일이 잠긴 것입니다.
> `Access is denied` 또는 `다른 프로세스가 파일을 사용 중`이라는 메시지가 나오면 아래로 정리한 뒤 다시 설치하십시오.
>
> ```powershell
> taxax-legal stop-servers
> ```
>
> 서버는 상태를 DB에 두므로 종료해도 잃는 자료가 없고, MCP client를 재시작하면 다시 연결됩니다.

### 4단계 — 법률 색인 채우기

설치 직후 로컬 법률 색인은 **비어 있습니다.** 이 상태에서는 조회해도 0건이 나오며, 응답에 "아직 수집하지 않은 상태"라는 경고가 함께 옵니다. 아래 명령으로 기본 법령을 받아 두십시오. 3단계에서 OC 인증키를 등록했어야 합니다.

```powershell
taxax-legal collect-seeds
```

한 번 채워 두면 이후에는 인터넷 없이도(`upstream` 없이) 조회할 수 있습니다. 최신 개정을 반영하려면 가끔 다시 실행하십시오.

### 5단계 — Claude Desktop 재시작

완전히 종료한 뒤 다시 실행하면 도구 6개가 나타납니다. Claude에게 이렇게 물어보십시오.

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
- 법제처 upstream 사용 시 **사용자가 [open.law.go.kr](https://open.law.go.kr)에서 직접 발급받은 OC 인증키**. `taxax-legal install`로 저장하거나 `TAXAX_LAW_GO_OC`로 지정합니다. 이 배포본은 공용 인증키를 대신 제공하지 않습니다(각자 발급 방식)
- NTS/OLTA 공개 조회는 기본 활성화됩니다. 운영상 중지가 필요하면 `TAXAX_NTS_ENABLED=0` 또는 `TAXAX_OLTA_ENABLED=0`으로 명시적으로 opt-out합니다. 기존 `TERMS_CONFIRMED` 값은 deprecated no-op입니다.
- K-AR와 `korean-law-mcp` bridge는 선택 기능이며 없어도 CLI, stdio MCP, 공개 법률 조회, research workflow가 기동

## Windows 로컬 설치 마법사

첫 공개 경로는 Windows 로컬 설치기입니다. 빌드된 bundle의 `SHA256SUMS.json`과 파일 hash를 먼저 대조한 뒤 `taxax-legal-setup-<version>-windows-<architecture>.exe`를 실행합니다. 최종 사용자는 별도 Python 설치가 필요하지 않습니다.

설치 마법사는 다음 순서로 동작합니다.

1. 법제처 Open API OC를 입력하거나 건너뜁니다. 키가 없어도 local MCP와 offline demo는 기동되고 공식 upstream 조회만 비활성화됩니다.
2. 실행파일을 `%LOCALAPPDATA%\TAXax\app\<version>`, 법률 data를 `%LOCALAPPDATA%\TAXax\legal`에 둡니다.
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
.\.venv\Scripts\python -m pip install .\dist\taxax_legal_mcp-<version>-py3-none-any.whl
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

공개 surface는 다음 6개입니다.

1. `search_knowledge`
2. `search_legal_sources`
3. `get_legal_document`
4. `get_applicable_law`
5. `verify_legal_citations`
6. `get_source_status`

`search_knowledge`는 `TAXAX_PRIVATE_KNOWLEDGE_DIR`를 명시한 경우에만 사설 읽기 전용 K-AR 디렉터리를 검색합니다. 현재 작업 디렉터리나 TAXax checkout의 `knowledge/`를 자동 탐색하지 않습니다.

## 조사 예시

법제처 upstream에는 운영자 OC credential이 필요합니다. NTS/OLTA 공개 조회는 기본 활성화되며 명시적 `ENABLED=0`만 중지로 처리합니다. 각 provider의 접근정책, 호출 제한, 수록 범위는 별도로 준수해야 합니다.

```powershell
$env:TAXAX_LAW_GO_OC = "<operator-managed-value>"
taxax-legal search-legal-sources "법인세법 대손금" --provider law.go.kr --target law --upstream
```

AI 클라이언트가 검색할 기관·검색어와 추가 원문 조회 여부를 선택합니다. `search_legal_sources`의 검색 요약은 원문이 아닙니다. 반환된 `next_cursor`로 같은 기관의 다음 검색 페이지를 조회하고, 검색 항목의 `document_id`로 `get_legal_document`를 호출하십시오. 상세 응답이 `partial`이고 본문이 없다면 `refresh=true`를 명시해 공식 상세를 조회하며, 절별 `next_cursor`가 있으면 필요한 다음 절도 조회합니다. 기준일 법령은 법령 ID가 아닌 해당 시행 버전의 MST와 기준일로 조회하고, 본문 확보 후 `verify_legal_citations`로 문구와 위치를 대조하십시오. **동일한 법령 ID 아래 시행본이 여러 개 있으면 `version_id`를 생략한 캐시 원문·인용 조회는 `TEMPORAL_UNRESOLVED` 또는 `unverified`로 멈춥니다.** 검색·상세 응답의 해당 MST를 `get_legal_document(document_id=…, version_id=…)`, `verify_legal_citations`의 각 인용 항목에 함께 전달하십시오. 과거 캐시에서 MST를 확인할 수 없으면 공식 MST로 새로 조회하십시오. `version_url`은 MST로 구성한 공식 웹 주소이며, 링크가 실제로 열리고 같은 시행본인지 별도 확인해야 합니다. 인증키가 없거나 출처가 응답하지 않으면 확인하지 못한 범위를 밝히고 법적 결론을 자동 확정하지 않습니다. 실제 고객자료는 별도 승인 없이 외부 AI 클라이언트로 보내지 마십시오.

**기관 장애 시** 법제처 HTTP 요청은 기본 1회만 시도하고 `429`는 같은 작업 안에서 자동 재시도하지 않습니다. 국세청·OLTA도 본문·첨부·파싱 오류가 나면 작업 전체를 자동 반복하지 않습니다. 다른 MCP 도구 호출까지 차단하는 회로는 아니므로 `AUTH_FAILED`, `RATE_LIMITED`, `ACCESS_DENIED`, 시간초과 직후 같은 출처를 계속 호출하지 마십시오. 브라우저의 `open.law.go.kr` 가이드 페이지 시간초과는 MCP의 `www.law.go.kr/DRF/` 요청 실패나 IP 차단의 증거가 아닙니다. [이용 신청·장애 진단 절차](docs/legal-mcp-deployment.md#공식-출처-장애-진단)에 따라 승인 범위·출처·실패 단계를 구분하고 미확보 원문은 인용하지 마십시오.

MCP 초기화 지침은 모델에게 답변 직전 각 법률 주장에 대응하는 **실제로 열리는 공식 원문 링크·시점·조문 또는 사건**을 재대조하고, 확인 불가한 주장은 제거하도록 요구합니다. 웹에서 발견한 링크도 해당 공식 페이지를 다시 확인해야 합니다. `verify_legal_citations`의 `verified`는 저장된 문서의 문구 일치만 뜻하며 링크 접근성·시행 버전·법적 적용을 보증하지 않습니다. **MCP 서버 자체는 클라이언트가 마지막에 작성하는 문장을 관찰하거나 모델의 두 번째 호출을 강제할 수 없습니다.** 위의 Codex `Stop` 훅은 설치·신뢰된 Codex 로컬 세션에서만 최대 1회 이어쓰기를 요청합니다. 모든 답변에 대한 기계적 강제가 필요하면 클라이언트의 최종 출력 승인 단계에서 별도로 검증하고 실패 시 출력을 차단해야 합니다.

## 데이터 분리

기본 `TAXAX_LEGAL_DATA_DIR` 아래에서 다음을 분리합니다.

```text
v1/legal.sqlite3              공개 법률 metadata/index
v1/raw/**                     append-only 원문 snapshot
v1/extracted/**               parser-version별 정규화 결과
v1/runs/**                    수집 run manifest
private/v1/reports.sqlite3    이전 버전에 파일이 있을 때만 백업·복원(새 설치에는 생성되지 않음)
```

이전 버전의 보고서 DB는 백업·복원에서 보존합니다. 새 공개 MCP는 조사 보고서를 생성하거나 조회하지 않습니다. 과거 DB의 접근 권한과 백업 암호화 수준을 낮추지 마십시오.

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

복원은 기존 경로를 덮어쓰지 않습니다. 새 경로에서 `doctor`, 출처별 문서 수, 원문 snapshot hash를 검증한 후 환경변수를 전환하십시오. 과거 보고서 DB가 있는 경우 백업·복원 파일 보존 여부만 확인하며 공개 MCP에서 조회하지 않습니다.

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
- AI 주도 조사 도구 계약: 합성 fixture와 오프라인 안전 probe 검증 완료. 실제 모델의 조사 선택·법적 답변 품질·3회 재생 및 구현 전 성능 기준선 대비 10% 판정은 미완료
- JWT/JWKS와 server fail-closed: 합성 RSA token 및 설정 테스트 완료
- 실제 공식 API 호출: 법제처는 사용자 발급 OC가 필요하며, NTS/OLTA는 공개 조회 경로와 provider 접근정책·호출 제한을 준수
- 실제 IdP/reverse proxy/DNS/직원 client 연결: 운영 인프라가 없는 환경에서는 미실시

공식 자료의 수록 범위, 최신성, 재이용조건은 provider별 정책을 별도로 확인해야 합니다. 소프트웨어의 MIT license가 수집 원문의 재배포 권한을 의미하지 않습니다.
