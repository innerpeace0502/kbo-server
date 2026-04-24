from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from datetime import datetime
import re, time

today = datetime.now().strftime('%Y%m%d')

options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--disable-gpu')
options.add_argument('user-agent=Mozilla/5.0')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

try:
    url = f'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}'
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    time.sleep(3)

    body = driver.find_element(By.TAG_NAME, 'body').text
    lines = [l.strip() for l in body.split('\n') if l.strip()]

    # 줄 번호와 함께 출력
    for i, line in enumerate(lines):
        print(f'{i:3d}: {line}')
finally:
    driver.quit()