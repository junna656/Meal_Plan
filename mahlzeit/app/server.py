"""Mahlzeit-Planer: kleiner Server für die Home-Assistant-App (nur Python-Standardbibliothek).

Speichert alle Daten als JSON-Dokumente in /data/store.json und liefert die Weboberfläche über Ingress aus.
"""
import json
import math
import os
import random
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import re
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DATA = Path(os.environ.get("DATA_DIR", "/data"))
WWW = Path(__file__).parent / "www"
STORE = DATA / "store.json"
OPTIONS = DATA / "options.json"
SEED = Path(__file__).parent / "seed.json"
PORT = int(os.environ.get("PORT", "8099"))
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
if not TOKEN:
    try:  # s6-overlay legt die Container-Umgebung hier ab
        TOKEN = Path("/run/s6/container_environment/SUPERVISOR_TOKEN").read_text().strip()
    except Exception:
        TOKEN = ""
CORE = os.environ.get("CORE_URL", "http://supervisor/core/api")
PATH_RE = re.compile(r"^(recipes|wishes|plans|shop|settings)/[A-Za-z0-9_\-.:@+~]{1,120}$")
MAX_BODY = 256 * 1024

lock = threading.Lock()
try:
    TZ = ZoneInfo(os.environ.get("TZ") or "Europe/Berlin")
except Exception:
    TZ = ZoneInfo("Europe/Berlin")


def deadline(week_id):
    """Samstag 9:00 vor der Planwoche."""
    y, w = week_id.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    sat = monday - timedelta(days=2)
    return datetime(sat.year, sat.month, sat.day, 9, 0, tzinfo=TZ)


def decide_due():
    """Entscheidet alle Abstimmungen, deren Frist abgelaufen ist."""
    now = datetime.now(TZ)
    changed = False
    with lock:
        docs = state["docs"]
        open_wishes = {p: d for p, d in docs.items() if p.startswith("wishes/") and not d.get("done")}
        wished = {d.get("r") for d in open_wishes.values() if d.get("r")}
        for path, plan in list(docs.items()):
            m = re.fullmatch(r"plans/(\d{4}-W\d{2})", path)
            if not m or deadline(m.group(1)) > now:
                continue
            for k, day in (plan.get("days") or {}).items():
                cands = (day or {}).get("cands") or []
                if not cands:
                    if (day or {}).get("voting"):
                        day["voting"] = None
                        changed = True
                    continue
                votes = day.get("votes") or {}
                n = {c: 0 for c in cands}
                for c in votes.values():
                    if c in n:
                        n[c] += 1
                top = max(n.values())
                tied = [c for c in cands if n[c] == top]
                tied = [c for c in tied if c in wished] or tied
                winner = random.choice(tied)
                day.update({"r": winner, "cands": None, "votes": None, "voting": None, "auto": True})
                for wp, wd in open_wishes.items():
                    if wd.get("r") == winner and not wd.get("done"):
                        wd.update({"done": True, "week": m.group(1)})
                print(f"Abstimmung {m.group(1)} {k} entschieden: {winner} ({top} Stimmen)", flush=True)
                changed = True
        if changed:
            state["rev"] += 1
            save_store(state)


DAYNAMES = {"mo": "Mo", "di": "Di", "mi": "Mi", "do": "Do", "fr": "Fr", "sa": "Sa", "so": "So"}
ORDER = list(DAYNAMES)


def parse_rules(s):
    s = str(s or "").lower().strip()
    if not s or s == "nie":
        return []
    out = []
    for part in [x.strip() for x in s.split(";") if x.strip()]:
        wk, days = "immer", None
        for t in part.split():
            if t in ("immer", "gerade", "ungerade"):
                wk = t
            else:
                days = [d.strip()[:2] for d in t.split(",") if d.strip()]
        out.append((wk, days))
    return out


def people(o):
    used, res = set(), []
    for p in o.get("personen", []):
        pid = re.sub(r"[^a-z0-9äöüß]+", "-", str(p.get("name") or "p").lower())[:30] or "p"
        while pid in used:
            pid += "-"
        used.add(pid)
        res.append({"id": pid, "n": p.get("name", ""), "kid": bool(p.get("kind")), "veg": bool(p.get("vegetarisch")),
                    "rules": parse_rules(p.get("muster"))})
    return res


def def_att(ppl, k, monday):
    even = monday.isocalendar()[1] % 2 == 0
    return {p["id"]: any((wk == "immer" or (wk == "gerade") == even) and (not days or k in days) for wk, days in p["rules"])
            for p in ppl}


def fixed_for(o, ppl, k, monday, att):
    everyone = bool(ppl) and all(att.get(p["id"]) for p in ppl)
    for f in o.get("feste_abende", []):
        if f.get("tag") != k or not f.get("gericht"):
            continue
        wenn = f.get("wenn", "immer")
        if wenn == "immer":
            return f["gericht"]
        if not everyone:
            continue
        if wenn == "alle_erster_im_monat":
            d = monday + timedelta(days=ORDER.index(k))
            x, first = d - timedelta(days=7), True
            while x.month == d.month:
                m2 = x - timedelta(days=x.weekday())
                a = def_att(ppl, k, m2)
                if all(a.get(p["id"]) for p in ppl):
                    first = False
                    break
                x -= timedelta(days=7)
            if first:
                return f["gericht"]
            continue
        return f["gericht"]
    return None


def num(x):
    s = f"{x:.1f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def fmt_q(q, u):
    if u in ("g", "ml"):
        v = math.ceil(q / 5) * 5 if q < 50 else math.ceil(q / 10) * 10
        return f"{num(v / 1000)} {'kg' if u == 'g' else 'l'}" if v >= 1000 else f"{v} {u}"
    return f"{num(math.ceil(q - 0.05) if u == 'Stk' else math.ceil(q * 2) / 2)} {u}"


def week_summary(week_id):
    """Plan-Übersicht und offene Einkaufsposten einer Woche (wie in der Weboberfläche gerechnet)."""
    o = load_options()
    ppl = people(o)
    y, w = week_id.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    docs = state["docs"]
    recipes = {p[8:]: d for p, d in docs.items() if p.startswith("recipes/")}
    by_name = {str(d.get("name", "")).lower(): d for d in recipes.values()}
    plan = (docs.get(f"plans/{week_id}") or {}).get("days") or {}
    shop = docs.get(f"shop/{week_id}") or {}
    kid = float(o.get("kinderportion") or 0.6)
    tage = [t.strip()[:2] for t in str(o.get("tage", "mo,di,mi,do")).lower().split(",")]
    lines, items = [], {}
    for k in [d for d in ORDER if d in tage]:
        day = plan.get(k) or {}
        att = {**def_att(ppl, k, monday), **(day.get("att") or {})}
        r = recipes.get(day.get("r"))
        fx = None if r else fixed_for(o, ppl, k, monday, att)
        if not r and fx:
            r = by_name.get(fx.lower())
        lines.append(f"{DAYNAMES[k]}: {fx or (r or {}).get('name') or '–'}")
        if not r:
            continue
        tot = veg = 0.0
        for p in ppl:
            if att.get(p["id"]):
                f = kid if p["kid"] else 1
                tot += f
                veg += f if p["veg"] else 0
        ing = r.get("ing") or []
        has_alt = any(x.get("t") == "veg" for x in ing)
        for x in ing:
            t = x.get("t", "all")
            n = (tot - veg if has_alt else tot) if t == "meat" else veg if t == "veg" else tot
            q = float(x.get("q") or 0) * n
            if q <= 0:
                continue
            key = re.sub(r"[^a-z0-9äöüß|]+", "-", f"{x.get('n')}|{x.get('u')}".lower())
            e = items.setdefault(key, {"n": x.get("n"), "u": x.get("u"), "q": 0.0})
            e["q"] += q
    checked = shop.get("checked") or {}
    todo = [f"{e['n']} ({fmt_q(e['q'], e['u'])})" for k, e in sorted(items.items(), key=lambda i: str(i[1]['n']).lower())
            if not checked.get(k)]
    todo += [x.get("t") for x in shop.get("extras") or [] if not x.get("done") and x.get("t")]
    return lines, todo


def add_todo(entity, items):
    """Fügt Einträge hinzu, die nicht schon offen auf der Liste stehen."""
    have = set()
    try:
        res = core("POST", "/services/todo/get_items?return_response", {"entity_id": entity, "status": "needs_action"})
        for v in ((res or {}).get("service_response") or {}).values():
            have |= {str(i.get("summary", "")).strip().lower() for i in v.get("items", [])}
    except Exception as e:
        print("Einkaufsliste nicht lesbar:", e, flush=True)
    added = 0
    for item in items:
        if item.strip().lower() in have:
            continue
        core("POST", "/services/todo/add_item", {"entity_id": entity, "item": item})
        have.add(item.strip().lower())
        added += 1
    return added


def saturday_send():
    """Samstag nach der Frist: Einkaufsliste nach HA und Push mit dem Plan, einmal pro Woche."""
    o = load_options()
    if not o.get("samstag_versand", True) or not TOKEN:
        return
    now = datetime.now(TZ)
    nxt = (now + timedelta(days=7 - now.weekday())).date()  # nächster Montag
    week_id = f"{nxt.isocalendar()[0]}-W{nxt.isocalendar()[1]:02d}"
    dl = deadline(week_id)
    if not (dl <= now < dl + timedelta(hours=12)):
        return
    with lock:
        sent = (state["docs"].get("settings/versand") or {}).get("weeks", [])
        if week_id in sent:
            return
        lines, todo = week_summary(week_id)
        state["docs"]["settings/versand"] = {"weeks": (sent + [week_id])[-10:]}
        state["rev"] += 1
        save_store(state)
    added = 0
    if o.get("einkaufsliste") and todo:
        try:
            added = add_todo(o["einkaufsliste"], todo)
        except Exception as e:
            print("Einkaufsliste fehlgeschlagen:", e, flush=True)
    panel = "/" + os.environ.get("HOSTNAME", "").replace("-", "_") if os.environ.get("HOSTNAME") else "/"
    msg = "\n".join(lines) + (f"\n{added} Dinge auf der Einkaufsliste." if added else "")
    for svc in o.get("benachrichtigen", []):
        svc = str(svc).removeprefix("notify.")
        try:
            core("POST", f"/services/notify/{svc}", {"title": f"Essensplan KW {week_id[-2:]} steht", "message": msg,
                                                    "data": {"url": panel, "clickAction": panel}})
        except Exception as e:
            print(f"Push an {svc} fehlgeschlagen:", e, flush=True)
    print(f"Samstags-Versand {week_id}: {added} Einträge, Push an {len(o.get('benachrichtigen', []))}", flush=True)


def scheduler():
    while True:
        try:
            decide_due()
            saturday_send()
        except Exception as e:
            print("Fehler beim Entscheiden:", e, flush=True)
        time.sleep(60)


def load_options():
    try:
        return json.loads(OPTIONS.read_text("utf-8"))
    except Exception:
        return {}


def load_store():
    if STORE.exists():
        try:
            return json.loads(STORE.read_text("utf-8"))
        except Exception:
            STORE.rename(STORE.with_suffix(".broken.json"))
    store = {"rev": 1, "docs": {}}
    try:
        for path, doc in json.loads(SEED.read_text("utf-8")).items():
            store["docs"][path] = doc
    except Exception:
        pass
    return store


def save_store(store):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False), "utf-8")
    tmp.replace(STORE)


state = load_store()
save_store(state)


def core(method, path, body=None):
    if not TOKEN:
        raise RuntimeError("Kein Zugriff auf Home Assistant")
    req = urllib.request.Request(
        CORE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


class Handler(BaseHTTPRequestHandler):
    server_version = "Mahlzeit/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("DEBUG"):
            super().log_message(fmt, *args)

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("Zu groß")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Objekt erwartet")
        return data

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            data = (WWW / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif url.path == "/api/config":
            o = load_options()
            self.send_json({
                "personen": o.get("personen", []),
                "tage": o.get("tage", "mo,di,mi,do"),
                "kinderportion": o.get("kinderportion", 0.6),
                "feste": o.get("feste_abende", []),
                "todo": bool(o.get("einkaufsliste")) and bool(TOKEN),
                "ki": bool(o.get("ki_entity")) and bool(TOKEN),
                "user": self.headers.get("X-Remote-User-Display-Name") or self.headers.get("X-Remote-User-Name") or "",
            })
        elif url.path == "/api/state":
            q = parse_qs(url.query)
            since = q.get("since", [""])[0]
            with lock:
                if since == str(state["rev"]):
                    return self.send_json({"rev": state["rev"], "same": True})
                docs = state["docs"]
                weeks = [w for w in q.get("week", []) if re.fullmatch(r"\d{4}-W\d{2}", w)]
                out = {p: d for p, d in docs.items()
                       if p.startswith(("recipes/", "wishes/", "settings/"))
                       or any(p in (f"plans/{w}", f"shop/{w}") for w in weeks)}
                self.send_json({"rev": state["rev"], "docs": out})
        else:
            self.send_json({"error": "nicht gefunden"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path.removeprefix("/api/doc/")
        if not PATH_RE.match(path):
            return self.send_json({"error": "ungültiger Pfad"}, 400)
        try:
            data = self.body()
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)
        with lock:
            state["docs"][path] = data
            state["rev"] += 1
            save_store(state)
            self.send_json({"rev": state["rev"]})

    def do_DELETE(self):
        path = urlparse(self.path).path.removeprefix("/api/doc/")
        if not PATH_RE.match(path):
            return self.send_json({"error": "ungültiger Pfad"}, 400)
        with lock:
            state["docs"].pop(path, None)
            state["rev"] += 1
            save_store(state)
            self.send_json({"rev": state["rev"]})

    def do_POST(self):
        path = urlparse(self.path).path
        o = load_options()
        try:
            body = self.body()
            if path == "/api/todo":
                entity = o.get("einkaufsliste")
                if not entity:
                    return self.send_json({"error": "Keine Einkaufsliste eingestellt"}, 400)
                items = [str(i)[:200] for i in body.get("items", []) if str(i).strip()][:100]
                return self.send_json({"added": add_todo(entity, items)})
            if path == "/api/ki":
                entity = o.get("ki_entity")
                if not entity:
                    return self.send_json({"error": "Keine KI eingestellt"}, 400)
                res = core("POST", "/services/ai_task/generate_data?return_response",
                           {"task_name": "Mahlzeit-Planer", "entity_id": entity,
                            "instructions": str(body.get("prompt", ""))[:20000]})
                data = (res or {}).get("service_response", {}).get("data")
                return self.send_json({"text": data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)})
        except Exception as e:
            return self.send_json({"error": str(e)}, 502)
        self.send_json({"error": "nicht gefunden"}, 404)


if __name__ == "__main__":
    print(f"Mahlzeit-Planer läuft auf Port {PORT} (HA-Zugriff: {'ja' if TOKEN else 'nein'})", flush=True)
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
