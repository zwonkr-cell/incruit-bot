# 인크루트 신입 공고 → 텔레그램 알림 봇

GitHub Actions 에서 돌아가며, 조건에 맞는 **새 공고**를 텔레그램으로 알리고 구글 시트에 기록합니다.

## 실행 주기
- **주 트리거**: 외부 스케줄러(구글 Apps Script / cron-job.org)가 주간 10분·야간 1시간마다 `workflow_dispatch` 로 실행
  → 설정 방법: [catch-alert 리포의 TRIGGER_SETUP.md](https://github.com/zwonkr-cell/catch-alert/blob/main/TRIGGER_SETUP.md)
- **안전망**: `.github/workflows/run.yml` 의 GitHub 예약(혼잡한 정각·30분을 피한 시각)
  · GitHub 예약만으로는 2026-08 말부터 하루 35회 중 약 6회만 실행돼 알림이 몇 시간씩 늦었습니다.

## 동작
1. **수집** — `netkit.py`(3개 봇 공통 파일)가 직접 연결과 무료 한국 프록시를 **동시에 경주**시켜 가장 빠른 정상 경로를 씁니다.
   직접 연결이 막히는 러너(일부 해외 IP 대역)에서도 수 초 안에 프록시로 넘어갑니다.
   통했던 프록시는 `bot_state.json` 의 `proxy_pool` 에 기억해 다음 실행에 먼저 씁니다.
2. **파싱** — 최근 등록순(sortfield=reg) 목록 1페이지(조건에 맞는 공고 전체, 현재 약 40건) 받아, 신규가 한 페이지를 꽉 채우면 다음 페이지까지 확인합니다.
   선택자가 바뀌어도 버티도록 대체 경로를 두었고, 공고를 못 읽으면 '구조 변경 의심'으로 알립니다.
3. **알림** — 오래된 공고부터 전송. **전송에 성공한 공고만** `processed_incruit_ids.txt` 에 기록하므로, 텔레그램 오류 시 다음 실행에서 다시 보냅니다.
4. **시트 기록** — 알림을 모두 보낸 뒤 Apps Script 웹앱(`SHEET_WEBHOOK_URL`)으로 기록합니다.

## 수동 실행 (Actions → 워크플로 → Run workflow)
| mode | 하는 일 |
|---|---|
| `run` | 정상 실행 |
| `dry-run` | 텔레그램·시트·상태 저장 없이 무엇을 보낼지 로그로만 확인 |
| `dry-run-proxy` | dry-run + 직접 연결을 건너뛰고 한국 프록시 경로만 점검 |

로컬: `pip install -r requirements.txt` 후 `python incruit_bot.py --dry-run`

## 테스트
```bash
python -m unittest discover -s tests -v
```
- `tests/test_parser.py`: `tests/fixtures/` 의 실제 페이지(2026-11-05 저장)로 파서 회귀 테스트
- `tests/test_netkit.py`: 로컬 가짜 사이트/프록시로 경주·장애 전환·프록시 풀 관리 테스트(인터넷 불필요)

사이트가 개편되면 새 페이지를 `tests/fixtures/` 에 저장하고 파서를 고친 뒤 테스트로 확인하세요.

## 필요한 Secrets
`TG_TOKEN`(봇 토큰) · `TG_CHAT_ID`(콤마로 여러 명) · `SHEET_WEBHOOK_URL`(선택)
