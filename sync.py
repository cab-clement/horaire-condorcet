# Robot de synchronisation de l'horaire Condorcet (Pronote Campus, espace invité).
# 1. Ouvre le site, choisit la promotion et le groupe.
# 2. Lit les cours de la semaine affichée (et des suivantes) à partir de leur position à l'écran.
# 3. Écrit horaire.json (historique) et horaire.ics (pour s'abonner dans Calendrier).
# Une capture de chaque étape est enregistrée dans debug/ pour corriger le robot si le site change.
import asyncio, datetime as dt, hashlib, json, os, re, sys, urllib.request, urllib.error
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

URL = os.environ.get("HORAIRE_URL", "https://horaires2026.condorcet.be/invite")
PROMO = os.environ.get("PROMO", "B1-Bac en aérotechnique (Charleroi)")
GROUPE = os.environ.get("GROUPE", "Groupe B")          # ton groupe
AUTRES = [g.strip() for g in os.environ.get("AUTRES_GROUPES", "Groupe A").split(",") if g.strip()]
SEMAINES = int(os.environ.get("SEMAINES", "3"))   # semaine actuelle + suivantes
TZ = ZoneInfo("Europe/Brussels")
OUT = "debug"
os.makedirs(OUT, exist_ok=True)
log = []
MOIS = {m: i + 1 for i, m in enumerate(["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
                                        "août", "septembre", "octobre", "novembre", "décembre"])}
# Noms complets pour les intitulés coupés par le site (« … »)
NOMS = ["Mathématiques appliquées B1.1", "Connaissance des matériaux aéronautiques",
        "Technologie mécanique et métrologie", "Sciences des matériaux et applications B1.1",
        "Technologie avionique", "Mécanique et mécanique des fluides B1.1", "Anglais aéronautique B1.1",
        "Électricité B1.1", "Remédiation"]

def L(msg):
    print(msg); log.append(str(msg))

# ---------------------------------------------------------------- page
GEOM_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const box = e => { const r = e.getBoundingClientRect();
    return {x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height}; };
  const out = {days: [], hours: [], blocks: [], ruler: []};
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e)) continue;
    const own = [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
    const t = (e.innerText || '').trim();
    if (/^(lun|mar|mer|jeu|ven|sam|dim)\.?\s+\d{1,2}\s+[a-zéû]+$/i.test(t) && e.children.length === 0)
      out.days.push({t, ...box(e)});
    else if (/^\d{1,2}h\d{2}$/.test(t) && e.children.length === 0)
      out.hours.push({t, ...box(e)});
    else if (/^(\d{1,2}|F)$/.test(t) && e.children.length === 0)
      out.ruler.push({t, bg: getComputedStyle(e).backgroundColor,
        pbg: getComputedStyle(e.parentElement).backgroundColor, ...box(e)});
    if (t && /(^|\n)\s*(M|Mme|Mlle)\s+[A-ZÀ-Ý]/.test(t) && /\n/.test(t))
      out.blocks.push({t, title: e.title || '', ...box(e)});
  }
  return out;
}"""

async def dump(page, name):
    try:
        await page.screenshot(path=f"{OUT}/{name}.png", full_page=True)
        with open(f"{OUT}/{name}.html", "w", encoding="utf-8") as f:
            f.write(await page.content())
    except Exception as e:
        L(f"capture {name}: {e!r}")

VOILE = ".BloquerInterface:visible, [class*='Voile']:visible"

async def degager(page, etape):
    # Pronote pose un voile gris pendant le chargement ou quand une fenêtre de message est ouverte
    for essai in range(6):
        if not await page.locator(VOILE).count():
            return True
        await page.wait_for_timeout(2500)
        if not await page.locator(VOILE).count():
            return True
        if essai == 0:
            await dump(page, f"voile_{etape}")
        for txt in ["Fermer", "OK", "J'ai compris", "Accepter", "Valider", "Continuer", "Ignorer"]:
            b = page.get_by_role("button", name=re.compile("^" + re.escape(txt), re.I))
            for j in range(await b.count()):
                try:
                    if await b.nth(j).is_visible():
                        await b.nth(j).click(timeout=2000)
                        L(f"fenêtre fermée avec « {txt} »")
                except Exception:
                    pass
        croix = page.locator("[title*='ermer' i]:visible, [aria-label*='ermer' i]:visible")
        for j in range(await croix.count()):
            try:
                await croix.nth(j).click(timeout=2000)
                L("fenêtre fermée avec la croix")
            except Exception:
                pass
        await page.keyboard.press("Escape")
    L(f"voile toujours présent ({etape})")
    return False

# Il existe plusieurs promotions « B1-Bac en aérotechnique… » : il faut exactement celle de Charleroi
PROMO_RE = re.compile(r"^\s*B1\W*Bac\s+en\s+a.rotechnique\s*\(Charleroi\)\s*$", re.I)
PROMO_TAPE = os.environ.get("PROMO_TAPE", "aérotechnique (Charleroi)")

async def lister_suggestions(page):
    textes = await page.evaluate("""() => [...document.querySelectorAll('body *')]
      .filter(e => e.children.length === 0 && e.offsetParent !== null && /rotechnique/i.test(e.textContent || ''))
      .map(e => e.textContent.trim()).slice(0, 30)""")
    L(f"suggestions visibles : {textes}")

async def cliquer_promo(page, c):
    await lister_suggestions(page)
    sugg = page.get_by_text(PROMO_RE)
    for j in range(await sugg.count() - 1, -1, -1):
        el = sugg.nth(j)
        try:
            if not await el.is_visible() or await el.evaluate("e => e.tagName === 'INPUT'"):
                continue
            await degager(page, "suggestions")
            try:
                await el.click(timeout=5000)
            except Exception:
                await el.dispatch_event("click")
            return True
        except Exception as e:
            L(f"clic suggestion: {e!r}")
    return False

async def promo_ok(page, c):
    try:
        val = await c.input_value()
    except Exception:
        val = ""
    L(f"promotion affichée : {val!r}")
    return bool(PROMO_RE.match(val)) or ("charleroi" in val.lower() and val.strip().upper().startswith("B1"))

async def choisir_promo(page):
    await degager(page, "accueil")
    champs = page.locator("input[aria-label*='promotion' i]:visible")
    if not await champs.count():
        champs = page.locator("input:visible")
    for i in range(await champs.count()):
        c = champs.nth(i)
        try:
            for texte in [PROMO_TAPE, "Charleroi", "aérotechnique"]:
                await c.focus()
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await c.type(texte, delay=80)
                await page.wait_for_timeout(3500)
                await dump(page, f"02_frappe_{texte[:12]}")
                ok = await cliquer_promo(page, c)
                if not ok:
                    for touche in ["ArrowDown", "Enter"]:
                        await c.focus()
                        await page.keyboard.press(touche)
                        await page.wait_for_timeout(3000)
                        ok = await cliquer_promo(page, c)
                        if ok:
                            break
                if ok:
                    await page.wait_for_timeout(5000)
                    await degager(page, "promo")
                    if await promo_ok(page, c):
                        L(f"promotion choisie en tapant {texte!r}")
                        return await c.bounding_box()
                    L("mauvaise promotion, nouvel essai")
                await page.keyboard.press("Escape")
                await degager(page, "promo_essai")
        except Exception as e:
            L(f"champ {i}: {e!r}")
    return None

async def cliquer_option_groupe(page, nom):
    motif = re.compile(r"\b" + re.escape(nom) + r"\b", re.I)
    for loc in [page.get_by_role("option", name=motif), page.get_by_role("menuitem", name=motif),
                page.get_by_role("treeitem", name=motif), page.get_by_text(motif)]:
        for j in range(await loc.count() - 1, -1, -1):
            el = loc.nth(j)
            try:
                if not await el.is_visible() or await el.evaluate("e => e.tagName === 'INPUT'"):
                    continue
                try:
                    await el.click(timeout=4000)
                except Exception:
                    await el.dispatch_event("click")
                return True
            except Exception as e:
                L(f"clic option groupe: {e!r}")
    return False

async def choisir_groupe(page, promo_box, nom):
    await page.wait_for_timeout(3000)
    await degager(page, "groupe")
    # 1) le menu porte souvent une étiquette « groupe » ; 2) sinon un menu déroulant ; 3) sinon ce qui est à droite de la promo
    ouvreurs = []
    for sel in ["[id$='Instances[1].Instances[2].bouton_Edit']:visible",
                "[id$='Instances[1].Instances[2]_wrapperbouton']:visible",
                "[aria-label*='groupe' i]:visible", "[title*='groupe' i]:visible",
                "[role=combobox]:visible", "[aria-haspopup]:visible"]:
        loc = page.locator(sel)
        for i in range(await loc.count()):
            ouvreurs.append(loc.nth(i))
    if promo_box:
        cy = promo_box["y"] + promo_box["height"] / 2
        els = page.locator("div:visible, input:visible")
        cands = []
        for i in range(min(await els.count(), 3000)):
            bb = await els.nth(i).bounding_box()
            if bb and abs(bb["y"] + bb["height"] / 2 - cy) < 15 and bb["x"] > promo_box["x"] + promo_box["width"] \
                    and 100 < bb["width"] < 900 and bb["height"] < 70:
                cands.append((bb["x"], -bb["width"], i))
        ouvreurs += [els.nth(i) for _, _, i in sorted(cands)[:6]]
    vus = set()
    for k, el in enumerate(ouvreurs[:20]):
        try:
            info = await el.evaluate("e => (e.id || '') + ' | ' + (e.getAttribute('aria-label') || '') + ' | ' + (e.innerText || e.value || '').trim().slice(0, 60)")
            if info in vus:
                continue
            vus.add(info)
            L(f"menu {k} : {info}")
            if "promotion" in info.lower() or "Instances[0]" in info or "Instances[1].Instances[1]" in info:
                continue
            try:
                await el.click(timeout=4000)
            except Exception:
                await el.focus()
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(2500)
            await dump(page, f"04_menu_{k}")
            ok = await cliquer_option_groupe(page, nom)
            if not ok:
                for touche in ["ArrowDown", "Alt+ArrowDown", "Space"]:
                    await el.focus()
                    await page.keyboard.press(touche)
                    await page.wait_for_timeout(2000)
                    ok = await cliquer_option_groupe(page, nom)
                    if ok:
                        break
            if ok:
                await page.wait_for_timeout(5000)
                await degager(page, "apres_groupe")
                L(f"{nom} choisi (menu {k})")
                return True
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(800)
        except Exception as e:
            L(f"menu {k}: {e!r}")
    return False

# ---------------------------------------------------------------- lecture de la grille
def hmin(t):
    h, m = t.split("h"); return int(h) * 60 + int(m)

def date_of(label, today):
    m = re.match(r"^\w+\.?\s+(\d{1,2})\s+([a-zéû]+)$", label.strip(), re.I)
    d, mois = int(m.group(1)), MOIS[m.group(2).lower()]
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            c = dt.date(y, mois, d)
        except ValueError:
            continue
        if best is None or abs((c - today).days) < abs((best - today).days):
            best = c
    return best

def nom_complet(c):
    c = c.strip()
    if c.endswith(("...", "…")):
        p = c.rstrip(".… ").lower()
        for n in NOMS:
            if n.lower().startswith(p):
                return n
    return c

def lire_grille(g, today):
    days = sorted(g["days"], key=lambda d: d["x"])
    if not days:
        raise RuntimeError("en-têtes des jours introuvables")
    col_w = (days[-1]["x"] - days[0]["x"]) / max(1, len(days) - 1) if len(days) > 1 else 300
    centers = [(d["x"] + d["w"] / 2, date_of(d["t"], today)) for d in days]
    hours = sorted({h["t"]: h["y"] + h["h"] / 2 for h in g["hours"] if h["x"] < days[0]["x"]}.items(),
                   key=lambda kv: kv[1])
    hours = [(hmin(t), y) for t, y in hours]
    if len(hours) < 4:
        raise RuntimeError("heures de la grille introuvables")

    def y2min(y):
        for (m1, y1), (m2, y2) in zip(hours, hours[1:]):
            if y <= y2:
                v = m1 + (y - y1) * (m2 - m1) / max(1e-6, (y2 - y1))
                break
        else:
            (m1, y1), (m2, y2) = hours[-2], hours[-1]
            v = m2 + (y - y2) * (m2 - m1) / max(1e-6, (y2 - y1))
        near = min(hours, key=lambda h: abs(h[0] - v))[0]
        return near if abs(near - v) <= 7 else int(round(v / 5) * 5)

    # garder un seul cadre par cours : le plus grand qui reste dans une colonne
    bl = [b for b in g["blocks"] if b["w"] <= col_w * 1.15 and b["h"] < 600 and b["x"] >= days[0]["x"] - col_w]
    bl.sort(key=lambda b: -b["w"] * b["h"])
    kept = []
    for b in bl:
        inside = any(b["x"] >= k["x"] - 2 and b["y"] >= k["y"] - 2 and b["x"] + b["w"] <= k["x"] + k["w"] + 2
                     and b["y"] + b["h"] <= k["y"] + k["h"] + 2 for k in kept)
        if not inside:
            kept.append(b)
    events = []
    for b in kept:
        cx = b["x"] + b["w"] / 2
        date = min(centers, key=lambda c: abs(c[0] - cx))[1]
        lignes = [l.strip() for l in (b["t"] or "").split("\n")
                  if l.strip() and not l.strip().startswith("<") and not re.fullmatch(r"\d{1,2}", l.strip())
                  and not re.match(r"(?i)^(ouverture|ouvrir|afficher|fermeture) (des|du|le|la) d.tails", l.strip())]
        prof = next((l for l in lignes if re.match(r"^(M|Mme|Mlle)\s", l)), "")
        local = next((l for l in lignes if re.match(r"^[A-Z]{2,5}\s+-\s+", l)), "")
        reste = [l for l in lignes if l not in (prof, local)]
        if not reste:
            continue
        cours = nom_complet(reste[0])
        notes = " ".join(reste[1:])
        site, salle = (local.split(" - ", 1) + [""])[:2] if local else ("", "")
        kind = "labo" if "labo" in notes.lower() else "remediation" if "remédiation" in cours.lower() else \
               "examen" if re.search(r"examen|interro", (cours + notes).lower()) else "cours"
        s, e = y2min(b["y"]), y2min(b["y"] + b["h"])
        if e <= s:
            continue
        events.append({"date": date.isoformat(), "start": f"{s // 60:02d}:{s % 60:02d}",
                       "end": f"{e // 60:02d}:{e % 60:02d}", "course": cours, "teacher": prof,
                       "room": salle.strip(), "site": site.strip(), "kind": kind, "note": notes})
    monday = min(c[1] for c in centers)
    monday = monday - dt.timedelta(days=monday.weekday())
    return monday.isoformat(), sorted(events, key=lambda e: (e["date"], e["start"]))

async def semaine_suivante(page):
    g = await page.evaluate(GEOM_JS)
    r = g["ruler"]
    if not r:
        return False
    # la réglette des semaines = la ligne qui contient le plus de cases
    rows = {}
    for c in r:
        rows.setdefault(round(c["y"] / 10), []).append(c)
    row = sorted(max(rows.values(), key=len), key=lambda c: c["x"])
    colors = [c["bg"] if c["bg"] not in ("rgba(0, 0, 0, 0)", "transparent") else c["pbg"] for c in row]
    uniq = [i for i, col in enumerate(colors) if colors.count(col) == 1]
    if not uniq:
        L("semaine affichée non repérée dans la réglette")
        return False
    i = uniq[0]
    if i + 1 >= len(row):
        return False
    nxt = row[i + 1]
    sx, sy = await page.evaluate("[scrollX, scrollY]")
    await degager(page, "reglette")
    await page.mouse.click(nxt["x"] + nxt["w"] / 2 - sx, nxt["y"] + nxt["h"] / 2 - sy)
    await page.wait_for_timeout(5000)
    await degager(page, "semaine")
    return True

# ---------------------------------------------------------------- calendrier
def ics(events):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//horaire-condorcet//FR", "CALSCALE:GREGORIAN",
           "METHOD:PUBLISH", "X-WR-CALNAME:Horaire B1 Aéro", "X-WR-TIMEZONE:Europe/Brussels",
           "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    esc = lambda s: s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    for e in events:
        d = dt.date.fromisoformat(e["date"])
        def u(t):
            h, m = map(int, t.split(":"))
            return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        uid = hashlib.sha1(f"{e['date']}{e['start']}{e['course']}".encode()).hexdigest()[:16]
        titre = e["course"] + (" (labo)" if e["kind"] == "labo" else "")
        desc = "\n".join(x for x in [e["teacher"], e["note"]] if x)
        out += ["BEGIN:VEVENT", f"UID:{uid}@horaire-condorcet", f"DTSTAMP:{now}", f"DTSTART:{u(e['start'])}",
                f"DTEND:{u(e['end'])}", f"SUMMARY:{esc(titre)}", f"LOCATION:{esc(e['room'])}",
                f"DESCRIPTION:{esc(desc)}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


# ---------------------------------------------------------------- envoi vers Notion (pour l'appli Horaire B1 Aéro)
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_PAGE = os.environ.get("NOTION_PAGE", "3f713405911081379ac4f532b0005dd2").replace("-", "").strip()

def notion(method, path, body=None):
    req = urllib.request.Request("https://api.notion.com/v1/" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + NOTION_TOKEN,
                                          "Notion-Version": "2022-06-28", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")

def envoyer_notion(data, today):
    if not NOTION_TOKEN:
        L("Notion : pas de clé NOTION_TOKEN, envoi ignoré")
        return
    debut = (today - dt.timedelta(days=today.weekday() + 7)).isoformat()
    utile = {"updated": data["updated"], "weeks": {k: v for k, v in data["weeks"].items() if k >= debut}}
    txt = json.dumps(utile, ensure_ascii=False, separators=(",", ":"))
    morceaux = [{"type": "text", "text": {"content": txt[i:i + 1900]}} for i in range(0, len(txt), 1900)][:100]
    bloc = {"rich_text": morceaux, "language": "json"}
    try:
        enfants = notion("GET", f"blocks/{NOTION_PAGE}/children?page_size=100").get("results", [])
        code = next((b for b in enfants if b.get("type") == "code"), None)
        if code:
            notion("PATCH", f"blocks/{code['id']}", {"code": bloc})
        else:
            notion("PATCH", f"blocks/{NOTION_PAGE}/children", {"children": [{"object": "block", "type": "code", "code": bloc}]})
        L(f"Notion : page mise à jour ({len(txt)} caractères)")
    except urllib.error.HTTPError as e:
        L(f"Notion : erreur {e.code} {e.read()[:300]!r} (la page est-elle connectée à l'intégration ?)")
    except Exception as e:
        L(f"Notion : {e!r}")

# ---------------------------------------------------------------- principal
AUTRE = "Groupe A" if GROUPE.strip().upper().endswith("B") else "Groupe B"

async def ouvrir(ctx, nom):
    page = await ctx.new_page()
    try:
        rep = await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        L(f"[{nom}] page chargée : statut {rep.status if rep else '?'}")
    except Exception as e:
        L(f"[{nom}] chargement de la page : {e!r}")
    try:
        await page.wait_for_selector("input:visible", timeout=60000)
    except Exception:
        L(f"[{nom}] aucun champ visible après 60 s")
    await page.wait_for_timeout(4000)
    await dump(page, f"01_accueil_{nom}")
    return page

async def lire_vue(ctx, groupe, today):
    """Lit SEMAINES semaines pour une vue : groupe=None (horaire commun + tout), ou 'Groupe A'/'Groupe B'."""
    nom = (groupe or "commun").replace(" ", "_")
    page = await ouvrir(ctx, nom)
    box = await choisir_promo(page)
    if not box:
        raise RuntimeError(f"[{nom}] promotion introuvable")
    if groupe and not await choisir_groupe(page, box, groupe):
        raise RuntimeError(f"[{nom}] {groupe} introuvable dans le menu")
    res, vu = {}, None
    for n in range(SEMAINES):
        await dump(page, f"04_{nom}_semaine_{n}")
        # attendre que les cours soient vraiment affichés (le site peut être lent)
        g = await page.evaluate(GEOM_JS)
        for _ in range(8):
            if g["blocks"]:
                break
            await page.wait_for_timeout(2500)
            await degager(page, f"{nom}_attente")
            g = await page.evaluate(GEOM_JS)
        with open(f"{OUT}/{nom}_semaine_{n}.json", "w", encoding="utf-8") as f:
            json.dump(g, f, ensure_ascii=False, indent=1)
        ws, evs = lire_grille(g, today)
        if ws == vu:
            L(f"[{nom}] la semaine n'a pas changé après le clic, arrêt")
            break
        vu = ws
        res[ws] = evs
        L(f"[{nom}] semaine du {ws} : {len(evs)} séances")
        if n + 1 < SEMAINES and not await semaine_suivante(page):
            L(f"[{nom}] pas de semaine suivante accessible")
            break
    await page.close()
    return res

def mm(t):
    h, m = map(int, t.split(":")); return h * 60 + m

def chevauche(a, b):
    return a["date"] == b["date"] and mm(a["start"]) < mm(b["end"]) and mm(b["start"]) < mm(a["end"])

def meme(a, b):
    return a["date"] == b["date"] and a["start"] == b["start"] and a["course"][:12].lower() == b["course"][:12].lower()

def fusion(commun, mien, autre):
    """Horaire final = séances de mon groupe + séances communes.
    Une séance de la vue « sans groupe » est retirée si elle chevauche une séance de mon groupe
    (elle est remplacée par la mienne) ou si c'est une séance de l'autre groupe."""
    out = []
    for e in mien:
        out.append({**e, "groupe": GROUPE})
    for e in commun:
        if any(chevauche(e, m) for m in mien):
            continue
        if any(meme(e, o) for o in autre):
            continue
        out.append({**e, "groupe": ""})
    return sorted(out, key=lambda e: (e["date"], e["start"]))

async def main():
    today = dt.datetime.now(TZ).date()
    try:
        data = json.load(open("horaire.json", encoding="utf-8"))
    except Exception:
        data = {"weeks": {}}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(locale="fr-BE", timezone_id="Europe/Brussels",
                                        viewport={"width": 1600, "height": 1300})
        try:
            commun = await lire_vue(ctx, None, today)
            mien = await lire_vue(ctx, GROUPE, today)
            try:
                autre = await lire_vue(ctx, AUTRE, today)
            except Exception as e:
                L(f"vue {AUTRE} non lue ({e!r}) : seules les séances qui chevauchent les tiennes seront retirées")
                autre = {}
        finally:
            await browser.close()

    lues = {}
    for ws in sorted(set(commun) | set(mien)):
        lues[ws] = fusion(commun.get(ws, []), mien.get(ws, []), autre.get(ws, []))
        L(f"semaine du {ws} : {len(lues[ws])} séances au final "
          f"({len(mien.get(ws, []))} de ton groupe)")
    if not lues or not any(lues.values()):
        L("ÉCHEC : aucune séance lue sur toutes les semaines, l'ancien horaire est gardé"); return 1

    for ws, evs in lues.items():
        if not evs and data["weeks"].get(ws):
            L(f"semaine du {ws} lue vide alors qu'elle avait des cours : ancienne version gardée")
            continue
        data["weeks"][ws] = evs
    limite = (today - dt.timedelta(days=60)).isoformat()
    data["weeks"] = {k: v for k, v in sorted(data["weeks"].items()) if k >= limite}
    data["updated"] = dt.datetime.now(TZ).isoformat(timespec="minutes")
    with open("horaire.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    tous = [e for w in data["weeks"].values() for e in w]
    with open("horaire.ics", "w", encoding="utf-8", newline="") as f:
        f.write(ics(tous))
    L(f"OK : {len(tous)} séances dans le calendrier")
    envoyer_notion(data, today)
    return 0

if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except Exception as e:
        L(f"ERREUR : {e!r}")
        code = 1
    with open(f"{OUT}/journal.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(log))
    sys.exit(code)
