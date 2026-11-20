#!/usr/local/bin/python
"""Local HTTP liveness only; never infer cloud, backend, or Tunnel readiness."""
import json
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

try:
    with build_opener(ProxyHandler({})).open('http://127.0.0.1:8642/api/health', timeout=2) as response:
        payload = json.loads(response.read(4097))
        if response.status != 200 or payload != {'ok': True, 'status': 'ok'} or payload['ok'] is not True:
            raise SystemExit(1)
except (OSError, URLError, TimeoutError, ValueError, KeyError, TypeError):
    raise SystemExit(1)
