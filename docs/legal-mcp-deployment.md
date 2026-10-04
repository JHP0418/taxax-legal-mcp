# Legal MCP deployment and operations

## 배포 형태

### Windows 로컬 설치기와 개인 stdio

- 첫 공개 최종 사용자 경로이며 중앙 hosted 기능을 제거하지 않습니다.
- frozen setup/CLI/MCP executable은 설치 후 별도 Python이나 TAXax checkout 없이 동작합니다.
- 실행파일은 `%LOCALAPPDATA%\TAXax\app\<version>`, data는 `%LOCALAPPDATA%\TAXax\legal`, provider secret은 `%LOCALAPPDATA%\TAXax\config\provider-secrets.json`에 분리합니다.
- OC는 선택 입력입니다. 없으면 MCP는 기동하고 법제처 upstream만 비활성화합니다.
- secret directory/file은 상속을 제거하고 현재 Windows 사용자 SID에만 `FullControl`을 부여한 뒤 실제 ACL을 검증합니다. 파일은 평문이며 관리자 접근까지 방지하는 암호화 저장소라고 주장하지 않습니다.
- OC는 Claude Desktop JSON의 `env`, command argument, log, manifest에 넣지 않습니다.
- 사용자 동의 후에만 Claude Desktop JSON을 정확히 백업하고 기존 field와 다른 MCP server를 보존해 `taxax-legal` 항목을 원자적으로 병합합니다.
- 같은 이름의 다른 server는 별도 update 동의 없이 교체하지 않으며 malformed JSON과 concurrent change는 fail-closed입니다.
- stdout은 MCP JSON-RPC 전용이며 설치 후 진단은 CLI `doctor`로 별도 실행합니다.

### 중앙 hosted HTTP

- 일반 직원에게 법제처·NTS·OLTA credential 발급이나 입력을 요구하지 않습니다.
- 중앙 운영 서버만 provider credential을 보관합니다.
- 조직 IdP가 JWT를 발급하고 MCP는 JWKS로 asymmetric signature를 검증합니다.
- TLS reverse proxy 뒤 private loopback bind를 권장합니다.
- reverse proxy는 original Host를 보존하고 request body를 128 KiB 이하로 제한합니다.
- 실제 DNS, TLS, IdP app/resource 등록, client settings 변경은 별도 운영 승인 후 수행합니다.

필수 auth 환경변수:

```text
TAXAX_MCP_AUTH_ISSUER
TAXAX_MCP_AUTH_AUDIENCE
TAXAX_MCP_AUTH_RESOURCE
TAXAX_MCP_AUTH_JWKS_URL
TAXAX_MCP_ALLOWED_HOSTS
TAXAX_MCP_ALLOWED_ORIGINS
```

기본 required scope는 `legal.read`, 기본 organization claim은 `org_id`, 기본 algorithm은 `RS256`입니다. 일부 설정만 있으면 서버는 시작하지 않습니다. 인증 없는 non-loopback bind도 시작하지 않습니다.

## Self-host provider 준비

1. 법제처 API 이용 승인과 운영 credential을 준비합니다.
2. NTS/OLTA 공개 조회는 설정이 없으면 기본 활성화됩니다. 운영상 중지가 필요할 때만 `TAXAX_NTS_ENABLED=0` 또는 `TAXAX_OLTA_ENABLED=0`으로 명시적으로 opt-out합니다.
3. 기존 `TAXAX_NTS_TERMS_CONFIRMED`, `TAXAX_OLTA_TERMS_CONFIRMED` 값은 deprecated no-op이며 활성화 조건이 아닙니다.
4. 각 provider의 접근정책, 호출 제한, 수록 범위와 원문 재이용 조건은 별도로 확인하고 준수합니다.
5. 실호출은 최소 공개 키워드로 제한하고 고객명·사업자등록번호·계좌번호 등 식별정보를 보내지 않습니다.
6. provider quota, 수록 범위, 실제 응답 schema를 fixture 검증과 별도로 기록합니다.

### 공식 출처 장애 진단

- [법제처 공동활용 신청·이용안내](https://open.law.go.kr/LSO/information/guide.do)에서 OC와 자료 종류별 **목록/본문·형식(XML/JSON)** 승인 상태를 먼저 확인합니다. 신청 화면의 선택 체크만으로 실제 승인·접근 성공을 증명하지 않습니다. 미신청 안내나 `AUTH_FAILED`를 IP 차단으로 단정하지 마십시오. TAXax의 국세청 해석 `ntsCgmExpc` 목록은 법제처 색인이며 해당 본문은 국세청 원본에서 확인합니다.
- `open.law.go.kr/LSO/openApi/guideList.do` 브라우저 시간초과와 MCP가 호출하는 `www.law.go.kr/DRF/lawSearch.do`·`lawService.do` 실패는 서로 다른 요청입니다. 브라우저 화면 하나만으로 API 또는 IP 차단을 판정할 수 없습니다. 법제처 [API 신청 화면](https://open.law.go.kr/LSO/usrJoin.do)은 짧은 시간의 과도한 호출 제한을 안내합니다. 반복 시험 전에 [공지](https://open.law.go.kr/LSO/support/noticeList.do), 승인 내역 및 사이트의 오류자가진단·IP 접속이력을 확인하고 운영기관에 문의합니다.
- 실패를 재현할 때는 비밀 OC·쿠키·고객자료를 제외한 provider, target, ID/MST, HTTP 상태, 실패 단계(timeout/DNS/TLS/HTTP/parse), 시도 횟수와 응답 안내 문구를 **한 요청 단위로** 기록합니다. `429`나 `Retry-After`, `401/403`, CAPTCHA/접근 차단, 시간초과 뒤 즉시 반복 호출하거나 다른 IP·인증값으로 우회하지 마십시오. 원문이 없으면 검증되지 않은 인용과 적용 결론을 보류합니다.
- 법제처·국세청(`taxlaw.nts.go.kr`)·OLTA(`olta.re.kr`) 호출은 시간초과·연결 실패·5xx에 한해 1회만 재시도하고 `429`·`Retry-After`·접근 차단은 재시도하지 않습니다. 캠페인 실행에서는 첫 실패에 출처 회로를 엽니다. 파싱 오류는 재시도하지 않습니다. 이 정책은 **한 작업 내 요청 증폭만** 방지하며, 서로 다른 MCP 호출을 가로지르는 영구 차단이나 기관의 가동률 보장은 아닙니다. 기관이 정상화되었는지 확인한 뒤 승인된 범위에서 새 요청을 수행합니다.
- 법제처 [API 활용가이드](https://open.law.go.kr/LSO/openApi/guideList.do)의 별표·서식 예시에는 텍스트와 별개인 HWP/PDF 다운로드 링크가 있습니다. 별표 본문이나 파일명만 받았다면 실제 파일 내용을 확인한 것으로 취급하지 않습니다. 다운로드 경로가 열리지 않으면 그 범위를 미확보로 남기고 반복 다운로드로 서버 부하를 키우지 않습니다.

## Backup

서비스를 중지할 필요 없이 SQLite online backup을 사용합니다.

```bash
taxax-legal --data-dir /srv/taxax-legal/data backup /srv/taxax-legal/backups/backup-20260914.zip
```

archive에는 다음이 포함됩니다.

- `v1/legal.sqlite3`
- `private/v1/reports.sqlite3` (이전 설치에 존재하는 경우만 보존)
- `v1/raw/**`
- `v1/extracted/**`
- `v1/runs/**`
- 파일별 byte length와 SHA-256이 있는 `backup-manifest.json`

private report에는 직원 질의 내용이 있을 수 있으므로 archive를 고객자료와 같은 수준으로 암호화·접근통제하고 외부 서비스에 업로드하지 않습니다.

Windows local secret 파일은 일반 legal backup에 포함하지 않습니다. 복원한 PC나 사용자 계정에서는 설치 마법사 또는 명시적 관리 동작으로 OC를 다시 입력해야 합니다.

## Restore 검증

restore는 기존 경로를 덮어쓰지 않고 새 경로만 허용합니다.

```bash
taxax-legal restore /srv/taxax-legal/backups/backup-20260914.zip /srv/taxax-legal/data-restored
TAXAX_LEGAL_DATA_DIR=/srv/taxax-legal/data-restored taxax-legal doctor
TAXAX_LEGAL_DATA_DIR=/srv/taxax-legal/data-restored taxax-legal get-source-status
```

전환 전 다음을 검증합니다.

1. `doctor`의 public/private schema가 정상인지 확인합니다.
2. `get-source-status`의 document/snapshot/report count를 원본과 대조합니다.
3. 대표 `snapshot_ref`의 원본 바이트 SHA-256을 계산하고 `snapshot_raw_sha256`과 대조합니다. 문서의 `raw_sha256`은 개별 정규화 항목의 해시이므로 여러 문서가 공유하는 응답 원본의 바이트 해시와 같다고 가정하지 않습니다.
4. 대표 공개 문서를 검색하고 같은 `document_id`의 상세 조회에서 본문 확보 상태·절별 cursor·개별 문서 해시를 대조합니다. 검색 메타데이터만 있으면 원문 확보로 판정하지 않습니다.
5. 이전 버전의 비공개 보고서 DB는 백업·복원에서 보존하되 새 공개 MCP의 조회 기능으로 간주하지 않습니다.
6. 서버를 복원 경로로 기동한 뒤 stdio 또는 인증 HTTP smoke test를 수행합니다.

## Update

1. 새 Python virtualenv 또는 새 container image에 정확한 version을 설치합니다.
2. 현재 data directory를 backup합니다.
3. 새 version에서 복원 복제본을 대상으로 `doctor`, tests, MCP smoke를 실행합니다.
4. 운영 binary/image만 교체하고 data directory는 그대로 유지합니다.
5. source status와 문서 검색·원문 조회·인용 검사를 재확인합니다.

## Rollback

1. 요청 유입을 중지합니다.
2. 이전 virtualenv/container image로 되돌립니다.
3. migration이 없었다면 기존 data directory를 계속 사용합니다.
4. data 변경까지 되돌려야 한다면 backup을 새 경로로 restore하고 검증 후 환경변수를 전환합니다.
5. 기존 DB나 snapshot을 삭제·덮어쓰지 않습니다.

## Container

`Dockerfile`은 non-root 사용자로 실행하고 `/data` volume만 사용합니다. image에 `.env`나 credential을 굽지 않습니다. `compose.yaml`의 host port는 loopback에만 publish하며 실제 외부 공개는 별도 TLS reverse proxy 뒤에서 수행합니다.

## Windows release 검증

Windows 3.11 CI는 `[dev,windows]` extras를 설치하고 PyInstaller one-file setup/CLI/MCP를 빌드합니다. smoke는 manifest hash, setup 내장 payload, 두 개의 서로 다른 cwd에서 `doctor`, 합성 demo, backup/restore, 실제 MCP SDK의 6-tool initialize/list/검색·원문·인용 확인 round trip을 임시 사용자 경로에서 검증합니다. 실제 사용자의 Claude Desktop 설정·credential은 변경하지 않습니다.

`SHA256SUMS.json`은 code signing과 SmartScreen 상태를 별도 field로 기록합니다. 현재 source 기준 기대값은 unsigned와 평판 미확립이며, clean Windows 계정에서의 GUI 설치·Claude Desktop 재시작·code signing·SmartScreen은 실제 release 환경에서 별도로 검증해야 합니다.

## 운영 판정 분리

- package build/test 성공
- 합성 fixture 성공
- unsigned Windows frozen artifact build/smoke 성공
- code signing과 SmartScreen 평판 성공
- 실제 provider 승인·실호출 성공
- IdP/reverse proxy/DNS 성공
- 직원 client 연결 성공

각 항목을 별도로 기록합니다. 앞 단계 성공을 실제 운영 배포 완료로 표현하지 않습니다.
