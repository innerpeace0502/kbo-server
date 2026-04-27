# test_lineup.py
import requests, re

headers = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.koreabaseball.com/',
    'X-Requested-With': 'XMLHttpRequest',
    'Content-Type': 'application/x-www-form-urlencoded'
}

from datetime import datetime, timezone, timedelta
KST = timezone(timedelta(hours=9))
today = datetime.now(KST).strftime('%Y%m%d')
year = today[:4]
month = today[4:6]

data = {
    'leId': '1', 'srIdList': '0,9',
    'seasonId': year, 'year': year,
    'month': month, 'gameMonth': month, 'teamId': ''
}
res = requests.post(
    'https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList',
    headers=headers, data=data, timeout=10
)
result = res.json()
target_date = f"{month}.{today[6:8]}"
current_date = ''

KBO_TEAMS = ["LG", "KT", "SSG", "NC", "두산", "KIA", "롯데", "삼성", "한화", "키움"]
STAD_LIST = ['잠실','수원','창원','대구','광주','인천','문학','대전','사직','고척','청주']

for row_obj in result.get('rows', []):
    row = row_obj.get('row', [])
    for cell in row:
        if cell.get('Class') == 'day':
            current_date = re.sub(r'<[^>]+>', '', cell.get('Text','')).strip()[:5]
    if target_date not in current_date:
        continue
    play_cell = next((c for c in row if c.get('Class') == 'play'), None)
    if not play_cell:
        continue
    play_text = play_cell.get('Text', '')
    teams = re.findall(r'<span(?:[^>]*)>(.*?)</span>', play_text)
    teams = [t for t in teams if t and 'vs' not in t.lower()]
    away = next((t for t in KBO_TEAMS if t in teams[0]), None) if teams else None
    home = next((t for t in KBO_TEAMS if t in teams[-1]), None) if len(teams) > 1 else None

    stadium = ''
    for cell in row:
        cell_text = re.sub(r'<[^>]+>', '', cell.get('Text', '')).strip()
        for s in STAD_LIST:
            if s in cell_text:
                stadium = s
                break

    print(f'구장:{stadium} away:{away} home:{home}')