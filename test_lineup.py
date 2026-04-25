import requests, json

headers = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.koreabaseball.com/',
    'X-Requested-With': 'XMLHttpRequest',
    'Content-Type': 'application/x-www-form-urlencoded'
}

game_id = '20260424LGOB0'
year = '2026'

url = 'https://www.koreabaseball.com/ws/Schedule.asmx/GetBoxScoreScroll'
data = {'gameId': game_id, 'leId': '1', 'srId': '0', 'seasonId': year}
r = requests.post(url, headers=headers, data=data, timeout=10)
result = r.json()

# 투수 파싱
print('=== 투수 정보 ===')
for i, team in enumerate(result['arrPitcher']):
    table = json.loads(team['table'])
    rows = table['rows']
    team_label = '원정' if i == 0 else '홈'
    print(f'\n[{team_label}팀]')
    for row in rows:
        cells = row['row']
        name   = cells[0]['Text']
        timing = cells[1]['Text']
        result_text = cells[2]['Text'].replace('&nbsp;', '')
        print(f'  {name} ({timing}) {result_text}')

# 라인업 파싱
print('\n=== 라인업 ===')
for i, team in enumerate(result['arrHitter']):
    table = json.loads(team['table1'])
    rows = table['rows']
    team_label = '원정' if i == 0 else '홈'
    print(f'\n[{team_label}팀]')
    seen = set()
    for row in rows:
        cells = row['row']
        order = cells[0]['Text']
        pos   = cells[1]['Text']
        name  = cells[2]['Text']
        if order not in seen:
            seen.add(order)
            print(f'  {order}번 {pos} {name}')