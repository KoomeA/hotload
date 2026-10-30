"""A stand-in for the Paystack API, for the browser end-to-end test only (never for production).

It implements just what Hotload calls: POST /transaction/initialize, GET /transaction/verify/<ref>, plus a
tiny hosted checkout page. It is NOT a faithful copy of Paystack; real behaviour must be confirmed in test mode.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PAYMENTS = {}
KEY = "sk_test_e2e"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        return self.headers.get("authorization") == f"Bearer {KEY}"

    def do_POST(self):
        if urlsplit(self.path).path != "/transaction/initialize":
            return self._json(404, {"status": False, "message": "not found"})
        if not self._authed():
            return self._json(401, {"status": False, "message": "Invalid key"})
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        ref = body["reference"]
        PAYMENTS[ref] = {"amount": body["amount"], "currency": body["currency"], "paid": False, "cb": body["callback_url"]}
        url = f"http://127.0.0.1:{self.server.server_port}/pay/{ref}"
        self._json(200, {"status": True, "message": "ok", "data": {"authorization_url": url, "access_code": "AC", "reference": ref}})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/transaction/verify/"):
            if not self._authed():
                return self._json(401, {"status": False, "message": "Invalid key"})
            p = PAYMENTS.get(path.rsplit("/", 1)[1])
            if not p:
                return self._json(404, {"status": False, "message": "Transaction reference not found"})
            return self._json(200, {"status": True, "data": {"status": "success" if p["paid"] else "abandoned",
                                                             "amount": p["amount"], "currency": p["currency"]}})
        if path.startswith("/pay/"):
            ref = path.rsplit("/", 1)[1]
            p = PAYMENTS.get(ref)
            html = f'<!doctype html><title>Fake Paystack</title><h1>Fake Paystack checkout</h1><p>{p["amount"]} {p["currency"]}</p>' \
                   f'<a id="pay" href="/confirm/{ref}">Pay now</a>'.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(html)))
            self.end_headers()
            return self.wfile.write(html)
        if path.startswith("/confirm/"):
            ref = path.rsplit("/", 1)[1]
            p = PAYMENTS[ref]
            p["paid"] = True
            sep = "&" if "?" in p["cb"] else "?"
            self.send_response(302)
            self.send_header("location", f"{p['cb']}{sep}trxref={ref}&reference={ref}")
            self.end_headers()
            return
        self._json(404, {"status": False, "message": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 9100), H).serve_forever()
