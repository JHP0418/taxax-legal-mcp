# Third-party notices

`taxax-legal-mcp`의 직접 Python runtime dependencies는 다음과 같습니다.

| Package | Version range | License |
|---|---:|---|
| mcp / mcp-types | >=2.2,<3 | MIT |
| pydantic | >=2.12,<3 | MIT |
| PyJWT | >=2.13,<3 | MIT |
| cryptography (`PyJWT[crypto]`) | dependency closure | Apache-2.0 OR BSD-3-Clause |

MCP의 HTTP runtime closure에는 AnyIO(MIT), httpx2(BSD-3-Clause), Starlette(BSD-3-Clause), Uvicorn(BSD-3-Clause) 등이 포함될 수 있습니다. 설치된 정확한 버전과 전체 license text는 각 distribution metadata를 확인하십시오.

Windows executable은 optional build dependency PyInstaller `>=6.11,<7`로 생성합니다. PyInstaller와 bootloader의 GPL 및 bootloader exception 조건은 release bundle을 만들 때 해당 version의 license text와 함께 별도로 확인해야 합니다. PyInstaller는 Python wheel runtime dependency가 아닙니다.

설계와 endpoint mapping을 검토할 때 다음 MIT 프로젝트를 참고했습니다. 이 package에는 해당 저장소의 원문 데이터나 전체 source tree를 복사하지 않습니다.

- `korean-law-mcp` 4.13.0: 선택 bridge의 package/version/tool allowlist 참고
- `nts-tax-mcp`: NTS/OLTA 공개 endpoint와 session 흐름 참고

선택 bridge는 자동 설치하지 않습니다. 운영자가 `korean-law-mcp` 4.13.0을 별도로 검토·설치하고 명시적으로 활성화한 경우에만 보조 탐색에 사용하며, 결과는 공식 provider로 다시 검증해야 합니다.

법제처·NTS·OLTA의 법률 원문과 공개 자료는 이 Software의 MIT license 대상이 아닙니다. 운영자는 각 기관의 이용조건, 접근정책, 재이용·재배포 조건을 별도로 확인해야 합니다.
