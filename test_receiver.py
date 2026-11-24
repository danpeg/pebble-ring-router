#!/usr/bin/env python3
"""End-to-end test of the ring receiver. It uses a temporary home and a test secret, never the real ones.

  python3 test_receiver.py                 signature, replay, and limit checks (no speech model necessary)
  python3 test_receiver.py --speech MODEL  also makes a spoken sample with `say` and checks the transcript
"""
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SPEECH = sys.argv[sys.argv.index("--speech") + 1] if "--speech" in sys.argv else ""
SENTENCE = "Orchestrator, add a task to the tracker. Check the release notes for the firmware."


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def post(port, key, parts, trigger="double-click-hold", ts=None, delivery=None, test=False, sign=True, tamper=False):
    boundary = uuid.uuid4().hex
    body = b""
    for name, (value, ctype, filename) in parts.items():
        disp = 'form-data; name="%s"' % name + ('; filename="%s"' % filename if filename else "")
        body += ("--%s\r\nContent-Disposition: %s\r\n" % (boundary, disp)).encode()
        if ctype:
            body += ("Content-Type: %s\r\n" % ctype).encode()
        body += b"\r\n" + value + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    ts = str(int(time.time()) if ts is None else ts)
    delivery = delivery or str(uuid.uuid4())
    headers = {"Content-Type": "multipart/form-data; boundary=" + boundary, "X-Index-Webhook-Version": "1",
               "X-Index-Trigger": trigger, "X-Index-Delivery": delivery}
    if test:
        headers["X-Index-Test"] = "true"
    if sign:
        prefix = "v1\n%s\n%s\n%s\n%s\n" % (ts, delivery, trigger, "1" if test else "0")
        headers["X-Index-Signature"] = hmac.new(key, prefix.encode() + body, hashlib.sha256).hexdigest()
        headers["X-Index-Timestamp"] = ts
    if tamper:
        body = body.replace(b"ring", b"rinG")
    req = urllib.request.Request("http://127.0.0.1:%d/ring" % port, data=body, headers=headers, method="POST")
    return request(req)


def request(req):
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def button(port, payload, method="POST"):
    body = json.dumps(payload).encode()
    return request(urllib.request.Request("http://127.0.0.1:%d/correct" % port, data=body,
                                         headers={"Content-Type": "application/json"}, method=method))


def correction_limits(port, key, check):
    sockets = []
    started = time.monotonic()
    try:
        for _ in range(4):
            conn = socket.create_connection(("127.0.0.1", port), timeout=7)
            sockets.append(conn)
            conn.sendall(b"POST /correct HTTP/1.0\r\nContent-Length: 80\r\n\r\n{")
        time.sleep(0.2)
        check("fifth simultaneous correction gets 429", button(port, {}), (429, "correction failed"))
        check("normal ring delivery still works while correction slots are full",
              post(port, key, {"transcription": (b"limit test", "", "")}, test=True, trigger="test-event")[0], 200)
        for conn in sockets:
            with conn.makefile("rb") as response:
                check("slow correction body returns 408", response.readline().split()[1:2], [b"408"])
        check("the four reads end at the five-second deadline", 4.6 <= time.monotonic() - started < 7, True)
        check("correction slots recover after timeout", button(port, {}), (400, "correction failed"))
    finally:
        for conn in sockets:
            conn.close()


def correction_tests(home, key, port, did, check):
    receipts, muse = queue.Queue(), queue.Queue()
    model_gates = queue.Queue()

    class Fake(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/jev":
                gate = threading.Event()
                model_gates.put(gate)
                gate.wait(timeout=4)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"answers":{"route":{"choice":"orchestrator","confidence":0.9}}}')
                return
            target = muse if self.path == "/muse" else receipts
            target.put((self.headers, body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Fake) as fake:
        thread = threading.Thread(target=fake.serve_forever, daemon=True)
        thread.start()
        try:
            root = Path(home)
            base = "http://127.0.0.1:%d" % fake.server_address[1]
            (root / "config.json").write_text(json.dumps({
                "receipt_url": base + "/receipt",
                "correct_url": "https://ring.invalid",
                "route_descriptions": {"muse": "", "orchestrator": "", "architect": "", "reviewer": ""},
            }))
            voice_hint = b'\nother, latest note only: say "that was for Reviewer"'
            (root / "muse-url").write_text(base + "/muse")
            (root / "muse-secret").write_text("fake-muse-correction-key")
            note = next((root / "inbox").glob("*-" + hashlib.sha256(did.encode()).hexdigest()[:8] + ".md"))

            def payload(name=note.name, route="architect", expires=None, from_route="orchestrator"):
                exp = int(time.time()) + 604800 if expires is None else expires
                data = ("correct|%s|%s|%s|%d" % (name, from_route, route, exp)).encode()
                return {"note": name, "from": from_route, "to": route, "exp": exp,
                        "sig": hmac.new(key, data, hashlib.sha256).hexdigest()}

            for reason, data, method, status in (
                ("signature", dict(payload(), sig="a" * 64), "POST", 401),
                ("unsigned unknown route", dict(payload(route="unknown"), sig="a" * 64), "POST", 401),
                ("tampered source route", dict(payload(), **{"from": "muse"}), "POST", 401),
                ("expiry", payload(expires=int(time.time()) - 1), "POST", 401),
                ("receiver", payload(route="unknown"), "POST", 400),
                ("path", payload(name="../" + note.name), "POST", 400),
                ("Windows path", payload(name="sub\\" + note.name), "POST", 400),
                ("method", payload(), "GET", 405),
                ("fields", {"note": "synthetic invalid details marker"}, "POST", 400),
                ("body size", "x" * 1025, "POST", 413),
            ):
                check("correction rejects " + reason, button(port, data, method), (status, "correction failed"))
            req = urllib.request.Request("http://127.0.0.1:%d/correct" % port, data=b"{", method="POST")
            check("correction rejects invalid JSON", request(req), (400, "correction failed"))
            check("rejected actions leave the note and receipts alone", note.exists() and receipts.empty(), True)
            log = (root / "receiver.log").read_text()
            check("each bad request writes one log line", log.count("correction rejected: "), 11)

            signed = payload()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: button(port, signed), range(2)))
            check("concurrent repeats both succeed", results, [(200, "OK"), (200, "OK")])
            moved = root / "inbox/architect" / note.name
            check("button moves the note out of the old inbox", not note.exists() and moved.is_file(), True)
            check("button updates the route", "route: architect" in moved.read_text(), True)
            examples = root / "route-examples.tsv"
            check("button adds one private ring example", examples.read_text().count("architect\tphone text\tring\n"), 1)
            check("examples are private", examples.stat().st_mode & 0o777, 0o600)
            headers, body = receipts.get(timeout=3)
            check("button sends a receipt for the new receiver", headers["Title"], "Architect")
            check("button receipt shows the old receiver, preview and voice hint",
                  body, b"moved from Orchestrator\nphone text" + voice_hint)
            check("concurrent identical taps send one receipt", receipts.empty(), True)
            actions = headers["Actions"].encode("latin-1").decode("utf-8")
            check("four receivers get two HTTP actions by default", actions.count("http, "), 2)
            muse_action = json.loads(re.search(r"body='([^']+)'", actions)[1])
            check("new receipt signs the current source",
                  muse_action == payload(route="muse", expires=muse_action["exp"], from_route="architect"), True)
            snapshot = (examples.stat().st_mtime_ns, moved.stat().st_mtime_ns)
            check("repeating a successful action returns success", button(port, signed), (200, "OK"))
            check("repeat sends no receipt and rewrites no files",
                  receipts.empty() and
                  snapshot == (examples.stat().st_mtime_ns, moved.stat().st_mtime_ns), True)
            other_button = payload(route="muse")
            check("an old action cannot move a note after a different correction", button(port, other_button), (200, "OK"))
            _, body = receipts.get(timeout=3)
            check("the old action reports the actual receiver", body == b"already moved to Architect" and muse.empty(), True)
            check("same target from a different source returns success",
                  button(port, payload(from_route="reviewer")), (200, "OK"))
            _, body = receipts.get(timeout=3)
            check("same target from a different source still sends status", body, b"already moved to Architect")

            env = dict(os.environ, RING_HOME=home)
            subprocess.run([sys.executable, "-B", os.path.join(HERE, "watch.py"), "--to", "architect", "--list"],
                           env=env, capture_output=True, check=True)
            check("watch marks a note that it prints", "shown: true" in moved.read_text(), True)
            journal = root / "muse-moving" / moved.name
            journal.parent.mkdir(exist_ok=True)
            journal.write_text(json.dumps({
                "phase": "pending", "old_route": "architect", "shown": True, "alerted": True, "attempts": 4,
                "automatic_attempts": 3, "last_attempt_at": time.time(),
                "record": {"route": "muse", "routed_by": "corrected",
                           "received_at": re.search(r"^received_at: (.+)$", moved.read_text(), re.M)[1]},
            }))
            check("a button resumes the Muse move after exhausting automatic attempts", button(port, muse_action), (200, "OK"))
            muse_headers, delivered = muse.get(timeout=3)
            check("the button retry uses the original delivery ID", muse_headers["X-Index-Delivery"],
                  hashlib.sha256(("move-to-muse|" + moved.name).encode()).hexdigest())
            headers, body = receipts.get(timeout=3)
            check("Muse gets the text", b"phone text" in delivered, True)
            check("Muse confirmation has no text or buttons",
                  (headers["Title"], body, headers["Actions"]),
                  ("Muse", b"moved from Architect\na work agent may have read it already", None))
            record = root / "muse-sent" / note.name
            check("the work side has a record without the text",
                  not moved.exists() and not journal.exists() and "phone text" not in record.read_text(), True)
            check("the old example is removed when moving to Muse", "phone text" in examples.read_text(), False)
            check("retrying move to Muse does not forward again", button(port, muse_action), (200, "OK"))
            check("repeat Muse tap sends no second receipt or webhook", receipts.empty() and muse.empty(), True)
            check("a Muse note cannot be moved back", button(port, payload(from_route="muse")), (409, "correction failed"))

            # The voice command arrived late. A later note and a test event must both be skipped.
            bookmark = root / "inbox" / "x-bookmark-577.md"
            when = time.time() - 10
            earlier = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when - 20))
            record.write_text(re.sub(r"received_at: .*", "received_at: " + earlier, record.read_text()))
            bookmark.write_text("---\nsource: x-bookmark\nrouted_to: orchestrator\nrouted_by: by folder\n---\n"
                                "Synthetic voice target marker\n\nLinks:\n")
            os.utime(bookmark, (when - 2, when - 2))
            later = root / "inbox" / "x-bookmark-578.md"
            later.write_text(bookmark.read_text())
            os.utime(later, (when + 1, when + 1))
            test_note = root / "inbox" / "x-bookmark-579.md"
            test_note.write_text(bookmark.read_text().replace("source: x-bookmark", "source: x-bookmark\ntest: true"))
            os.utime(test_note, (when - 1, when - 1))
            voice = "No, that should have been for the Architect, please."
            def voice_note(command, recorded):
                return post(port, key, {"transcription": (command.encode(), "", ""),
                                        "recordedAt": (str(int(recorded * 1000)).encode(), "", "")})
            check("signed voice correction is accepted", voice_note(voice, when)[0], 200)
            headers, body = receipts.get(timeout=3)
            check("voice receipt shows the old and new receivers",
                  (headers["Title"], body), ("Architect", b"moved from Orchestrator to Architect" + voice_hint))
            corrections = list((root / "corrections").glob("*.md"))
            check("voice command is saved only as a correction", len(corrections) == 1 and
                  corrections[0].read_text() == voice + "\n", True)
            new_bookmark = root / "inbox/architect" / bookmark.name
            check("voice moves the newest note and keeps bookmark metadata",
                  not bookmark.exists() and "routed_to: architect" in new_bookmark.read_text(), True)
            check("voice skips a later arrival and a prior test event", later.exists() and test_note.exists(), True)
            check("voice learns the post text without the Links footer",
                  "architect\tSynthetic voice target marker\tbookmark\t577\n" in examples.read_text(), True)
            failed_muse = root / "muse-failed" / "20261116T000000Z-block.md"
            failed_muse.write_text("---\nroute: muse\nrouted_by: spoken name\nreceived_at: %s\n---\nSynthetic private marker\n" %
                                   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when + 3)))
            voice_note("send that to the Architect.", when + 4)
            _, body = receipts.get(timeout=3)
            check("failed Muse is the latest target and blocks voice",
                  body == b"the last note is with Muse; nothing moved" and failed_muse.exists() and later.exists(), True)
            failed_muse.unlink()
            voice_note("that one was for Architect.", when - 86400)
            _, body = receipts.get(timeout=3)
            check("voice reports when no note can move", body == b"no note to move", True)
            ordinary = b"That was for a test of the usual routing."
            post(port, key, {"transcription": (ordinary, "", "")})
            headers, body = receipts.get(timeout=3)
            check("correction phrase without a receiver routes normally", headers["Title"], "Orchestrator")
            for command in ("That was for Architect about testing.", "Move that to Architect or Orchestrator."):
                voice_note(command, time.time())
                headers, body = receipts.get(timeout=3)
                check("a longer or multi-receiver note routes normally " + str(len(command)),
                      headers["Title"] == "Orchestrator" and command.encode() in body, True)

            oversized = root / "inbox" / "oversized.md"
            oversized.write_text("---\nroute: orchestrator\nrouted_by: spoken name\n---\n" + "z" * 4001)
            check("oversized move returns a generic failure", button(port, payload(name=oversized.name, route="muse")), (409, "correction failed"))
            _, body = receipts.get(timeout=3)
            check("oversized move explains the limit on the phone", body == b"too long to move; say it again to Muse" and
                  oversized.exists() and muse.empty(), True)
            check("the three moves are counted once each", (root / "receiver.log").read_text().count(" route corrected\n"), 3)
            log = (root / "receiver.log").read_text()
            check("correction logs contain no notes, invalid body, or signature",
                  any(text in log for text in (voice, "phone text", "Synthetic voice target marker",
                                               "synthetic invalid details marker", signed["sig"])), False)
            settings = json.loads((root / "config.json").read_text())
            (root / "jev-key").write_text("fake-delay-key")
            for timed_out in (False, True):
                settings.update(jev_url=base + "/jev", correction_wait=0.25 if timed_out else 2)
                (root / "config.json").write_text(json.dumps(settings))
                delivery = "delayed-" + str(timed_out)
                digest = hashlib.sha256(delivery.encode()).hexdigest()
                delayed = b"Synthetic delayed model note " + delivery.encode()
                check("earlier ring delivery is acknowledged while in work",
                      post(port, key, {"transcription": (delayed, "", "")}, delivery=delivery), (200, "OK"))
                gate = model_gates.get(timeout=2)
                marker = root / "in-work" / digest
                try:
                    entry = json.loads(marker.read_text()) if marker.exists() else {}
                    check("accepted in-work record has only an ID and arrival time",
                          set(entry) == {"delivery", "received_at"} and entry["delivery"] == delivery, True)
                    voice_note("Move that to Architect.", time.time())
                    if timed_out:
                        _, body = receipts.get(timeout=3)
                        check("voice wait expires with the required receipt",
                              body, b"an earlier note is still in work; nothing moved")
                    else:
                        try:
                            receipts.get(timeout=0.15)
                            stayed_waiting = False
                        except queue.Empty:
                            stayed_waiting = True
                        check("voice does not move an older routed note while waiting", stayed_waiting, True)
                finally:
                    gate.set()
                reports = [receipts.get(timeout=3)[1] for _ in range(1 if timed_out else 2)]
                folder = root / ("inbox" if timed_out else "inbox/architect")
                check("voice moves the newly completed note only when the wait succeeds",
                      len(list(folder.glob("*-" + digest[:8] + ".md"))) == 1 and
                      (b"moved from Orchestrator to Architect" + voice_hint in reports) == (not timed_out), True)
                deadline = time.monotonic() + 2
                while marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.03)
                check("finishing delivery clears its in-work record", marker.exists(), False)
            correction_limits(port, key, check)
        finally:
            fake.shutdown()
            thread.join()


def main():
    test_home = tempfile.TemporaryDirectory(prefix="ring-test-")
    home = test_home.name
    key = secrets.token_hex(32).encode()
    with open(os.path.join(home, "secret"), "wb") as f:
        f.write(key)
    port = free_port()
    env = dict(os.environ, RING_HOME=home, RING_PORT=str(port), RING_HOST="127.0.0.1")
    if SPEECH:
        env["RING_MODEL"] = SPEECH
    srv = subprocess.Popen([sys.executable, "-B", os.path.join(HERE, "receiver.py")], env=env)
    checks, fails = [], []

    def check(name, got, want):
        checks.append(name)
        ok = got == want
        print("%s  %s: got %r" % ("PASS" if ok else "FAIL", name, got))
        if not ok:
            fails.append(name)

    try:
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        meta = {"recordedAt": (str(int(time.time() * 1000)).encode(), "", ""), "client": (b"ring", "", "")}
        text_only = dict(meta, transcription=(b"phone text", "", ""))
        check("no signature is refused", post(port, key, text_only, sign=False)[0], 400)
        check("wrong secret is refused", post(port, b"wrong", text_only)[0], 401)
        check("changed body is refused", post(port, key, text_only, tamper=True)[0], 401)
        check("old timestamp is refused", post(port, key, text_only, ts=int(time.time()) - 3600)[0], 401)
        check("unknown trigger is refused", post(port, key, text_only, trigger="triple-click")[0], 400)
        check("bad delivery id is refused", post(port, key, text_only, delivery="../../x")[0], 400)
        did = str(uuid.uuid4())
        check("signed note is accepted", post(port, key, text_only, delivery=did), (200, "OK"))
        check("replay is not processed twice", post(port, key, text_only, delivery=did), (200, "Already received"))
        check("test event is accepted", post(port, key, dict(meta, test=(b"true", "", ""), transcription=(b"hello", "", "")),
                                             trigger="test-event", test=True)[0], 200)
        time.sleep(1.0)
        inbox = os.path.join(home, "inbox")
        check("2 notes are in the inbox", len(os.listdir(inbox)), 2)
        if SPEECH:
            aiff, m4a = os.path.join(home, "s.aiff"), os.path.join(home, "s.m4a")
            subprocess.run(["say", "-o", aiff, SENTENCE], check=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", aiff, "-ac", "1", "-ar", "16000", "-c:a", "aac", m4a], check=True)
            audio = open(m4a, "rb").read()
            t0 = time.time()
            check("recording is accepted", post(port, key, dict(meta, audio=(audio, "audio/mp4", "rec.m4a")))[0], 200)
            note = ""
            while time.time() - t0 < 180 and not note:
                time.sleep(0.5)
                for fn in sorted(os.listdir(inbox)):
                    body = open(os.path.join(inbox, fn)).read()
                    if "audio:" in body:
                        note = body
            print("--- note after %.1f s ---\n%s" % (time.time() - t0, note))
            low = note.lower()
            check("transcript has the key words", all(w in low for w in ("tracker", "release", "notes", "firmware")), True)
            listed = subprocess.run([sys.executable, os.path.join(HERE, "watch.py")], env=env, capture_output=True, timeout=10).stdout.decode()
            check("watcher prints 3 notes", "RING NOTES WAITING: 3" in listed, True)
        sys.path.insert(0, HERE)
        os.environ["RING_HOME"] = home
        import receiver
        check("looped text is flagged", receiver.looks_repeated("I don't know, " * 6), True)
        check("normal text is not flagged", receiver.looks_repeated(SENTENCE + " Then tell me the result today."), False)
        log = open(os.path.join(home, "receiver.log")).read()
        check("the log holds no secret", key.decode() in log, False)
        correction_tests(home, key, port, did, check)
    finally:
        srv.terminate()
        srv.wait(timeout=5)
    print("%d checks, %d failed" % (len(checks), len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
