"""Run BreatheWise on your laptop: python3 local_server.py [port]

Passes each request to the same handler that runs on AWS Lambda.
`python3 local_server.py nightly` runs the evening job once and writes to outbox/.
`python3 local_server.py nightly 2025-11-18` does the same for a chosen date.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from app.handler import lambda_handler


class Handler(BaseHTTPRequestHandler):
    def _handle(self):
        url = urlsplit(self.path)
        length = int(self.headers.get("content-length") or 0)
        event = {
            "requestContext": {"http": {"method": self.command}},
            "rawPath": url.path,
            "queryStringParameters": dict(parse_qsl(url.query)),
            "body": self.rfile.read(length).decode("utf-8", "replace") if length else "",
        }
        result = lambda_handler(event)
        body = result["body"].encode("utf-8")
        self.send_response(result["statusCode"])
        for name, value in result["headers"].items():
            self.send_header(name, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _handle


if __name__ == "__main__":
    if sys.argv[1:2] == ["nightly"]:
        event = {"date": sys.argv[2]} if len(sys.argv) > 2 else {}
        print(json.dumps(lambda_handler(event), indent=2))
    else:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
        print(f"BreatheWise is running at http://127.0.0.1:{port}")
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
