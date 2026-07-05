import requests
from bs4 import BeautifulSoup
import os
import re
import html
import time

# 1. 설정
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
TARGET_URL = "https://job.incruit.com/entry/?jobty=4&jobty=1&group1=7&compty=4&compty=10&scale=2&scale=5&scale=3&group1=17&group1=5&group1=4&group1=1&group1=3&schol=60&occ1=200&occ1=102&rgn2=18&rgn2=14&rgn2=11"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Referer': 'https://job.incruit.com/',
}

def get_jobs():
    for attempt in range(5):
        try:
            print(f"{attempt + 1}번째 인크루트 접속 시도...")
            res = requests.get(TARGET_URL, headers=HEADERS, timeout=30)
            res.encoding = 'euc-kr' 
            
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                job_list = []
                items = soup.select('ul.c_row')
                
                for item in items:
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
        except: pass
        time.sleep(10)
    return []

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

if __name__ == "__main__":
    jobs = get_jobs()
    db_file = "processed_incruit_ids.txt"
    processed_ids = open(db_file, "r").read().splitlines() if os.path.exists(db_file) else []

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
            time.sleep(1.2)

    with open(db_file, "w") as f:
        f.write("\n".join((new_id_list + processed_ids)[:200]))
