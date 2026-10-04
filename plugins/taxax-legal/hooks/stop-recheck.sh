#!/bin/sh
# TAXax 법률 답변 1회 재검토 — Claude 플러그인 Stop 훅. jq·python 없이 동작한다.
# 판정 단어는 taxax.legal.codex_hook._LEGAL_CONTENT와 같다(tests/test_legal_plugins.py가 대조).
# Claude의 stop_hook_active는 Codex와 뜻이 달라 쓰지 않고, 질문(prompt_id)마다 한 번만 막는다.
input=$(cat)
case "$input" in *'"last_assistant_message"'*) ;; *) exit 0 ;; esac
# 작업 폴더·대화 기록 경로에 세무 단어가 있어도 답변으로 오인하지 않게 뺀다.
body=$(printf '%s' "$input" | sed 's/"cwd"[[:space:]]*:[[:space:]]*"[^"]*"//; s/"transcript_path"[[:space:]]*:[[:space:]]*"[^"]*"//')
printf '%s' "$body" | LC_ALL=C grep -Eq '법령|법률|판례|결정례|심판례|예규|유권해석|시행령|시행규칙|제[[:space:]]*[0-9]+[[:space:]]*조|세법|국세|지방세|과세|비과세|세무|세금|세율|세액|가산세|원천징수|경정청구|공제|감면|법인세|소득세|부가가치세|취득세|상속|증여|양도세|양도소득|종합부동산세|손금|익금|결손금|대손금|감가상각|시가|부당행위|특수관계|비상장[[:space:]]*주식' || exit 0
key=$(printf '%s' "$input" | sed -n 's/.*"prompt_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | tr -cd 'A-Za-z0-9_-')
if [ -z "$key" ]; then
  # prompt_id가 없는 클라이언트는 이미 한 번 이어진 턴(stop_hook_active=true)이면 막지 않는다.
  printf '%s' "$input" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true' && exit 0
else
  state="${CLAUDE_PLUGIN_DATA:-${TMPDIR:-/tmp}}/taxax-stop-recheck"
  mkdir -p "$state" 2>/dev/null
  [ -e "$state/$key" ] && exit 0
  : > "$state/$key" 2>/dev/null
  find "$state" -type f -mtime +1 -exec rm -f {} + 2>/dev/null
fi
printf '%s\n' '{"decision":"block","reason":"TAXax 법률 답변 최종 점검을 한 번 수행하십시오. 주장마다 법제처·국세청 등 실제 열리는 공식 원문 링크, 해당 조문/사건, 시행·결정일과 인용 문구를 다시 확인하십시오. TAXax MCP 원문·verify_legal_citations 또는 공식 웹 원문을 재조회하고, 원문이나 시점·링크가 확인되지 않으면 그 주장은 빼고 확인 불가로 표시하십시오. 합성 자료를 실제 법령으로 인용하지 마십시오. 이미 충분히 검증했다면 기존 근거를 다시 대조하고 요점만 답하십시오. 고객 자료나 인증키를 새 도구에 보내지 마십시오."}'
