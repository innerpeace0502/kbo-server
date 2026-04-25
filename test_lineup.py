# test_lineup.py
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time, re

options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--disable-gpu')
options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
try:
    # 내일 경기 미리 찾기 - Livesport KBO 페이지
    url = 'https://www.livesport.com/kr/baseball/south-korea/kbo/'
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    time.sleep(4)

    # 경기 링크 찾기
    links = driver.find_elements(By.TAG_NAME, 'a')
    game_links = [l.get_attribute('href') for l in links 
                  if l.get_attribute('href') and 'kbo' in str(l.get_attribute('href'))
                  and any(x in str(l.get_attribute('href')) for x in ['summary', 'lineup', 'match'])]
    print('경기 링크들:')
    for link in game_links[:10]:
        print(f'  {link}')

finally:
    driver.quit()