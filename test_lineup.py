import requests, json

headers = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.koreabaseball.com/',
    'X-Requested-With': 'XMLHttpRequest',
    'Content-Type': 'application/x-www-form-urlencoded'
}

# 어제 경기 (경기 완료)
game_id = '20260424LGOB0'
year = '2026'

url = 'https://www.koreabaseball.com/ws/Schedule.asmx/GetBoxScoreScroll'
data = {'gameId': game_id, 'leId': '1', 'srId': '0', 'seasonId': year}
r = requests.post(url, headers=headers, data=data, timeout=10)
result = r.json()
print('code:', result.get('code'))
print('msg:', result.get('msg'))
print('arrPitcher 수:', len(result.get('arrPitcher', [])))
print('arrHitter 수:', len(result.get('arrHitter', [])))