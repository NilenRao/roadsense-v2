"""`python -m roadsense.api [--port 8000] [--forecast-as-of ISO]` serves the API and the officer page."""
import argparse
import os

import uvicorn

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8000)
ap.add_argument("--forecast-as-of", default=None, help="pretend the forecast was retrieved then (STALE demo)")
a = ap.parse_args()
if a.forecast_as_of:
    os.environ["ROADSENSE_FORECAST_AS_OF"] = a.forecast_as_of
print(f"RoadSense on http://{a.host}:{a.port}")
uvicorn.run("roadsense.api.main:app", host=a.host, port=a.port)
