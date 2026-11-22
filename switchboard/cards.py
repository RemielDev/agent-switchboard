#!/usr/bin/env python3
"""Discord card building blocks (Components V2) and the sample cards of the gallery thread (stdlib only).

A V2 message carries flags=32768 and no content; its body is components: a container (type 17, a coloured card) holding
text displays (10, markdown), separators (14), sections (9: text with a thumbnail or button beside it), media galleries
(12) and action rows (1) of buttons (2) or a select (3). Buttons with custom_id "demo:<what>:<n>" are answered by the
bridge's demo handler, so the gallery works without touching any real work.

  cards.py --selfcheck"""
import sys

V2 = 1 << 15                                     # IS_COMPONENTS_V2: the message is components only
COLORS = {"working": 0x5865F2, "ask": 0xF0B232, "done": 0x23A55A, "fail": 0xDA373C, "info": 0x4E5058, "update": 0x00A8FC}

def text(md): return {"type": 10, "content": md}
def sep(big=False, line=True): return {"type": 14, "divider": line, "spacing": 2 if big else 1}
def btn(label, cid=None, style=2, url=None, emoji=None, disabled=False):
    b = {"type": 2, "style": 5 if url else style, "label": label}
    if url: b["url"] = url
    else: b["custom_id"] = cid
    if emoji: b["emoji"] = {"name": emoji}
    if disabled: b["disabled"] = True
    return b
def row(*items): return {"type": 1, "components": list(items)}
def section(md, accessory): return {"type": 9, "components": [text(md)], "accessory": accessory}
def thumb(url): return {"type": 11, "media": {"url": url}}
def gallery(*urls): return {"type": 12, "items": [{"media": {"url": u}} for u in urls]}
def card(kind, *parts): return {"type": 17, "accent_color": COLORS[kind], "components": list(parts)}
def select(cid, placeholder, options): return row({"type": 3, "custom_id": cid, "placeholder": placeholder, "options": [{"label": l, "value": str(n), "description": d} for n, (l, d) in enumerate(options)]})
def message(*parts): return {"flags": V2, "components": list(parts), "allowed_mentions": {"parse": []}}

# ---- the sample cards: what each kind of update could look like ----
def status_card(avatar):
    return card("working",
        section("### ⚙️ Kepler · search filters\n**Working** · step 3 of 5\n-# Builder on Desk-PC · started 4 min ago", thumb(avatar)),
        sep(),
        text("✅ Plan  →  ✅ Build  →  🔄 **Test**  →  ⬜ Review  →  ⬜ Deploy"),
        text("> *Now:* running the integration tests"),
        row(btn("Show live log", "demo:log:0"), btn("Stop", "demo:stop:0", style=4)))

def decision_card():
    return card("ask",
        text("## 🚀 Deploy Kepler?\nThe search filters **pass the test suite** and **passed review**."),
        sep(),
        text("**What goes live**\n- Filter results by date and tag\n- Saved searches\n-# 2 services restart · about a minute of read-only mode"),
        row(btn("Deploy", "demo:deploy:0", style=3, emoji="🚀"), btn("More changes", "demo:changes:0"), btn("Not now", "demo:later:0")))

def report_card():
    return card("done",
        text("## ✅ Built, reviewed, not live yet\n*Kepler · checkout fix* · ready to deploy"),
        sep(),
        text("**Changed**\n- Prices load before the checkout opens\n- Fixed the double submit on slow connections\n\n**Checked**\n- Test suite passed\n- Reviewer: no issues"),
        sep(line=False),
        text("-# 0.4% of Claude's week · 5 runs · 7 min · Opus 58% · Sonnet 42%"),
        row(btn("Accept", "demo:accept:0", style=3), btn("Needs fixing", "demo:fix:0"), btn("Undo", "demo:undo:0", style=4), btn("Close", "demo:close:0")))

def update_card():
    return card("update",
        text("### 🔗 Update from another Kepler thread\n**Release 2.4.0 is live** and the release notes are posted in #announcements."),
        text("-# The deploy question here is no longer needed, so its buttons were removed."),
        row(btn("Open that thread", "demo:jump:0"), btn("Got it", "demo:ok:0")))

def alert_card():
    return card("fail",
        text("## ❌ Build failed\n`checkout/prices.py:142` TypeError: 'NoneType' object is not subscriptable"),
        sep(),
        text("> The checkout opened before prices loaded. A retry adds a wait for the price table."),
        row(btn("Retry with the fix", "demo:retry:0", style=1), btn("Show the log", "demo:log:1"), btn("Skip", "demo:skip:0")))

def media_card(avatar):
    return card("info",
        text("### 🖼️ Screenshots\n3 screenshots from the checkout test run"),
        gallery(avatar, avatar, avatar),
        row(btn("Looks right", "demo:accept:1", style=3), btn("Something is off", "demo:fix:1")))

def menu_card():
    return card("info",
        text("### 🧭 Pick one from a menu\nGood when there are more than 4 choices, such as which repo to work on."),
        select("demo:pick:0", "Choose a repo", [("Kepler", "search, checkout"), ("Field Notebook", "UI round 5"), ("Sloop", "sync engine"), ("Stack.io", "website")]))

def openers_card():
    return card("info",
        text("### 🃏 Buttons that open cards\nEach one opens a private card only you can see, or a form."),
        row(btn("Status card", "demo:open:status", style=1), btn("Decision card", "demo:open:decision", style=1), btn("Build report", "demo:open:report", style=1)),
        row(btn("Alert card", "demo:open:alert"), btn("Open a request form", "demo:form:0", style=3, emoji="📝")))

def styles_card():
    return card("info",
        text("### 🎛️ Button styles"),
        row(btn("Primary", "demo:style:1", style=1), btn("Secondary", "demo:style:2"), btn("Success", "demo:style:3", style=3), btn("Danger", "demo:style:4", style=4)),
        row(btn("With emoji", "demo:style:5", emoji="⚡"), btn("Disabled", "demo:style:6", disabled=True), btn("Link", url="https://discord.com/developers/docs/components/overview")))

def opened(what, avatar):
    """The private card a 'demo:open:<what>' button shows."""
    return {"status": lambda: status_card(avatar), "decision": decision_card, "report": report_card, "alert": alert_card}.get(what, update_card)()

def form():
    """The modal a 'demo:form' button opens: a request typed into a form instead of the chat."""
    return {"custom_id": "demo:form:submit", "title": "New request", "components": [
        row({"type": 4, "custom_id": "what", "label": "What should change?", "style": 2, "placeholder": "Make the checkout open faster on phones", "required": True}),
        row({"type": 4, "custom_id": "repo", "label": "Repo or area", "style": 1, "placeholder": "Kepler", "required": False})]}

FORMATTING = """# Card and button gallery
Everything here is a **demo**: taps answer privately and change nothing. Pick what you like and say which to use where.
## Text formatting
**bold**, *italic*, __underline__, ~~strike~~, ||spoiler||, `inline code`, [a masked link](https://discord.com)
> a quote for what the agent is doing now
-# small grey subtext, good for receipts and footnotes
### Lists and code
- bullet one
  - nested detail
1. numbered step
```python
print("code block with highlighting")
```
<t:1794510000:R> · a live timestamp that shows relative time"""

def selfcheck():
    m = message(decision_card())
    assert m["flags"] == V2 and m["components"][0]["type"] == 17 and m["components"][0]["components"][-1]["type"] == 1
    assert all(len(r["components"]) <= 5 for c in (status_card("u"), report_card(), openers_card(), styles_card()) for r in c["components"] if r["type"] == 1)
    assert opened("report", "u")["accent_color"] == COLORS["done"] and form()["components"][0]["components"][0]["type"] == 4
    assert styles_card()["components"][2]["components"][2]["style"] == 5 and "custom_id" not in styles_card()["components"][2]["components"][2]
    print("cards self-check OK")

if __name__ == "__main__":
    if "--selfcheck" in sys.argv: selfcheck()
