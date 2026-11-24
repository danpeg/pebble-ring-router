# pebble-ring-router

Talk to your Claude Code sessions with the Pebble Index 01 ring.

You hold the ring button and speak. The note is transcribed on your Mac and lands in the inbox of the right agent. That agent's Claude Code session wakes up and does the work. There is no API harness: the agents are ordinary Claude Code sessions on your subscription.

## How one note travels

1. **Ring → iPhone.** The ring sends the audio to the Pebble app over Bluetooth.
2. **iPhone → Mac.** The Pebble app posts the raw audio to `receiver.py` on your Mac. It uses the Index webhook with "Sign requests" on. The Mac is reachable only on your own tailnet, through Tailscale Serve.
3. **Speech to text on the Mac.** whisper.cpp transcribes the note. The audio never leaves the Mac.
4. **Routing.** The receiver picks one receiver for the note:
   - **A spoken name first:** "Architect, ..." goes to the architect. No model.
   - **No name:** it compares the note with saved example notes, by shared words. No model.
   - **No clear match:** a small decision model picks (optional, see "Decision model").
   - **Still unsure:** the note goes to the default receiver.
5. **Delivery.** A note becomes a text file in `~/.ring-router/inbox/<receiver>/`. The default receiver uses `inbox/` itself.
6. **Wake-up.** Each Claude Code session runs `watch.py` as a background command. When a note arrives, the command exits, and that exit wakes the session with the note. The session works, answers, marks the note done, and starts the watcher again.

Optional: a push to your phone for each note (ntfy), with buttons to move a note that went to the wrong receiver.

## What you need

- A Mac that stays on, with the Claude Code sessions open. A note waits in its folder while its session is closed.
- Python 3.9 or later. Standard library only.
- `ffmpeg` and `whisper-cli` (whisper.cpp), plus a model file. The default is `~/.ring-router/models/ggml-large-v3-q5_0.bin`.
- Tailscale on the Mac and the iPhone.
- The Pebble app with the Index 01 ring.

## Set up

```bash
bash setup.sh
```

- It makes a signing secret in `~/.ring-router/secret`. The secret is never printed. It goes to the clipboard for 2 minutes.
- It starts the receiver as a launchd service and puts Tailscale Serve (HTTPS, tailnet only) in front of it.
- It prints the address to paste in the Pebble app webhook. Paste the secret under "Sign requests".
- `bash setup.sh --copy` puts the secret on the clipboard again.

On the `/ring` webhook, the receiver refuses unsigned requests, old timestamps, and changed bodies. A valid duplicate returns HTTP 200 without reprocessing. `/health` needs no signature: it only says that the receiver runs.

## In a Claude Code session

Ask the session to run this as a background command, and to start it again after each note:

```bash
python3 watch.py --to architect
```

- With no `--to`, it reads the notes in the root `inbox/`, which belong to the default receiver.
- `python3 watch.py --to architect --list` shows the waiting notes. `python3 watch.py --to architect --done <file>` moves a note to `done/`.
- **Keep `default_route` as `orchestrator`.** `watch.py` treats `orchestrator` as the owner of the root `inbox/`.

## Receivers

Receivers live in `~/.ring-router/config.json`. Each one has spoken names and one line about what it owns:

```json
{
  "default_route": "orchestrator",
  "route_names": {
    "orchestrator": ["orchestrator", "orchestra"],
    "architect": ["architect", "architecture"]
  },
  "route_descriptions": {
    "orchestrator": "Builds and runs the product: code, bugs, deploys.",
    "architect": "How the agents and their rules work."
  }
}
```

- The routing settings are read again for each note, so a changed value needs no restart. If you delete a key, restart the receiver.
- This sample replaces the built-in receivers, so it has no `muse`. To keep the webhook receiver, add `muse` to both maps.
- Add a word to `route_names` when the speech model hears a receiver's name wrong. The receiver matches these words against the transcript.
- `glossary.txt` (and `~/.ring-router/glossary.txt`) gives words to the speech model, so it spells your terms right.
- Example notes: `route-examples.tsv` here, and your own private notes in `~/.ring-router/route-examples.tsv`.
- `python3 routes.py --help` lists the commands. The main ones:
  - `last [N]`: where the last notes went;
  - `move FILE ROUTE`: deliver a note that went to the wrong receiver;
  - `add ROUTE --note FILE`: correct a note and keep it as an example;
  - `check`: test that each example still goes to its receiver. With a Jev key, `check` can send eligible `ring` examples in full (up to 4,000 characters), private ones too. `"examples_to_jev": []` keeps them local.

## Decision model (optional)

- With a key in `~/.ring-router/jev-key`, notes with no clear match go to Jev, a small decision model from TypeSafe (`jev-latest`).
- **What it gets:** the note text, the receiver descriptions, and up to a few saved example notes that look like the note (200 characters each). Your private examples can be among them. Muse examples are never sent.
- Set `"examples_to_jev": []` in `config.json` to send no examples.
- `jev_url`, `jev_model`, and `jev_min_confidence` in `config.json` change it.
- With no key, those notes go to the default receiver.

## A receiver that is not Claude Code (optional)

A receiver named `muse` posts the note text, signed, to a webhook in the Index webhook form. Put the address in `~/.ring-router/muse-url` and the signing secret in `~/.ring-router/muse-secret`. The audio is never sent.

## Phone receipts (optional)

- `receipt_url`: a full ntfy topic URL. Each note sends a push that names the receiver.
- **A push with note text holds the first 120 characters of the note**, also for a Muse note. A failed Muse send and some move receipts carry only a status. Use a private ntfy server or topic.
- `correct_url`: the Tailscale Serve base address of the receiver. The push then has up to 2 buttons (`receipt_buttons`) to move the note to another receiver. Muse receipts have no buttons.

## Tests

```bash
python3 test_routing.py
python3 test_receipt.py
python3 test_receiver.py
```

The tests use a temporary home, fake models, and test secrets. They never touch your real files. The speech test in `test_receiver.py` runs only with `--speech <path to a whisper model>`.
