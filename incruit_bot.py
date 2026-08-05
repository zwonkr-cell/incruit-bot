import requests
from bs4 import BeautifulSoup
import os
import re
import html
import json
import time
import traceback
from datetime import datetime, timezone, timedelta

# 1. 설정
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
# 2026-07 중순 인크루트가 신입관(/entry/)을 React 기반 'Navi'로 개편하면서 구 페이지가
# 리다이렉트됨 → 동일 필터를 클래식 검색(searchjob.asp)에 적용 (마크업/파라미터 호환 확인됨)
# crr=1(신입): 구 신입관은 경력직이 자동 배제됐지만 클래식 검색은 경력 필터가 필요함(2026-07-21 추가)
TARGET_URL = "https://job.incruit.com/jobdb_list/searchjob.asp?jobty=4&jobty=1&group1=7&compty=4&compty=10&scale=2&scale=5&scale=3&group1=17&group1=5&group1=4&group1=1&group1=3&schol=60&occ1=200&occ1=102&rgn2=18&rgn2=14&rgn2=11&crr=1"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Referer': 'https://job.incruit.com/',
}

# ── 차단/접속불가 시 즉시 한국 프록시로 우회 (캐치봇과 동일 방식) ──
PROXY_SOURCES = [
    "https://proxylist.geonode.com/api/proxy-list?country=KR&protocols=http%2Chttps&limit=100&page=1&sort_by=lastChecked&sort_type=desc",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=kr",
]
MAX_PROXY_TRIES = 40
PROXY_TIMEOUT = 10

def get_kr_proxy_candidates():
    cands = []
    for u in PROXY_SOURCES:
        try:
            r = requests.get(u, timeout=15)
            if "geonode" in u:
                for p in r.json().get("data", []):
                    cands.append(f"{p['ip']}:{p['port']}")
            else:
                cands += [ln.strip() for ln in r.text.splitlines() if ":" in ln]
        except Exception as e:
            print("프록시 목록 수집 실패:", e)
    return list(dict.fromkeys(cands))

def _parse_jobs(html_text):
    soup = BeautifulSoup(html_text, 'html.parser')
    job_list = []
    for item in soup.select('ul.c_row'):
        try:
            job_id = item.get('jobno')
            company = item.select_one('.cpname').get_text(strip=True)

            # 지역 정보 추출 (cl_md 클래스 내부의 첫 번째 span)
            location_tag = item.select_one('.cl_md span')
            location = location_tag.get_text(strip=True) if location_tag else "지역미정"

            title_tag = item.select_one('.cell_mid .cl_top a')
            title = title_tag.get_text(strip=True)
            link = title_tag['href']
            if not link.startswith('http'):
                link = "https:" + link

            info_spans = item.select('.cell_last .cl_btm span')
            deadline = info_spans[0].get_text(strip=True) if len(info_spans) > 0 else "마감정보 없음"
            reg_time = info_spans[1].get_text(strip=True) if len(info_spans) > 1 else ""
            # 괄호 제거 (예: (8일전 등록) -> 8일전 등록)
            reg_time = reg_time.replace('(', '').replace(')', '')

            job_list.append({
                'id': job_id,
                'company': company,
                'location': location,
                'title': title,
                'link': link,
                'deadline': deadline,
                'reg_time': reg_time
            })
        except: continue
    return job_list

def get_jobs(state=None):
    """반환: (jobs, transport)  transport = 'direct' 또는 'proxy:IP'"""
    saw_blocked = False
    saw_empty_200 = False
    # 1) 직접 연결 3회
    for attempt in range(3):
        try:
            print(f"{attempt + 1}번째 인크루트 직접 접속 시도...")
            res = requests.get(TARGET_URL, headers=HEADERS, timeout=30)
            res.encoding = 'euc-kr'
            if res.status_code == 200:
                jobs = _parse_jobs(res.text)
                if jobs:
                    return jobs, "direct"
                saw_empty_200 = True
                print("응답은 200이지만 공고 목록 파싱 0건")
            elif res.status_code in (403, 429):
                saw_blocked = True
                print(f"HTTP {res.status_code} (차단 의심)")
            else:
                print(f"HTTP {res.status_code}")
        except Exception as e:
            print(f"에러: {e}")
        time.sleep(8)
    # 2) 직접 실패 → 즉시 한국 프록시 폴백
    print("직접 연결 실패 → 한국 프록시 폴백 시도")
    cands = get_kr_proxy_candidates()
    lp = (state or {}).get("last_proxy")
    if lp:
        cands = [lp] + [c for c in cands if c != lp]   # 직전 성공 프록시 최우선
    print(f"프록시 후보 {len(cands)}개" + (f" (직전 성공 {lp} 우선)" if lp else ""))
    for p in cands[:MAX_PROXY_TRIES]:
        prox = {"http": f"http://{p}", "https": f"http://{p}"}
        try:
            res = requests.get(TARGET_URL, headers=HEADERS, proxies=prox, timeout=PROXY_TIMEOUT)
            res.encoding = 'euc-kr'
            if res.status_code == 200:
                jobs = _parse_jobs(res.text)
                if jobs:
                    print(f"프록시 우회 성공: {p}")
                    return jobs, f"proxy:{p}"
        except Exception:
            continue
    # 3) 프록시도 실패 → 원인별 예외 (오류 분류 노티로 이어짐)
    if saw_empty_200:
        raise RuntimeError("페이지 응답은 정상(200)이지만 공고를 읽지 못했습니다 - 사이트 구조 변경 의심")
    if saw_blocked:
        raise RuntimeError("접근 차단 의심(403 등)이며 프록시 우회도 실패 - 다음 실행에서 재시도")
    raise RuntimeError("인크루트 접속 실패(직접+프록시 모두) - 사이트 접근 불가")

def handle_transport(state, transport):
    """연결 방식(직접/프록시) 변화를 기록만 한다.
    프록시 우회는 정상 동작의 일부 - 텔레그램 알림 없음(2026-07 사용자 요청: 프록시 알림 전면 중단)."""
    prev = str(state.get("transport", "direct"))
    if prev != transport:
        print(f"연결 방식 변경: {prev} -> {transport} (알림 없음)")
    state["transport"] = transport

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # TG_CHAT_ID 에 콤마/공백으로 여러 명을 넣을 수 있습니다. (예: "8755814064,8467039744")
    chat_ids = [c for c in re.split(r"[,\s]+", TG_CHAT_ID or "") if c]
    for cid in chat_ids:
        data = {
            "chat_id": cid,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        res = requests.post(url, data=data, timeout=10)
        if res.status_code != 200:
            print(f"전송 실패 (chat_id={cid}): {res.text}")

# ─────────────────────────────────────────────────────────────
# 상태(bot_state.json) + 오류 분류 노티 + 12시간 무신규 하트비트
# ─────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
STATE_FILE = "bot_state.json"
HEARTBEAT_HOURS = 12       # 이 시간 동안 새 공고가 없으면 '신규 없음' 노티
ERROR_DEDUP_HOURS = 12     # 같은 유형 오류는 이 시간에 한 번만 노티(스팸 방지)
BOT_NAME = "인크루트봇"

def _now_iso():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

def _parse_iso(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except Exception:
        return None

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("state 저장 실패:", e)

def send_plain(text):
    """모든 chat_id 에 일반텍스트 전송(오류/하트비트/리포트 노티용)."""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for cid in [c for c in re.split(r"[,\s]+", TG_CHAT_ID or "") if c]:
        try:
            requests.post(url, data={"chat_id": cid, "text": text[:3900],
                                     "disable_web_page_preview": True}, timeout=10)
        except Exception as e:
            print("노티 전송 실패:", cid, e)

# ── 구글 시트 기록(Apps Script 웹앱으로 POST) ──
SHEET_WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL", "").strip()

def log_to_sheet(payload):
    """새 공고 1건을 구글 시트에 기록. 실패해도 알림/봇 동작에는 영향 없음."""
    if not SHEET_WEBHOOK_URL:
        return
    try:
        requests.post(SHEET_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print("구글시트 기록 실패:", e)

# ── 오류 분류: (키, 이모지, 심각도, 알림까지 필요한 연속횟수, 키워드, 제목, 원인, 조치) ──
ERROR_CATEGORIES = [
    ("structure", "🔴", "높음 · 조치 필요", 1,
     ["구조 변경", "파싱 0건", "attributeerror", "keyerror", "indexerror", "nonetype"],
     "사이트 구조 변경 의심 — 공고를 읽지 못했어요",
     "인크루트가 페이지를 개편하면 봇이 공고 위치를 찾지 못하게 돼요.",
     "네, 코드 수정이 필요해요. 이 알림 내용을 개발 세션(클로드)에 전달해 주세요. 수정 전까지 새 공고 알림이 중단돼요."),
    ("blocked", "🟠", "중간 · 지켜보기", 1,
     ["403", "forbidden", "captcha", "차단"],
     "사이트가 접근을 차단했을 가능성",
     "사이트가 자동 수집을 일시적으로 막았을 수 있어요. 봇이 한국 프록시 우회까지 자동 시도했지만 이번엔 실패했어요.",
     "당장 조치는 필요 없어요(다음 실행에서 다시 우회 시도). 이 알림이 하루 이상 반복되면 개발 세션에 전달해 주세요."),
    ("network", "🟡", "낮음 · 조치 불필요", 2,
     ["접속 실패", "접속 5회 모두 실패", "connection", "timeout", "timed out", "10054", "reset", "aborted", "urlerror"],
     "일시적 접속 장애",
     "서버 혼잡이나 순간적인 네트워크 문제로 가끔 발생해요(프록시 우회도 함께 시도했어요).",
     "아니요. 다음 실행에서 자동 복구되고, 놓친 공고도 그대로 알림돼요."),
    ("state", "🟠", "중간 · 지켜보기", 1,
     ["oserror", "permissionerror", "json.decoder", "state 저장"],
     "기록 파일 저장/읽기 문제",
     "공고 기록 파일을 읽거나 쓰는 데 문제가 생겼어요.",
     "일시적일 수 있어요. 반복되면 개발 세션에 전달해 주세요."),
]

def classify_error(raw_text):
    low = (raw_text or "").lower()
    for key, emoji, sev, min_consec, kws, title, why, action in ERROR_CATEGORIES:
        if any(k in low for k in kws):
            return {"key": key, "emoji": emoji, "sev": sev, "min_consec": min_consec,
                    "title": title, "why": why, "action": action}
    return {"key": "unknown", "emoji": "🔴", "sev": "높음 · 조치 필요", "min_consec": 1,
            "title": "알 수 없는 오류",
            "why": "예상하지 못한 문제가 발생했어요.",
            "action": "네, 확인이 필요해요. 이 알림 내용을 개발 세션(클로드)에 전달해 주세요."}

def notify_error(state, raw_text):
    """오류를 분류해 쉬운 설명으로 노티. 일시적 오류는 연속 2회부터, 같은 유형은 12h 1회."""
    raw_text = (raw_text or "").strip() or "알 수 없는 오류"
    summary = raw_text.splitlines()[-1][:100]
    info = classify_error(raw_text)
    consec = state.setdefault("consec_err", {})
    cnt = consec.get(info["key"], 0) + 1
    consec[info["key"]] = cnt
    # 크리티컬(구조 변경, 미분류)만 전송. 나머지는 로그만 - 하트비트가 장기 장애를 잡아줌(2026-07 사용자 요청).
    if info["key"] not in ("structure", "unknown"):
        print(f"[{info['key']}] 비크리티컬 오류 -> 텔레그램 생략(로그만): {summary}")
        return
    if cnt < info["min_consec"]:
        print(f"[{info['key']}] 1회성 오류 → 알림 보류(연속 {info['min_consec']}회부터 알림)")
        return
    last_at = _parse_iso(state.get("err_notified_at", {}).get(info["key"]))
    if last_at and (datetime.now(KST) - last_at) < timedelta(hours=ERROR_DEDUP_HOURS):
        print(f"[{info['key']}] 같은 유형 최근 알림됨 → 생략(스팸 방지)")
        return
    consec_note = f" (연속 {cnt}회째)" if cnt >= 2 else ""
    # 새로운(미분류) 유형은 판단 근거가 없으므로 오류 원문을 함께 첨부
    if info["key"] == "unknown":
        tail = f"■ 오류 원문 (처음 보는 유형이라 원문을 함께 보내요)\n{raw_text[:1500]}\n"
    else:
        tail = f"(참고: {summary})\n"
    msg = (f"{info['emoji']} [{BOT_NAME}] 오류 알림 — 심각도: {info['sev']}\n"
           f"\n"
           f"■ 무슨 오류인가요?\n{info['title']}{consec_note}\n"
           f"\n"
           f"■ 왜 발생하나요?\n{info['why']}\n"
           f"\n"
           f"■ 조치가 필요한가요?\n{info['action']}\n"
           f"\n"
           f"{tail}"
           f"(발생 시각: {_now_iso()})")
    send_plain(msg)
    state.setdefault("err_notified_at", {})[info["key"]] = _now_iso()

def maybe_heartbeat(state, collected, new_count):
    now = datetime.now(KST)
    last = _parse_iso(state.get("last_activity_at"))
    if last is None:
        state["last_activity_at"] = _now_iso()   # 최초: 타이머 시작(즉시 노티 방지)
        return
    if (now - last) >= timedelta(hours=HEARTBEAT_HOURS):
        send_plain(f"⏰ [{BOT_NAME}] 최근 {HEARTBEAT_HOURS}시간 내 새로 스크래핑된 채용공고가 없어요.\n"
                   f"수집완료 {collected}건, 신규후보 {new_count}건\n(확인 시각: {_now_iso()})")
        state["last_activity_at"] = _now_iso()

# ── 일일 리포트: 매일 18시(KST) 이후 첫 실행 시 전일18~당일18 신규 공고 요약 ──
REPORT_HOUR = 18
SENT_LOG_KEEP_HOURS = 48

def record_sent(state, company):
    state.setdefault("sent_log", []).append({"company": (company or "(기업명 없음)"), "at": _now_iso()})

def prune_sent_log(state, keep_hours=SENT_LOG_KEEP_HOURS):
    now = datetime.now(KST)
    kept = []
    for e in state.get("sent_log", []):
        t = _parse_iso(e.get("at"))
        if t and (now - t) <= timedelta(hours=keep_hours):
            kept.append(e)
    state["sent_log"] = kept

def maybe_daily_report(state, now=None):
    now = now or datetime.now(KST)
    today_1800 = now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    today_str = now.strftime("%Y-%m-%d")
    # 최초 실행: 이미 18시 지났으면 오늘 리포트는 데이터 없어 건너뜀(다음날부터)
    if not state.get("report_initialized"):
        state["report_initialized"] = True
        if now >= today_1800:
            state["last_report_date"] = today_str
        return
    if now < today_1800 or state.get("last_report_date") == today_str:
        return
    win_start = today_1800 - timedelta(days=1)
    companies = []
    n = 0
    for e in state.get("sent_log", []):
        t = _parse_iso(e.get("at"))
        if t and win_start <= t < today_1800:
            n += 1
            c = e.get("company") or "(기업명 없음)"
            if c not in companies:
                companies.append(c)
    header = (f"📊 [{BOT_NAME}] 일일 리포트\n"
              f"({win_start.strftime('%m/%d %H:%M')} ~ {today_1800.strftime('%m/%d %H:%M')})")
    if n:
        body = (f"\n신규 공고 {n}건 · 기업 {len(companies)}곳\n\n"
                + "\n".join(f"• {c}" for c in companies))
    else:
        body = "\n이 기간에 새로 올라온 공고가 없었어요."
    send_plain(header + body)
    state["last_report_date"] = today_str
    state["last_activity_at"] = _now_iso()   # 리포트도 활동 → 하트비트 리셋

if __name__ == "__main__":
    state = load_state()
    # 일일 리포트(매일 18시 이후 첫 실행) — 수집 실패와 무관하게 먼저 처리
    try:
        maybe_daily_report(state)
    except Exception as e:
        print("일일 리포트 처리 실패:", e)
    prune_sent_log(state)
    try:
        jobs, transport = get_jobs(state)
        handle_transport(state, transport)   # 프록시 우회 시작/지속/복구 노티
        if transport.startswith("proxy:"):
            state["last_proxy"] = transport.split(":", 1)[1]   # 다음 실행에서 최우선 재사용
        db_file = "processed_incruit_ids.txt"
        processed_ids = open(db_file, "r").read().splitlines() if os.path.exists(db_file) else []

        new_count = 0
        new_id_list = []
        for job in reversed(jobs):
            if job['id'] not in processed_ids:
                # 공고명/회사명에 <, >, & 가 있어도 메시지가 깨지지 않도록 이스케이프
                c = html.escape(job['company'])
                t = html.escape(job['title'])
                loc = html.escape(job['location'])
                lk = html.escape(job['link'])
                dl = html.escape(job['deadline'])
                rt = html.escape(job['reg_time'])
                # 재원님이 요청하신 새로운 메시지 양식 적용
                message = (
                    f"<b>{c} - {t}</b>\n\n"
                    f"• {c}({loc})\n"
                    f"• <a href='{lk}'><b>{t}</b></a>\n"
                    f"• {dl}\n\n"
                    f"본 공고는 {rt}됐어요"
                )
                send_telegram(message)
                new_id_list.append(job['id'])
                new_count += 1
                record_sent(state, job['company'])   # 일일 리포트용 기록
                log_to_sheet({
                    "bot": "incruit",
                    "scraped_at": _now_iso(),
                    "company": job['company'],
                    "region": job['location'],
                    "title": job['title'],
                    "link": job['link'],
                    "deadline": job['deadline'],
                    "extra": job['reg_time'],   # 남은 속성(등록시점)
                })
                time.sleep(1.2)

        with open(db_file, "w") as f:
            f.write("\n".join((new_id_list + processed_ids)[:200]))
        print(f"완료: 수집완료 {len(jobs)}건, 신규후보 {new_count}건 발송 시도됨")

        state["consec_err"] = {}   # 성공 → 연속 오류 카운터 리셋

        # 활동 기록 / 12시간 무신규 하트비트
        if new_count > 0:
            state["last_activity_at"] = _now_iso()
        else:
            maybe_heartbeat(state, len(jobs), 0)
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        notify_error(state, tb)   # 분류된 쉬운 설명으로 노티(원문은 로그에만)
    finally:
        save_state(state)
