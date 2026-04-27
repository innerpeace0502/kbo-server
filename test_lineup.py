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

options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--disable-gpu')

STADIUMS = ['잠실','문학','광주','고척','대전','수원','사직','창원','대구','인천','청주']
KBO_TEAMS = ["LG","KT","SSG","NC","두산","KIA","롯데","삼성","한화","키움"]

def get_stadium_map(today):
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.koreabaseball.com/',
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    year = today[:4]; month = today[4:6]
    data = {'leId':'1','srIdList':'0,9','seasonId':year,'year':year,
            'month':month,'gameMonth':month,'teamId':''}
    res = requests.post('https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList',
                       headers=headers, data=data, timeout=10)
    result = res.json()
    target_date = f"{month}.{today[6:8]}"
    current_date = ''
    stadium_map = {}
    STAD_LIST = ['잠실','수원','창원','대구','광주','인천','문학','대전','사직','고척','청주']
    for row_obj in result.get('rows', []):
        row = row_obj.get('row', [])
        for cell in row:
            if cell.get('Class') == 'day':
                txt = re.sub(r'<[^>]+>','',cell.get('Text','')).strip()[:5]
                if txt: current_date = txt
        if target_date not in current_date: continue
        play_cell = next((c for c in row if c.get('Class') == 'play'), None)
        if not play_cell: continue
        play_text = play_cell.get('Text','')
        teams = re.findall(r'<span(?:[^>]*)>(.*?)</span>', play_text)
        teams = [t for t in teams if t and 'vs' not in t.lower()]
        away = next((t for t in KBO_TEAMS if t in teams[0]), None) if teams else None
        home = next((t for t in KBO_TEAMS if t in teams[-1]), None) if len(teams)>1 else None
        if not away or not home: continue
        for cell in row:
            cell_text = re.sub(r'<[^>]+>','',cell.get('Text','')).strip()
            for s in STAD_LIST:
                if s in cell_text:
                    stadium_map[s] = (away, home)
                    break
        play_clean = re.sub(r'<[^>]+>','',play_text)
        for s in STAD_LIST:
            if s in play_clean and s not in stadium_map:
                stadium_map[s] = (away, home)
    return stadium_map

today = '20260426'
stadium_map = get_stadium_map(today)
print(f'구장맵: {stadium_map}')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
scores = []
try:
    url = f'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}'
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    time.sleep(3)
    body = driver.find_element(By.TAG_NAME, 'body').text
    lines = [l.strip() for l in body.split('\n') if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]
        stadium_match = next((s for s in STADIUMS if line.startswith(s)), None)
        if not (stadium_match and re.search(r'\d{2}:\d{2}', line)):
            i += 1; continue

        teams = stadium_map.get(stadium_match)
        print(f'\n[i={i}] 구장={stadium_match} teams={teams}')

        if i+2 >= len(lines):
            i += 1; continue

        status_line = lines[i+2]
        print(f'  status={status_line}')

        if '경기종료' in status_line:
            vs_idx = None
            for k in range(i+3, min(i+12, len(lines))):
                print(f'    lines[{k}]={lines[k]}')
                if lines[k].strip().upper() == 'VS':
                    vs_idx = k; break
            print(f'  vs_idx={vs_idx}')
            if vs_idx:
                away_score = lines[i+3]
                home_score = lines[vs_idx+1] if vs_idx+1 < len(lines) else ''
                print(f'  away_score={away_score} home_score={home_score}')
                print(f'  isdigit: away={away_score.isdigit()} home={home_score.isdigit()}')
                if away_score.isdigit() and home_score.isdigit() and teams:
                    scores.append({'away':teams[0],'home':teams[1],
                                   'away_score':away_score,'home_score':home_score,'status':'2'})
                    print(f'  ✅ 추가됨!')
        i += 1

finally:
    driver.quit()

print(f'\n최종 스코어: {scores}')