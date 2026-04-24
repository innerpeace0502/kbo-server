import sys
sys.path.insert(0, '.')
from app import get_live_scores
import json

scores = get_live_scores()
print(json.dumps(scores, ensure_ascii=False, indent=2))