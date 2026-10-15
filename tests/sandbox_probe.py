import os
import socket
import urllib.request

failures = []


def expect_fail(name, fn):
    try:
        fn()
    except Exception:
        return
    failures.append(name)


expect_fail("shadow", lambda: open("/etc/shadow").read())
expect_fail("write_root", lambda: open("/owned-by-probe", "w").write("x"))


def reach_internet():
    urllib.request.urlopen("http://example.com", timeout=3).read()


expect_fail("internet", reach_internet)


def reach_loopback():
    socket.create_connection(("127.0.0.1", 18894), 2).close()


expect_fail("loopback", reach_loopback)

if "PROVIDER_KEY" in os.environ:
    failures.append("env")

keys = sorted(os.environ)
print("env_keys", ",".join(keys))
if failures:
    print("unexpected_success", ",".join(failures))
    raise SystemExit(1)
print("sandbox denials held")
