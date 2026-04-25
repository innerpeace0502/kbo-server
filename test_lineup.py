# test_lineup.py
import requests, re
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
today = datetime.now(KST).strftime('%Y%m%d')
year  = today[:4]
month = today[4:6]

headers = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.koreabaseball.com/',
    'X-Requested-With': 'XMLHttpRequest',
    'Content-Type': 'application/x-www-form-urlencoded'
}

team = 'LG'
results = []

for m in [month, f'{int(month)-1:02d}']:
    if int(m) < 1:
        continue
    data = {
        'leId': '1', 'srIdList': '0,9',
        'seasonId': year, 'year': year,
        'month': m, 'gameMonth': m, 'teamId': ''
    }
    res = requests.post(
        'https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList',
        headers=headers, data=data, timeout=10
    )
    rows = res.json().get('rows', [])
    print(f'{m}월: {len(rows)}개 row')

    for row_obj in rows:
        row = row_obj.get('row', [])
        cells = [re.sub(r'<[^>]+>', '', c.get('Text', '')).strip() for c in row]
        cells = [c for c in cells if c]

        # LG 포함 경기 찾기
        game_cell = next((c for c in cells if 'vs' in c.lower() and team in c), None)
        if not game_cell:
            continue

        print(f'  경기: {cells}')

        # 점수 파싱으로 승/패 판단
        # 예: 'KIA2vs7LG' → KIA:2, LG:7 → LG 승
        m2 = re.search(r'(\D+?)(\d+)vs(\d+)(\D+)', game_cell)
        if m2:
            team1     = m2.group(1).strip()
            score1    = int(m2.group(2))
            score2    = int(m2.group(3))
            team2     = m2.group(4).strip()

            if team in team1:
                result = '승' if score1 > score2 else ('패' if score1 < score2 else '무')
            else:
                result = '승' if score2 > score1 else ('패' if score2 < score1 else '무')
            results.append(result)
            print(f'    → {team1}({score1}) vs {team2}({score2}) → {team} {result}')

print(f'\n{team} 최근 결과: {results[-10:]}')