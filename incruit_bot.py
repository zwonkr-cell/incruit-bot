# -*- coding: utf-8 -*-
"""
인크루트 신입 공고 → 텔레그램 알림 봇 (GitHub Actions)

- 수집: netkit(직접 연결 + 한국 프록시 '경주')로 가장 빠른 경로를 씁니다.
- 완전성: '최근 등록순'으로 받아 새 공고가 항상 위에 오게 하고, 신규가 한 페이지를 꽉 채우면
  다음 페이지까지 확인합니다.
- 신뢰성: 텔레그램 전송에 성공한 공고만 '처리됨'으로 기록(실패하면 다음 실행에서 재시도).

사용법:
    python incruit_bot.py              # 정상 실행
    python incruit_bot.py --dry-run    # 전송/저장 없이 무엇을 보낼지 미리보기 (환경변수 DRY_RUN=1 도 동일)
"""
import requests
from bs4 import BeautifulSoup
import os
import re
import sys
import html
import json
import time
import traceback
from urllib.parse import urljoin
from datetime import datetime, timezone, timedelta

from netkit import Net, FetchError

# 1. 설정
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() not in ("", "0", "false", "no") or "--dry-run" in sys.argv
# 2026-07 중순 인크루트가 신입관(/entry/)을 React 기반 'Navi'로 개편하면서 구 페이지가
# 리다이렉트됨 → 동일 필터를 클래식 검색(searchjob.asp)에 적용 (마크업/파라미터 호환 확인됨)
# crr=1(신입): 구 신입관은 경력직이 자동 배제됐지만 클래식 검색은 경력 필터가 필요함(2026-07-21 추가)
TARGET_URL = "https://job.incruit.com/jobdb_list/searchjob.asp?jobty=4&jobty=1&group1=7&compty=4&compty=10&scale=2&scale=5&scale=3&group1=17&group1=5&group1=4&group1=1&group1=3&schol=60&occ1=200&occ1=102&rgn2=18&rgn2=14&rgn2=11&crr=1"
# 2026-11: 기본 정렬(추천순)은 새 공고가 어디에 끼어들지 몰라 '최근 등록순'(sortfield=reg&sortorder=1)으로
# 받습니다(같은 조건·같은 공고 집합 확인). 신규가 한 페이지를 꽉 채우면 다음 페이지까지(최대 MAX_PAGES).
MAX_PAGES = 3
DB_FILE = "processed_incruit_ids.txt"
DB_KEEP = 1000            # 처리한 공고 ID 보관 개수(최신순)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ko-KR,ko;q=0.9',
    'Referer': 'https://job.incruit.com/',
}


def _set_qs(url, key, value):
    """URL 쿼리의 key 값을 바꾸거나(있으면) 덧붙인다(없으면). 나머지 인코딩은 그대로 둔다."""
    pat = re.compile(r"([?&])%s=[^&]*" % re.escape(key))
    if pat.search(url):
        return pat.sub(lambda m: "%s%s=%s" % (m.group(1), key, value), url, count=1)
    return url + ("&" if "?" in url else "?") + "%s=%s" % (key, value)


def page_url(page):
    u = _set_qs(TARGET_URL, "page", page)
    u = _set_qs(u, "sortfield", "reg")
    return _set_qs(u, "sortorder", 1)


# ─────────────────────────────────────────────────────────────
# 파싱 (선택자가 바뀌어도 버티도록 대체 경로를 함께 둠)
# ─────────────────────────────────────────────────────────────
NO_RESULT_MARKERS = ("검색결과가 없습니다",)
SITE_MARKERS = ("c_row", "jobpost_list", "cBbslist")
BLOCK_MARKERS = ("captcha", "자동입력 방지", "비정상적인 접근", "접근이 차단", "access denied")
_JOBNO_RE = re.compile(r"[?&]job=(\d{6,})")


def _text(el):
    return el.get_text(strip=True) if el is not None else ""


def parse_jobs(html_text):
    """반환: (공고 목록, 공고 행 수, 읽지 못한 행 수)"""
    soup = BeautifulSoup(html_text, 'html.parser')
    rows = soup.select('ul.c_row')
    job_list, bad = [], 0
    for item in rows:
        try:
            title_tag = (item.select_one('.cell_mid .cl_top a')
                         or item.select_one("a[href*='jobpost.asp']"))
            href = (title_tag.get('href') or '').strip() if title_tag is not None else ''
            job_id = (item.get('jobno') or '').strip()
            if not job_id:   # 대체: 상세 링크의 job=번호
                m = _JOBNO_RE.search(href)
                job_id = m.group(1) if m else ''
            title = _text(title_tag)
            company = _text(item.select_one('.cpname') or item.select_one('.cell_first .cl_top a'))
            if not (job_id and title and href):
                bad += 1
                continue
            link = urljoin("https://job.incruit.com/jobdb_list/searchjob.asp", href)

            # 지역 정보 추출 (cl_md 클래스 내부의 첫 번째 span)
            location = _text(item.select_one('.cl_md span')) or "지역미정"

            info_spans = item.select('.cell_last .cl_btm span')
            deadline = _text(info_spans[0]) if len(info_spans) > 0 else "마감정보 없음"
            reg_time = _text(info_spans[1]) if len(info_spans) > 1 else ""
            # 괄호 제거 (예: (8일전 등록) -> 8일전 등록)
            reg_time = reg_time.replace('(', '').replace(')', '')

            job_list.append({
                'id': job_id,
                'company': company or "(회사명 없음)",
                'location': location,
                'title': title,
                'link': link,
                'deadline': deadline or "마감정보 없음",
                'reg_time': reg_time
            })
        except Exception:
            bad += 1
    return job_list, len(rows), bad


def validate_page(status, text):
    """netkit 용 응답 검증(작업 스레드에서 호출). ('ok', (jobs, rows, bad)) | (종류, 사유)"""
    if status in (403, 429):
        return "blocked", "HTTP %d" % status
    if status != 200:
        return "bad", "HTTP %d" % status
    jobs, rows, bad = parse_jobs(text)
    if jobs:
        return "ok", (jobs, rows, bad)
    site = rows > 0 or any(m in text for m in SITE_MARKERS)
    if site and any(m in text for m in NO_RESULT_MARKERS):
        return "ok", ([], 0, 0)               # 조건에 맞는 공고가 정말 0건(빈 페이지: '결과 없음' 행은 세지 않음)
    if any(m in text.lower() for m in BLOCK_MARKERS):
        return "blocked", "차단/캡차 페이지로 보임"
    if site:
        return "empty", "공고 %d행 중 0건 파싱(구조 변경 의심)" % rows
    return "bad", "인크루트 목록 페이지가 아님(%dB)" % len(text)


def collect_jobs(net, state, processed):
    """최근 등록순으로 페이지를 넘기며 신규 공고를 모은다.
    - 받은 페이지에 '이미 알린 공고'가 하나라도 있으면 그 뒤는 예전 공고이므로 멈춤
    - 첫 실행(기록 없음)은 1페이지만(알림 폭주 방지)
    - 뒤 페이지 수집이 실패하면 다음 실행에서 MAX_PAGES 까지 다시 훑음(deep_scan)
    반환: (공고 목록(최신순), 공고 행 수 합, 읽지 못한 행 수 합)"""
    deep = bool(state.get("deep_scan"))
    pages = 1 if not processed else MAX_PAGES
    out, seen, rows_sum, bad_sum = [], set(), 0, 0
    complete = True
    for page in range(1, pages + 1):
        try:
            (jobs, rows, bad), _via = net.get(page_url(page), validate_page, budget=90 if page == 1 else 45)
        except FetchError:
            if page == 1:
                raise
            print(f"{page}페이지 수집 실패 → 앞 페이지 결과로 진행(다음 실행에서 더 깊이 확인)")
            complete = False
            break
        rows_sum += rows
        bad_sum += bad
        for j in jobs:
            if j['id'] not in seen:
                seen.add(j['id'])
                out.append(j)
        if not jobs:
            break
        if not deep and any(j['id'] in processed for j in jobs):
            break
        if page < pages:
            print(f"{page}페이지가 모두 신규 → 다음 페이지 확인")
    if not DRY_RUN:
        if not complete:
            state["deep_scan"] = True
        elif deep:
            state.pop("deep_scan", None)
    return out, rows_sum, bad_sum


def handle_transport(state, transport):
    """연결 방식(직접/프록시) 변화를 기록만 한다.
    프록시 우회는 정상 동작의 일부 - 텔레그램 알림 없음(2026-07 사용자 요청: 프록시 알림 전면 중단).
    state 에는 종류('direct'/'proxy')만 저장한다. 어느 프록시였는지는 [net] 로그에 남고,
    바뀌는 주소까지 저장하면 실행마다 bot_state.json 이 달라져 불필요한 커밋이 쌓인다."""
    if not transport:
        return
    kind = "direct" if transport == "direct" else "proxy"
    prev = "direct" if str(state.get("transport", "direct")) == "direct" else "proxy"
    if prev != kind:
        print(f"연결 방식 변경: {prev} -> {kind} (알림 없음)")
    state["transport"] = kind


# ─────────────────────────────────────────────────────────────
# 텔레그램 (실패한 공고는 '처리됨'으로 기록하지 않음 → 다음 실행에서 재시도)
# ─────────────────────────────────────────────────────────────
class TelegramAuthError(Exception):
    """봇 토큰/수신자 설정 오류(401/404 등). 이번 실행의 전송을 중단한다."""


def _chat_ids():
    # TG_CHAT_ID 에 콤마/공백으로 여러 명을 넣을 수 있습니다. (예: "8755814064,8467039744")
    return [c for c in re.split(r"[,\s]+", TG_CHAT_ID or "") if c]


def _tg_post(cid, text, use_html):
    data = {"chat_id": cid, "text": text, "disable_web_page_preview": True}
    if use_html:
        data["parse_mode"] = "HTML"
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=(5, 20))
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body


def _to_plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def tg_send(cid, text, use_html=True, tries=4):
    """반환: 'ok' | 'permanent'(이 수신자에겐 재시도 무의미) | 'transient'(다음 실행에서 재시도)"""
    for attempt in range(1, tries + 1):
        try:
            code, body = _tg_post(cid, text, use_html)
        except requests.RequestException as e:
            print(f"텔레그램 네트워크 오류({e.__class__.__name__}) {attempt}/{tries}")
            time.sleep(min(2 * attempt, 6))
            continue
        if code == 200 and body.get("ok"):
            return "ok"
        desc = str(body.get("description", ""))[:200]
        low = desc.lower()
        if code in (401, 404):
            raise TelegramAuthError(desc or f"HTTP {code}")
        if code == 429:
            try:
                wait = float((body.get("parameters") or {}).get("retry_after", 3))
            except (TypeError, ValueError):
                wait = 3.0
            print(f"텔레그램 429(전송 한도) → {wait:.0f}초 대기")
            time.sleep(min(wait + 0.5, 60))
            continue
        if code == 403 or any(k in low for k in ("chat not found", "bot was blocked", "user is deactivated",
                                                   "peer_id_invalid", "bot can't initiate")):
            print(f"전송 실패(영구, chat_id={cid}): {desc}")
            return "permanent"
        if code == 400:
            if use_html:
                print(f"텔레그램 400(형식) → 일반 텍스트로 재전송: {desc}")
                use_html, text = False, _to_plain(text)
                continue
            print(f"전송 실패(400, chat_id={cid}): {desc}")
            return "permanent"
        print(f"전송 실패 (chat_id={cid}) HTTP {code} {attempt}/{tries}: {desc}")
        time.sleep(min(2 * attempt, 6))
    return "transient"


def send_telegram(msg):
    """모든 수신자에게 전송. True = 이 공고를 '처리됨'으로 기록해도 됨(모두 성공 또는 영구 실패)."""
    ids = _chat_ids()
    if not ids:
        raise TelegramAuthError("TG_CHAT_ID 가 비어 있습니다")
    results = []
    for i, cid in enumerate(ids):
        results.append(tg_send(cid, msg))
        if i < len(ids) - 1:
            time.sleep(0.3)
    return all(r in ("ok", "permanent") for r in results)


def build_message(job):
    # 공고명/회사명에 <, >, & 가 있어도 메시지가 깨지지 않도록 이스케이프
    c = html.escape(job['company'])
    t = html.escape(job['title'])
    loc = html.escape(job['location'])
    lk = html.escape(job['link'])
    dl = html.escape(job['deadline'])
    rt = html.escape(job['reg_time'])
    # 재원님이 요청하신 메시지 양식
    return (
        f"<b>{c} - {t}</b>\n\n"
        f"• {c}({loc})\n"
        f"• <a href='{lk}'><b>{t}</b></a>\n"
        f"• {dl}\n\n"
        f"본 공고는 {rt}됐어요"
    )


# ─────────────────────────────────────────────────────────────
# 상태(bot_state.json) + 오류 분류 노티 + 24시간 무신규 하트비트
# ─────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
STATE_FILE = "bot_state.json"
HEARTBEAT_HOURS = 24       # 이 시간 동안 새 공고가 없으면 '신규 없음' 노티
ERROR_DEDUP_HOURS = 12     # 같은 유형 오류는 이 시간에 한 번만 노티(스팸 방지)
BOT_NAME = "인크루트봇"
# 전송 시간 상한: 워크플로 timeout-minutes(10분) 안에 기록 저장·커밋까지 끝내도록,
# 실행 시작 후 이 시간이 지나면 새 전송을 시작하지 않고 남은 공고는 다음 실행으로 넘긴다.
SEND_BUDGET_SEC = 360
MAX_SEND_FAILS = 2         # 연속으로 이만큼 전송 실패(텔레그램 장애 의심) → 남은 공고는 다음 실행에서


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


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save_state(state):
    try:
        _atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as e:
        print("state 저장 실패:", e)


def load_processed():
    if not os.path.exists(DB_FILE):
        return []
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f.read().splitlines() if ln.strip()]


def save_processed(ids, original):
    ids = ids[:DB_KEEP]
    if ids == original[:DB_KEEP] and os.path.exists(DB_FILE):
        return   # 변화 없으면 파일을 건드리지 않음(불필요한 커밋 방지)
    try:
        _atomic_write(DB_FILE, "\n".join(ids))
    except Exception as e:
        print("처리 기록 저장 실패:", e)


def send_plain(text):
    """모든 chat_id 에 일반텍스트 전송(오류/하트비트 노티용)."""
    if DRY_RUN:
        print("[DRY-RUN] 노티(전송 안 함):\n" + text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for cid in _chat_ids():
        try:
            requests.post(url, data={"chat_id": cid, "text": text[:3900],
                                     "disable_web_page_preview": True}, timeout=10)
        except Exception as e:
            print("노티 전송 실패:", cid, e)


# ── 구글 시트 기록(Apps Script 웹앱으로 POST) ──
SHEET_WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL", "").strip()


def log_to_sheet(payload):
    """새 공고 1건을 구글 시트에 기록. 실패해도 알림/봇 동작에는 영향 없음."""
    if not SHEET_WEBHOOK_URL or DRY_RUN:
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
    # 크리티컬(구조 변경, 미분류)만 전송. 나머지는 로그만 - 하트비트가 장기 장애를 잡아줌(2026-07 사용자 요청).
    # (로그만 남기는 오류는 state 도 건드리지 않음 → 장애 중 실행마다 bot_state.json 이 바뀌어 커밋이 쌓이지 않게)
    if info["key"] not in ("structure", "unknown"):
        print(f"[{info['key']}] 비크리티컬 오류 -> 텔레그램 생략(로그만): {summary}")
        return
    consec = state.setdefault("consec_err", {})
    cnt = consec.get(info["key"], 0) + 1
    consec[info["key"]] = cnt
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

# (일일 리포트 기능은 2026-08-05 사용자 요청으로 삭제 — 하트비트만 유지)


def main():
    t_start = time.monotonic()
    state = load_state()
    # 일일 리포트 폐지에 따른 상태키 정리(1회성, 없으면 no-op)
    for _k in ("sent_log", "report_initialized", "last_report_date"):
        state.pop(_k, None)
    processed = load_processed()
    original = list(processed)
    processed_set = set(processed)
    # 인크루트 목록은 한 페이지가 크므로(약 270KB) 직접 연결에 4초 먼저 기회를 줌
    net = Net(state, headers=HEADERS, encoding="cp949", log=print, site="인크루트", head_start=4.0)
    sheet_rows = []
    exit_code = 0
    if DRY_RUN:
        print("[DRY-RUN] 텔레그램 전송·시트 기록·파일 저장 없이 미리보기만 합니다.")
    try:
        jobs, rows, bad = collect_jobs(net, state, processed_set)
        handle_transport(state, net.label)   # 연결 방식 기록(알림 없음)

        new_jobs = [j for j in jobs if j['id'] not in processed_set]
        sent = failed = fail_streak = 0
        todo = list(reversed(new_jobs))       # 오래된 것부터 전송
        for idx, job in enumerate(todo):
            message = build_message(job)
            if DRY_RUN:
                print("[DRY-RUN] 전송 예정 ─────────────\n" + message)
                continue
            if time.monotonic() - t_start > SEND_BUDGET_SEC:
                print(f"실행 {SEND_BUDGET_SEC}초 초과 → 남은 {len(todo) - idx}건은 다음 실행에서 전송")
                break
            if fail_streak >= MAX_SEND_FAILS:
                print(f"전송 연속 {fail_streak}회 실패(텔레그램 장애 의심) → 남은 {len(todo) - idx}건은 다음 실행에서 전송")
                break
            if send_telegram(message):
                processed.insert(0, job['id'])
                processed_set.add(job['id'])
                # 보내자마자 기록: 실행이 도중에 끊겨도(시간 초과 등) 이미 보낸 공고를 다시 보내지 않음
                save_processed(processed, original)
                sent += 1
                fail_streak = 0
                sheet_rows.append({
                    "bot": "incruit",
                    "scraped_at": _now_iso(),
                    "company": job['company'],
                    "region": job['location'],
                    "title": job['title'],
                    "link": job['link'],
                    "deadline": job['deadline'],
                    "extra": job['reg_time'],   # 남은 속성(등록시점)
                })
            else:
                failed += 1
                fail_streak += 1
                print(f"전송 실패(일시적) → 다음 실행에서 재시도: {job['id']}")
            time.sleep(1.0)
        later = 0 if DRY_RUN else len(new_jobs) - sent - failed
        print(f"완료: 수집완료 {len(jobs)}건, 신규후보 {len(new_jobs)}건 중 {sent}건 전송"
              + (f", {failed}건 재시도 예정" if failed else "")
              + (f", {later}건 다음 실행으로" if later else ""))

        state["consec_err"] = {}   # 성공 → 연속 오류 카운터 리셋
        if rows and bad * 2 >= rows:   # 절반 이상 못 읽음 → 부분적인 구조 변경 의심
            notify_error(state, f"공고 {rows}행 중 {bad}행 파싱 실패 - 사이트 구조 변경 의심(읽은 공고만 알림)")

        # 활동 기록 / 24시간 무신규 하트비트
        if sent > 0:
            state["last_activity_at"] = _now_iso()
        elif not DRY_RUN:
            maybe_heartbeat(state, len(jobs), len(new_jobs))
    except TelegramAuthError as e:
        print(f"텔레그램 설정 오류(봇 토큰/chat_id 확인 필요): {e}")
        exit_code = 1
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        notify_error(state, tb)   # 분류된 쉬운 설명으로 노티(원문은 로그에만)
        if not DRY_RUN:
            # 접속 장애처럼 '로그만 남기는' 오류가 며칠 이어져도 무소식이 되지 않도록,
            # 실패한 실행에서도 24시간 하트비트(기존 '신규 없음' 노티)를 확인한다.
            maybe_heartbeat(state, 0, 0)
    finally:
        net.finalize()
        print(net.summary())
        if not DRY_RUN:
            save_processed(processed, original)
            save_state(state)
            for p in sheet_rows:      # 시트 기록은 알림을 모두 보낸 뒤에(알림 지연 방지)
                log_to_sheet(p)
        print(f"실행 시간 {time.monotonic() - t_start:.1f}초")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
