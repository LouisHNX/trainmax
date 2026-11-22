import base64
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

os.environ["WEB_PASSWORD"] = "pw"
import web  # noqa: E402


def call(port, method, path, body=None, password="pw"):
    c = HTTPConnection("127.0.0.1", port)
    auth = base64.b64encode(f":{password}".encode()).decode()
    c.request(method, path, json.dumps(body) if body else None, {"Authorization": f"Basic {auth}"})
    r = c.getresponse()
    return r.status, r.read()


class WebTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(self.dir.name, "t.db")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()

    def test_auth_required(self):
        self.assertEqual(call(self.port, "GET", "/api/routes", password="bad")[0], 401)

    def test_add_list_del(self):
        self.assertEqual(call(self.port, "POST", "/api/command", {"text": "/add paris lyon jeudi 08:00-12:00"})[0], 200)
        routes = json.loads(call(self.port, "GET", "/api/routes")[1])["routes"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(call(self.port, "POST", "/api/command", {"text": f"/del {routes[0]['id']}"})[0], 200)
        self.assertEqual(json.loads(call(self.port, "GET", "/api/routes")[1])["routes"], [])

    def test_bad_and_forbidden_commands(self):
        self.assertEqual(call(self.port, "POST", "/api/command", {"text": "/add xx yy 15/11"})[0], 400)
        self.assertEqual(call(self.port, "POST", "/api/command", {"text": "/now"})[0], 400)


if __name__ == "__main__":
    unittest.main()
