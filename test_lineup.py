# test_lineup.py
# -*- coding: utf-8 -*-
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time, re, requests
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
now = datetime.now(KST)
today = (now - timedelta(days=1)).strftime('%Y%m%d') if now.hour < 4 else now.strftime('%Y%m%d')
print(f'조회 날짜: {today}')

STADIUMS = ['잠실','문학','광주','고척','대전','수원','사직','창원','대구','인천','청주']

options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--disable-gpu')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
try:
    url = f'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}'
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    time.sleep(3)
    body = driver.find_element(By.TAG_NAME, 'body').text
    lines = [l.strip() for l in body.split('\n') if l.strip()]
    print(f'전체 라인 수: {len(lines)}')

    # 광주 경기만 집중 출력
    for i, line in enumerate(lines):
        if '광주' in line:
            print(f'\n=== 광주 발견 i={i} ===')
            for j in range(i, min(i+15, len(lines))):
                print(f'  [{j}] (i+{j-i}): {lines[j]}')
            break

    # 전체 라인 출력
    print('\n=== 전체 라인 ===')
    for i, line in enumerate(lines):
        print(f'{i:3d}: {line}')

    # 스코어 파싱 직접 시뮬레이션
    print('\n=== 스코어 파싱 시뮬레이션 ===')
    stadium_map = {'잠실':('NC','LG'),'문학':('롯데','SSG'),'대구':('한화','삼성'),'광주':('KT','KIA'),'고척':('두산','키움')}
    i = 0
    while i < len(lines):
        line = lines[i]
        stadium_match = next((s for s in STADIUMS if line.startswith(s)), None)
        if not (stadium_match and re.search(r'\d{2}:\d{2}', line)):
            i += 1; continue
        teams = stadium_map.get(stadium_match)
        status_line = lines[i+2] if i+2 < len(lines) else ''
        print(f'[i={i}] {stadium_match} teams={teams} status={status_line}')
        if '경기종료' in status_line:
            vs_idx = None
            for k in range(i+3, min(i+12, len(lines))):
                if lines[k].strip().upper() == 'VS':
                    vs_idx = k; break
            if vs_idx:
                away_score = lines[i+3]
                home_score = lines[vs_idx+1] if vs_idx+1 < len(lines) else ''
                print(f'  → away_score={away_score} home_score={home_score} isdigit={away_score.isdigit()}/{home_score.isdigit()}')
        i += 1
finally:
    driver.quit()