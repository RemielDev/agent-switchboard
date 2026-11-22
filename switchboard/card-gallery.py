#!/usr/bin/env python3
"""Post the card and button gallery as a thread in #orchestrator (stdlib only). Its buttons are answered by the bridge's
demo handler. The thread and message ids go to Queue/discord/gallery.json, which the janitor leaves alone.

  card-gallery.py post        create the thread and post every sample
  card-gallery.py --selfcheck"""
import importlib.util, json, sys, time
from pathlib import Path

TK = Path(__file__).resolve().parent
sys.path.insert(0, str(TK))
import cards

def bridge():
    spec = importlib.util.spec_from_file_location("bridge", TK / "discord-bridge.py"); br = importlib.util.module_from_spec(spec); spec.loader.exec_module(br)
    return br

def samples(avatar):
    """(caption, payload) in the order they are posted."""
    c = cards
    embed = {"embeds": [{"title": "Classic embed card", "description": "The older card style: a coloured side bar, fields and a footer. Fine for read-only summaries.",
                         "color": c.COLORS["working"], "fields": [{"name": "Repo", "value": "Kepler", "inline": True}, {"name": "Step", "value": "Review", "inline": True},
                         {"name": "Device", "value": "Desk-PC", "inline": True}], "footer": {"text": "Footer for receipts"}, "thumbnail": {"url": avatar}}]}
    poll = {"poll": {"question": {"text": "Which results layout should the builder use?"}, "answers": [{"poll_media": {"text": "Grid"}}, {"poll_media": {"text": "List"}},
            {"poll_media": {"text": "Carousel"}}], "duration": 24, "allow_multiselect": False}}
    return [
        ("Formatting", {"content": c.FORMATTING, "allowed_mentions": {"parse": []}}),
        ("Button styles", c.message(c.text("## 🎛️ Buttons"), c.styles_card())),
        ("Buttons that open cards", c.message(c.openers_card())),
        ("Status card", c.message(c.text("## 🗂️ Cards for each situation\n-# Coloured bar = state: blue working, yellow needs you, green done, red failed, light blue update"), c.status_card(avatar))),
        ("Decision card", c.message(c.decision_card())),
        ("Build report card", c.message(c.report_card())),
        ("Cross-thread update", c.message(c.update_card())),
        ("Failure alert", c.message(c.alert_card())),
        ("Media card", c.message(c.media_card(avatar))),
        ("Menu card", c.message(c.menu_card())),
        ("Classic embed", embed),
        ("Poll", poll),
    ]

def post():
    br = bridge(); cfg = json.loads(br.dpath("config.json").read_text(encoding="utf-8"))
    parent = cfg["channels"]["orchestrator"]
    me = br.api("GET", "/users/@me")
    avatar = f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=256" if me.get("avatar") else "https://cdn.discordapp.com/embed/avatars/0.png"
    th = br.api("POST", f"/channels/{parent}/threads", {"name": "🃏 Card and button gallery", "type": 11, "auto_archive_duration": 10080})["id"]
    br.api("PUT", f"/channels/{th}/thread-members/{cfg['owner_id']}")  # a thread the owner is not in never shows in their sidebar
    out = {"thread": th, "msgs": [], "at": time.time()}
    for name, body in samples(avatar):
        try: out["msgs"].append(br.api("POST", f"/channels/{th}/messages", body)["id"]); print("posted", name)
        except Exception as e: print("FAILED", name, str(e)[:300])
        time.sleep(0.6)
    br.dpath("gallery.json").write_text(json.dumps(out), encoding="utf-8")
    print("thread", th)

def selfcheck():
    s = samples("u"); assert len(s) == 12 and s[1][1]["flags"] == cards.V2 and "poll" in s[-1][1]
    print("card-gallery self-check OK")

if __name__ == "__main__":
    if "--selfcheck" in sys.argv: selfcheck()
    elif sys.argv[1:2] == ["post"]: post()
    else: print(__doc__)
