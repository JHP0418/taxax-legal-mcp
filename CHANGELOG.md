# Changelog

## 0.2.14 - 2026-10-02

- `install`에서 명시한 `--data-dir` 또는 `TAXAX_LEGAL_DATA_DIR`를 Codex/Claude 등록과 진단에 그대로 사용합니다. 이전에는 기본 경로를 등록해 다른 저장소를 조회할 수 있었습니다.

## 0.2.13 - 2026-10-01

- Windows PyInstaller 빌드가 namespace `taxax` 아래의 실행 진입점 때문에 내부 `taxax.mcp`를 외부 SDK `mcp`로 오인하지 않도록, 세 진입점을 임시 빌드 디렉터리로 복사해 분석합니다.

## 0.2.12 - 2026-10-01

- Windows PyInstaller의 MCP 진입점을 다른 하위 패키지로 옮겼지만, 공개 namespace 패키지의 import 경로 충돌은 남아 있어 CI에서 MCP 기동이 실패했습니다. 0.2.13에서 빌드 진입점을 격리했습니다.
- Windows CI의 캠페인 SQLite 테스트가 열린 DB 파일을 임시 폴더에서 삭제하려다 실패하던 문제를 해결했습니다.
- 공개 배포에는 비공개 E2E 스크립트가 필요한 테스트를 포함하지 않습니다.

## 0.2.9 - 2026-09-15

배포 파이프라인이 실제로 통과하도록 환경 의존성과 버전 관리 방식을 정리했습니다.

- **PSModulePath가 바뀐 환경에서 credential 저장·설정 병합이 전부 실패하던
  문제**: Windows ACL 스크립트가 `Get-Acl` 커맨드릿을 쓰는데, 이 커맨드릿은
  `Microsoft.PowerShell.Security` 모듈에 들어 있어서 PSModulePath를 pwsh용
  경로로 덮어쓴 환경에서는 "module could not be loaded"로 죽는다. 이 상태에서는
  설치, Claude Desktop/Code/Codex 등록, secret 읽기·쓰기가 모두 막힌다(CI의
  Windows job에서 24개 테스트 실패로 드러남). 같은 파일이 `Set-Acl`에 대해
  이미 쓰던 우회 방식대로, 5곳 모두 FileInfo/DirectoryInfo의
  `GetAccessControl()` 메서드로 바꿔 모듈 의존을 없앴다
- **버전 문자열이 파일마다 따로 박혀 어긋나던 문제**: Dockerfile은 `==0.2.0`을
  고정해 둔 탓에 컨테이너 빌드가 깨져 있었고, MCP 서버는 클라이언트에 계속
  0.2.0을 광고했으며, Windows 설치기는 0.2.0, 설치 경로 상수는 0.2.1이었다.
  이제 `taxax.legal.version.package_version()` 한 곳에서만 읽는다

## 0.2.8 - 2026-09-15

외부 스트레스 테스트 리포트를 항목별로 재현 검증하며 남은 데이터 무결성 문제를
정리했습니다.

- **검색 한 페이지의 모든 문서가 같은 raw_sha256을 갖던 문제**: snapshot의
  raw_sha256은 HTTP 응답 페이지 전체의 해시인데 이 값을 페이지에 포함된 각
  문서에 그대로 복사하고 있었다. 그래서 법인세법 / 시행령 / 시행규칙처럼
  완전히 다른 법령이 항상 같은 해시를 갖게 돼, "이 문서의 원문 해시"라는
  필드 의미가 깨졌다(실제 DB에서 15개 그룹 확인). 이제 문서별로 그 항목의
  내용만으로 해시를 계산한다. 같은 페이지에서 왔다는 출처(snapshot_ref)는
  그대로 공유한다
- **research_as_of 미래 날짜 무검증**: "이 시점 기준으로 조사했다"는 출처
  표기인데 2099년 같은 미래 날짜를 그대로 받아들여, 아직 오지 않은 시점의 법
  상태를 조사한 것처럼 보고서에 남았다. 이제 거부하되 UTC/KST 하루 차이는
  허용한다

## 0.2.7 - 2026-09-15

실사용 스트레스 테스트에서 나온 심각한 안정성 문제를 고쳤습니다. 그중 하나는
한 번의 이상 입력이 이후 동일한 조사 요청을 영구히 막아버리는 문제였습니다.

- **연구 파이프라인 영구 락 해소**: 프로세스가 응답 없이 죽거나 MCP client가
  먼저 연결을 끊으면, 진행 중이던 수집 작업(run)이 'running' 상태로 DB에
  영원히 남아 이후 동일한 요청을 재시도해도 매번 "같은 요청이 이미 실행
  중"으로만 거부됐습니다. 10분 넘게 멈춰 있는 run은 죽은 것으로 보고
  재시도가 되찾을 수 있게 했고, 서버가 새로 뜰 때도 같은 기준으로 정리합니다
  (get_source_status가 죽은 작업을 계속 "실행 중"이라고 보여주던 문제도 함께
  해결됩니다)
- **research_tax_issue의 max_seconds가 실제로 안 지켜지던 문제**: 예산은
  요청 사이에서만 검사돼, 이미 시작한 HTTP 시도 하나가 기본 소켓 timeout
  (law.go.kr 30초, NTS/OLTA 20초)까지 그대로 걸릴 수 있었다(5초 예산에
  22초 이상 걸린 사례 확인). 활성 예산의 남은 시간을 실제 소켓 timeout에
  반영해, 짧은 예산이 첫 시도부터 지켜지도록 했습니다
- **issue 500자 제한이 사실관계 서술을 막던 문제**: research_tax_issue의
  issue는 검색 키워드가 아니라 사실관계 서술인데, search_knowledge용 500자
  제한을 그대로 썼다. 실제 세무 케이스는 복잡할수록 길어지는데 정작 복잡한
  케이스일수록 입력을 거부하는 역설이 있었다. issue 전용 4000자 제한으로
  분리하고, 업스트림 검색에 실제로 나가는 쿼리는 provider가 받는 500자로
  줄이도록 했습니다(전체 서술은 라우팅·보고서에는 그대로 남습니다)
- NTS 검색·상세 결과의 제목·본문에 남아있던 `<!HS>`/`<!HE>` 하이라이트
  마크업을 제거했습니다

## 0.2.6 - 2026-09-15

실제 설치를 처음부터 다시 해 보며 드러난 배포 경로 문제를 정리했습니다.

- `install --claude-code` 추가. 지금까지 Claude Code(`~/.claude.json`) 등록만 자동화되지 않아 손으로 맞춰야 했습니다. 프로젝트 기록 등 MCP 외 설정은 건드리지 않고 우리 항목만 병합하며, `uninstall`도 함께 정리합니다
- `stop-servers` 추가. MCP client가 서버를 띄워 둔 상태에서는 Windows가 실행파일을 잠가 `pip install --upgrade`가 WinError 32로 실패하는데, 무엇을 닫아야 하는지 알 방법이 없었습니다. `install`도 이전 버전 서버가 남아 있으면 안내합니다
- 서버 탐지는 실행 이미지 기준으로만 판단하고 자신과 부모 프로세스를 제외합니다. 명령줄에 이름이 스쳐 지나간 셸까지 종료 대상에 넣으면 사용자의 터미널을 죽일 수 있습니다

## 0.2.5 - 2026-09-15

실 API 검증에서 드러난 수집 경로 오류를 정리했습니다. 이전까지 법제처 조회는 전량 실패했고 NTS 상세는 본문 없이 저장됐습니다.

- 법제처: 정상 응답에 되돌아오는 OC를 저장 전 치환하도록 변경(기존에는 전량 차단). `official_url`에서 인증 파라미터 제거 및 절대경로 정규화
- 법제처: 행정규칙 `ID`/`LID` 식별자 매핑이 공식 가이드와 반대였던 문제 수정. MST 조회 시 응답에 없는 식별자를 대조하려다 실패하던 문제 수정
- 법제처: 권한 미신청·HTML 전용 target(`lsHistory`, 국세청 출처 판례)에 대해 원인을 알 수 있는 메시지로 응답. `lsHistory`는 조사 대상에서 제외
- NTS: 상세 본문 필드명이 실제 응답과 달라 제목·본문이 비어 있던 문제 수정
- OLTA: 텍스트 추출 순서 오류, 결과 목록 탐색 실패, category 표기 불일치, 본문에 화면 UI 문구가 섞이던 문제 수정
- Windows: `Set-Acl`이 디렉터리에서 `SeSecurityPrivilege`를 요구해 인증키 저장이 실패하던 문제 수정
- 설치: `--nts`/`--olta` 동의 절차를 install 흐름에 추가

## 0.2.0 - 2026-09-14

- 공식 법제처 adapter와 append-only raw snapshot/index 계약 추가
- NTS/OLTA 제한 보완 adapter와 provider policy gate 추가
- 결정론적 세무 조사, 다차원 evidence rank, citation/temporal review 추가
- 공개 cache와 scope별 private report DB 분리 및 report IDOR 방지
- stdio와 JWT/JWKS 인증 Streamable HTTP, 8개 MCP tool 추가
- 합성 offline demo, doctor, SQLite online backup/비파괴 restore 추가
- wheel/sdist 공개 allowlist, container, CI, 운영 문서 추가

실 API와 실제 IdP/reverse proxy/client 운영 검증은 별도이며 이 release의 fixture 검증과 동일하지 않습니다.
