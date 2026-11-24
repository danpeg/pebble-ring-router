#!/usr/bin/env python3
"""Test phone receipts with a temporary home and a patched HTTP call. No network.

  python3 test_receipt.py
"""
import hashlib
import hmac
import json
import os
import re
import tempfile
from http.client import HTTPConnection
from unittest.mock import MagicMock, patch


def main():
    with tempfile.TemporaryDirectory(prefix="ring-receipt-test-") as home:
        os.environ["RING_HOME"] = home
        import receiver

        fails, checked = [], []

        def check(name, got, want):
            checked.append(name)
            ok = got == want
            print(("PASS " if ok else "FAIL ") + name)
            if not ok:
                fails.append(name)

        def config(value):
            with open(os.path.join(home, "config.json"), "w") as f:
                json.dump(value, f)

        def run(short, text, test=False, audio=""):
            meta = {"kind": "ring-note", "delivery": short, "trigger": "single-click-hold",
                    "test": str(test).lower(), "recorded_at": "", "received_at": ""}
            receiver.process("20261116T000000Z", short, meta, audio, text)
            return meta

        def note(folder, short):
            with open(os.path.join(home, folder, "20261116T000000Z-%s.md" % short)) as f:
                return f.read()

        url = "http://receipt.invalid/a-test-topic"
        with patch.object(receiver.urllib.request, "urlopen", return_value=MagicMock()) as http:
            receiver.send_receipt("orchestrator", "spoken name", "No config", False)
            check("no config means no HTTP call", http.call_count, 0)
            config({})
            receiver.send_receipt("orchestrator", "spoken name", "No key", False)
            check("no receipt key means no HTTP call", http.call_count, 0)
            config({"receipt_url": ""})
            receiver.send_receipt("orchestrator", "spoken name", "Empty URL", False)
            check("empty receipt URL means no HTTP call", http.call_count, 0)
            config({"receipt_url": url})

            def sent(route, how, text="Work text", test=False, failed=None):
                http.reset_mock()
                receiver.send_receipt(route, how, text, test, failed=failed)
                check("one HTTP call per receipt", http.call_count, 1)
                req = http.call_args.args[0]
                check("configured URL is used", req.full_url, url)
                check("receipt uses POST", req.get_method(), "POST")
                check("HTTP timeout is 3 seconds", http.call_args.kwargs, {"timeout": 3})
                check("all header values are ASCII", all(v.isascii() for _, v in req.header_items()), True)
                return req, req.data.decode("utf-8")

            text = "Architect, " + "1234567890" * 15
            req, body = sent("architect", "spoken name", text)
            check("work note uses exactly the first 120 characters", body, "by name\n" + text[:120])
            check("Architect title", req.get_header("Title"), "Architect")
            req, body = sent("partner", "spoken name", "Partner, work text")
            check("a receiver from config.json shows its own name", req.get_header("Title"), "Partner")
            check("name route has a check mark", req.get_header("Tags"), "white_check_mark")
            req, body = sent("orchestrator", "decision model (0.97)")
            check("sure model route uses plain words", body, "sure (0.97)\nWork text")
            check("Orchestrator title", req.get_header("Title"), "Orchestrator")
            check("sure route has a check mark", req.get_header("Tags"), "white_check_mark")
            req, body = sent("orchestrator", "default (decision model was not sure: muse at 0.55)")
            check("unsure route uses plain words", body, "not sure: muse 0.55, sent to the default\nWork text")
            check("unsure route has a question", req.get_header("Tags"), "question")
            for how in ("default (decision model failed: TimeoutError)",
                        "default (decision model answered HTTP 503)",
                        "default (no decision model key)",
                        "default (the note was not understood)",
                        "default (test event or empty note)"):
                req, body = sent("orchestrator", how)
                check("default route has a question", req.get_header("Tags"), "question")
                check("default reason is in the routing line", body.splitlines()[0], how[9:-1] + ", sent to the default")

            private = "Muse, private marker: שלום café 🐙"
            for how in ("spoken name", "decision model (0.91)"):
                req, body = sent("muse", how, private)
                check("Muse title", req.get_header("Title"), "Muse")
                check("Muse body has the routing line and the text", body,
                      ("by name" if how == "spoken name" else "sure (0.91)") + "\n" + private[:120])
                check("Muse text is absent from the headers",
                      any(word in str(req.header_items()) for word in ("private", "שלום", "café", "🐙")), False)
            req, body = sent("muse", "spoken name", private, failed="HTTP 503")
            check("Muse failure has a warning", req.get_header("Tags"), "warning")
            check("Muse failure has no note text", body, "Not sent to Muse (HTTP 503)")
            req, body = sent("muse", "spoken name", private, test=True, failed="HTTP 503")
            check("test failure starts with test", body, "test Not sent to Muse (HTTP 503)")
            req, body = sent("muse", "spoken name", private, test=True)
            check("test Muse receipt starts with test", body, "test by name\n" + private[:120])
            unicode_text = "שלום café 🐙" * 20
            req, body = sent("architect", "spoken name", unicode_text, test=True)
            check("UTF-8 body cuts characters rather than bytes", body, "test by name\n" + unicode_text[:120])

            key = b"synthetic-receipt-key"
            with open(os.path.join(home, "secret"), "wb") as f:
                f.write(key)
            note_name = "20261116T000000Z-action.md"
            config({"receipt_url": url})
            receiver.send_receipt("architect", "spoken name", "Synthetic actions marker", False, note_name=note_name)
            check("no correct URL means no buttons", http.call_args.args[0].has_header("Actions"), False)
            address = "https://ring.invalid:9446"
            settings = {"receipt_url": url, "correct_url": address + "/", "route_descriptions": {
                "partner": "", "architect": "", "muse": "", "orchestrator": "",
            }}
            config(settings)
            with patch.object(receiver.time, "time", return_value=1700000000):
                run("action", "Architect, synthetic actions marker")
            req = http.call_args.args[0]
            header = req.get_header("Actions")
            actions = header.decode("utf-8").split("; ")
            check("four receivers get two actions in config order by default",
                  [part.split(", ")[1] for part in actions], ["→ Partner", "→ Muse"])
            check("receipt ends with the voice command for the omitted receiver",
                  req.data.decode(), 'by name\nArchitect, synthetic actions marker\nother, latest note only: say "that was for Orchestrator"')
            for action, target in zip(actions, ("partner", "muse")):
                prefix = "http, → %s, %s/correct, method=POST, headers.Content-Type=application/json" % (target.capitalize(), address)
                check("action uses POST and clear for " + target, action.startswith(prefix) and action.endswith("clear=true"), True)
                payload = json.loads(re.search(r"body='([^']+)'", action)[1])
                expires = 1700000000 + 7 * 86400
                data = ("correct|%s|architect|%s|%d" % (note_name, target, expires)).encode()
                signature = hmac.new(key, data, hashlib.sha256).hexdigest()
                check("action body and signature for " + target,
                      payload, {"note": note_name, "from": "architect", "to": target, "exp": expires, "sig": signature})
            conn = HTTPConnection("receipt.invalid")
            with patch.object(conn, "send") as send:
                conn.request("POST", "/topic", body=req.data, headers=dict(req.header_items()))
            check("arrow goes over HTTP as UTF-8", b"Actions: http, " + "→ Partner".encode() in send.call_args_list[0].args[0], True)
            titles = ["Partner", "Muse", "Orchestrator"]
            for configured, expected in ((0, 0), (1, 1), (2, 2), (5, 2)):
                config(dict(settings, receipt_buttons=configured))
                receiver.send_receipt("architect", "spoken name", unicode_text, False, note_name=note_name)
                req = http.call_args.args[0]
                header = req.get_header("Actions")
                actions = header.decode().split("; ") if header else []
                check("receipt_buttons=%d keeps the expected buttons" % configured,
                      [action.split(", ")[1] for action in actions], ["→ " + title for title in titles[:expected]])
                hints = ' or '.join('"that was for %s"' % title for title in titles[expected:])
                check("receipt_buttons=%d keeps the 120-character preview and gives one hint line" % configured,
                      req.data.decode(), "by name\n" + unicode_text[:120] + "\nother, latest note only: say " + hints)
            check("a receiver title works in a voice correction without a configured alias",
                  receiver.correction_target("That was for Partner"), "partner")
            receiver.send_receipt("muse", "spoken name", private, False, note_name=note_name)
            check("Muse has no buttons even when configured", http.call_args.args[0].has_header("Actions"), False)
            check("Muse has no voice correction hint", http.call_args.args[0].data.decode(), "by name\n" + private[:120])
            config({"receipt_url": url})

            # A changed or removed URL takes effect even when CFG has a startup URL.
            receiver.CFG["receipt_url"] = url
            url = "http://receipt.invalid/changed-topic"
            config({"receipt_url": url})
            sent("orchestrator", "spoken name")
            config({})
            http.reset_mock()
            receiver.send_receipt("muse", "spoken name", private, False)
            check("removing the key disables a startup URL", http.call_count, 0)
            os.unlink(os.path.join(home, "config.json"))
            receiver.send_receipt("muse", "spoken name", private, False)
            check("removing the config disables a startup URL", http.call_count, 0)
            config({"receipt_url": url})

            # Exercise process(), including final route overrides and Muse failure.
            for route in ("orchestrator", "architect", "muse"):
                http.reset_mock()
                folder = "muse-sent" if route == "muse" else ("inbox" if route == "orchestrator" else "inbox/architect")

                def after_write(req, timeout):
                    check("note is written before receipt", os.path.isfile(os.path.join(home, folder, "20261116T000000Z-%s.md" % route)), True)
                    return MagicMock()

                http.side_effect = after_write
                with patch.object(receiver, "forward_to_muse", return_value=(True, "HTTP 200")):
                    run(route, route.capitalize() + ", synthetic note")
                check("process sends one receipt", http.call_count, 1)
                if route == "muse":
                    check("sent Muse note has its text in the receipt", http.call_args.args[0].data, "by name\nMuse, synthetic note".encode("utf-8"))

            http.side_effect = None
            http.reset_mock()
            with patch.object(receiver, "forward_to_muse", return_value=(False, "HTTP 503")):
                run("failed", private, test=True)
            check("Muse failure sends one receipt", http.call_count, 1)
            req = http.call_args.args[0]
            check("Muse failure keeps Muse title", req.get_header("Title"), "Muse")
            check("process failure receipt has no personal text", req.data, b"test Not sent to Muse (HTTP 503)")
            check("failed note is retained in personal folder", private in note("muse-failed", "failed"), True)
            check("failure alert has no personal text", private in note("inbox", "failed"), False)

            with patch.object(receiver, "transcribe", return_value=("words repeat " * 10, "fake", "")), \
                    patch.object(receiver, "choose_route", return_value=("muse", "decision model (0.99)")), \
                    patch.object(receiver, "forward_to_muse") as forward:
                run("unclear", "", audio="fake-audio")
            check("receipt uses final default route", http.call_args.args[0].get_header("Title"), "Orchestrator")
            check("unclear note has question tag", http.call_args.args[0].get_header("Tags"), "question")
            check("unclear note is not forwarded to Muse", forward.call_count, 0)

            # The exception text includes private content to catch accidental logging.
            http.side_effect = RuntimeError(private)
            with patch.object(receiver, "prune") as prune:
                run("http-error", "Architect, work marker")
            check("HTTP error does not stop process after writing", "work marker" in note("inbox/architect", "http-error"), True)
            check("process continues to prune after HTTP error", prune.call_count, 1)
            with open(os.path.join(home, "receiver.log")) as f:
                log = f.read()
            check("HTTP error is logged once", log.count("receipt failed: RuntimeError"), 1)
            check("log has no note text, body, exception message or URL",
                  any(s in log for s in (private, "work marker", "by name", "Not sent to Muse", url)), False)
            check("receipt error did not reach the process handler", "process failed" in log, False)
            with patch.object(receiver, "log", side_effect=OSError("log unavailable")):
                receiver.send_receipt("muse", "spoken name", private, False)
            check("a log failure is also contained", True, True)

        print("%d checks, %d failed" % (len(checked), len(fails)))
        return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
