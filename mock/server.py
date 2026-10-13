"""Tiny fake of Mt. SAC's Banner 9 registration pages, for testing register.py.

    python mock/server.py PORT OPEN_IN_SECONDS [FULL_CRNS] [load]

  FULL_CRNS  comma list of CRNs that are full (only Waitlist/Remove offered)
  load       simulate heavy traffic: random 503 pages, ignored clicks, and
             Submits that go through while the page never updates

CRN 99999 errors on Submit (time conflict); 00000 is an invalid CRN.
Registered classes are kept by the server, so they survive a reload.
"""
import http.server
import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
OPEN_AT_MS = int((time.time() + float(sys.argv[2] if len(sys.argv) > 2 else 0)) * 1000)
FULL = sys.argv[3] if len(sys.argv) > 3 else ""
LOAD = len(sys.argv) > 4 and sys.argv[4] == "load"
REGISTERED = set()
ROUTES = {
    "/StudentRegistrationSsb/ssb/registration": "registration.html",
    "/StudentRegistrationSsb/ssb/term/termSelection": "termSelection.html",
    "/StudentRegistrationSsb/ssb/classRegistration/classRegistration": "classRegistration.html",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def send(self, code, body, ctype="text/html"):
        body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/mock/state":
            return self.send(200, json.dumps(sorted(REGISTERED)), "application/json")
        name = ROUTES.get(path)
        if not name:
            return self.send(404, "not found")
        if LOAD and random.random() < 0.25:
            time.sleep(random.uniform(0, 3))
            return self.send(503, "<h1>Service Unavailable</h1>")
        html = ((HERE / name).read_text()
                .replace("__OPEN_AT__", str(OPEN_AT_MS)).replace("__FULL__", FULL)
                .replace("__LOAD__", "true" if LOAD else "false")
                .replace("__REGISTERED__", json.dumps(sorted(REGISTERED))))
        self.send(200, html)

    def do_POST(self):  # /mock/register?crn=..  /mock/drop?crn=..
        path, _, q = self.path.partition("?")
        crn = q.split("=")[-1]
        if path == "/mock/register":
            REGISTERED.add(crn)
        elif path == "/mock/drop":
            REGISTERED.discard(crn)
        self.send(200, "{}", "application/json")

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
