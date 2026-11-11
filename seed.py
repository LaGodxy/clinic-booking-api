"""Load three future demo slots into a fresh deployment."""
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

tomorrow = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
tomorrow += timedelta(days=1)
slots = []
for index in range(3):
    start = tomorrow + timedelta(hours=index)
    end = start + timedelta(minutes=30)
    slots.append(dict(id=f"s{index + 1}", provider="Dr Ade",
                      start=start.isoformat(), end=end.isoformat()))

base = os.environ.get("BOOKING_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
request = Request(base + "/reset", data=json.dumps({"slots": slots}).encode(),
                  headers={"Content-Type": "application/json",
                           "X-API-Key": os.environ["BOOKING_API_KEY"]})
with urlopen(request, timeout=15) as response:
    print(response.read().decode())
