# test_lineup.py
# -*- coding: utf-8 -*-
import requests, re

headers = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.koreabaseball.com/',
    'X-Requested-With': 'XMLHttpRequest',
    'Content-Type': 'application/x-www-form-urlencoded'
}

team = 'KIA'
results_by_month = {}

for m in ['05', '04']:
    data = {
        'leId': '1', 'srIdList': '0,9',
        'seasonId': '2026', 'year': '2026',
        'month': m, 'gameMonth': m, 'teamId': ''
    }
    res = requests.post(
        'https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList',
        headers=headers, data=data, timeout=10
    )
    rows = res.json().get('rows', [])
    print(f'\n{m}월: {len(rows)}개 row')

    results = []
    for row_obj in rows:
        row = row_obj.get('row', [])
        cells = [re.sub(r'<[^>]+>', '', c.get('Text', '')).strip() for c in row]
        cells = [c for c in cells if c]

        game_cell = next((c for c in cells
                         if 'vs' in c.lower() and team in c
                         and re.search(r'\d', c)), None)
        if not game_cell:
            continue

        m2 = re.search(r'(.+?)(\d+)vs(\d+)(.+)', game_cell)
        if not m2:
            print(f'  점수없음: {game_cell}')
            continue

        team1  = m2.group(1).strip()
        score1 = int(m2.group(2))
        score2 = int(m2.group(3))

        if team in team1:
            result = '승' if score1 > score2 else ('패' if score1 < score2 else '무')
        else:
            result = '승' if score2 > score1 else ('패' if score2 < score1 else '무')
        results.append(result)
        print(f'  {game_cell} → {result}')

    results_by_month[m] = results

all_results = results_by_month['05'] + results_by_month['04']
print(f'\n전체: {all_results}')
print(f'최근 10경기: {all_results[-10:]}')
print(f'승:{all_results.count("승")} 패:{all_results.count("패")} 무:{all_results.count("무")}')