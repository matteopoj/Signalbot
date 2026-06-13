# ============================================================
#  SignalBot Scanner — tourne sur GitHub Actions (gratuit)
#  Alertes Telegram autonomes : app fermée, téléphone éteint.
#
#  ⚠️ NE JAMAIS écrire le token Telegram ici (dépôt public !)
#     Il vient des "Secrets" GitHub : TELEGRAM_TOKEN et CHAT_ID.
#
#  👉 PERSONNALISE LES 3 BLOCS CI-DESSOUS (crayon ✏️ sur GitHub)
# ============================================================
import json, os
from datetime import datetime, timezone

import requests
import yfinance as yf

# ── 1) TON ARGENT ───────────────────────────────────────────
CAPITAL = 1000.0        # capital en €
RISQUE_PCT = 1.0        # % du capital risqué par opportunité
STOP_PCT = 8.0          # alerte si une position perd X % sous ton PRU
EXPO_MAX = 0.30         # une position ≤ 30 % du capital
SEUIL_SCORE = 5         # opportunité envoyée si score ≥ 5 (sur 8)

# ── 2) TES POSITIONS (ventes de protection UNIQUEMENT ici) ──
POSITIONS = {
    "AI.PA": {"qty": 20, "pru": 171.58},   # Air Liquide — exemple : adapte !
    # "AAPL": {"qty": 2, "pru": 290.50},
}

# ── 3) WATCHLIST (opportunités d'achat) ─────────────────────
WATCHLIST = [
    # 🇺🇸 US
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","JPM","LLY",
    "V","XOM","MA","COST","NFLX","BAC","AMD","KO","WMT","MRK",
    "ORCL","QCOM","CAT","DIS","GS","BA","UBER","PLTR",
    # 🇪🇺 Europe
    "MC.PA","OR.PA","TTE.PA","SAN.PA","AIR.PA","RMS.PA","SU.PA","BNP.PA",
    "AI.PA","SAF.PA","CAP.PA","DG.PA","SAP.DE","BMW.DE","SIE.DE","ASML.AS",
    # 🌏 Asie
    "7203.T","6758.T","7974.T","9984.T","8035.T",
    "0700.HK","9988.HK","1810.HK","005930.KS","000660.KS",
]

COOLDOWN_H = {"opp": 40, "stop": 20, "trail": 20, "drop": 10, "tp": 20, "br": 20}

# ── Telegram ────────────────────────────────────────────────
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("CHAT_ID", "")

def tg(text: str) -> bool:
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          json={"chat_id": CHAT, "text": text}, timeout=10)
        return r.ok
    except Exception as e:
        print("Telegram:", e); return False

# ── État entre deux passages (cooldowns + plus-hauts) ───────
def load_state():
    try:
        return json.load(open("state.json"))
    except Exception:
        return {"cool": {}, "hi": {}}

def save_state(st):
    json.dump(st, open("state.json", "w"))

def cooled(st, key, hours):
    now = datetime.now(timezone.utc).timestamp()
    if now - st["cool"].get(key, 0) < hours * 3600:
        return False
    st["cool"][key] = now
    return True

# ── Devises ─────────────────────────────────────────────────
def cur(sym):
    for suf, c in ((".PA","€"),(".DE","€"),(".AS","€"),(".MI","€"),(".BR","€"),
                   (".T","¥"),(".HK","HK$"),(".KS","₩"),(".L","£")):
        if sym.endswith(suf): return c
    return "$"

def en_dollars_ou_euros(sym):
    return cur(sym) in ("$", "€")

# ── Indicateurs ─────────────────────────────────────────────
def compute(df):
    c = df["Close"].squeeze().dropna()
    if len(c) < 60: return None
    h, l = df["High"].squeeze(), df["Low"].squeeze()
    v = df["Volume"].squeeze().fillna(0)

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + gain / loss)

    e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    hist = macd - macd.ewm(span=9, adjust=False).mean()

    ma20, ma50 = c.rolling(20).mean(), c.rolling(50).mean()
    tr = (h - l).combine((h - c.shift()).abs(), max).combine((l - c.shift()).abs(), max)
    atr = float(tr.rolling(14).mean().iloc[-1])

    vol_avg = float(v.rolling(20).mean().iloc[-2]) if len(v) > 21 else 0
    volx = float(v.iloc[-1]) / vol_avg if vol_avg > 0 else 1.0

    price = float(c.iloc[-1])
    y_hi, y_lo = float(c.max()), float(c.min())
    return dict(price=price, atr=atr, volx=volx,
        r=float(rsi.iloc[-1]), r_prev=float(rsi.iloc[-2]),
        h0=float(hist.iloc[-1]), h1=float(hist.iloc[-2]),
        m20=float(ma20.iloc[-1]), m20p=float(ma20.iloc[-2]),
        m50=float(ma50.iloc[-1]), m50p=float(ma50.iloc[-2]),
        hi50=float(c.iloc[-51:-1].max()),
        dp=float((c.iloc[-1]/c.iloc[-2]-1)*100) if len(c) > 1 else 0.0,
        r52=(price - y_lo) / (y_hi - y_lo) if y_hi > y_lo else None)

DISCLAIMER = "\n\n⚠️ Signal technique automatique — vérifie avant d'agir. Pas un conseil financier."

# ── Moteur d'opportunités (achat) ───────────────────────────
def chasse_opportunite(sym, k, st):
    fx, score = [], 0
    if k["price"] > k["m50"]: score += 1; fx.append("tendance > MA50")
    if k["m20"] > k["m50"]:   score += 1; fx.append("MA20 > MA50")
    rebond = k["r_prev"] < 38 and k["r"] > k["r_prev"] and k["r"] < 55
    cassure = k["price"] > k["hi50"]
    if rebond:  score += 2; fx.append(f"rebond RSI naissant ({k['r_prev']:.0f}→{k['r']:.0f})")
    if cassure: score += 2; fx.append("cassure du plus haut 50 j")
    if k["h0"] > 0 and k["h1"] <= 0: score += 2; fx.append("retournement MACD haussier")
    if k["volx"] >= 1.3: score += 1; fx.append(f"volume ×{k['volx']:.1f}")
    if k["r52"] is not None:
        if rebond and k["r52"] < 0.25:  score += 1; fx.append("proche du plancher annuel")
        if cassure and k["r52"] > 0.85: score += 1; fx.append("proche du sommet annuel")

    if not (rebond or cassure) or score < SEUIL_SCORE: return 0
    if not cooled(st, f"{sym}:opp", COOLDOWN_H["opp"]): return 0

    d = cur(sym)
    stop_d = 1.5 * k["atr"]; stop = k["price"] - stop_d
    cap_qty = (CAPITAL * EXPO_MAX) / k["price"]
    risk_qty = (CAPITAL * RISQUE_PCT / 100) / stop_d if en_dollars_ou_euros(sym) and stop_d > 0 else float("inf")
    qty = max(0.0, min(risk_qty, cap_qty)); n = int(qty)

    msg = (f"🟢 ACHAT 🎯 OPPORTUNITÉ {score}/8\n{sym} — {k['price']:,.2f} {d}\n"
           f"📊 {' · '.join(fx)}\n🛑 Stop : {stop:,.2f} {d} (1,5×ATR)")
    if n >= 1:
        msg += (f"\n📦 Taille conseillée : {n} action(s) — coût ≈ {n*k['price']:,.0f} {d} (≤30% du capital)"
                f"\n📉 Perte si stop touché : ≈ {n*stop_d:,.0f} {d}")
    elif qty > 0:
        msg += f"\n📦 {qty:.2f} action en fraction (si ton courtier le permet) — sinon passe ton tour"
    else:
        msg += "\n📦 Trop volatile pour ton capital — passe ton tour"
    return 1 if tg(msg + DISCLAIMER) else 0

# ── Gardes-fous sur TES positions (vente) ───────────────────
def protege_position(sym, pos, k, st):
    n = 0; d = cur(sym); price = k["price"]; pru = pos["pru"]
    hi = max(st["hi"].get(sym, pru), price); st["hi"][sym] = hi

    if price <= pru * (1 - STOP_PCT/100) and cooled(st, f"{sym}:stop", COOLDOWN_H["stop"]):
        n += tg(f"🔴 VENTE 🛡️ POSITION\n{sym} — {price:,.2f} {d}\n"
                f"🛑 Stop touché : −{STOP_PCT:.0f}% sous ton PRU ({pru:,.2f} {d})" + DISCLAIMER)
    if hi > pru and price <= hi * 0.90 and price > pru and cooled(st, f"{sym}:trail", COOLDOWN_H["trail"]):
        n += tg(f"🔴 VENTE 🛡️ POSITION\n{sym} — {price:,.2f} {d}\n"
                f"📉 Prise de profit : −10% depuis le plus haut ({hi:,.2f}) — gain préservé "
                f"{(price/pru-1)*100:+.1f}%" + DISCLAIMER)
    if k["dp"] <= -4 and cooled(st, f"{sym}:drop", COOLDOWN_H["drop"]):
        n += tg(f"🔴 VENTE 🛡️ POSITION\n{sym} — {price:,.2f} {d}\n"
                f"⚠️ Chute de {k['dp']:.1f}% sur la séance — vérifie l'actualité du titre" + DISCLAIMER)
    if k["r"] > 70 and k["h0"] < 0 and k["h1"] >= 0 and cooled(st, f"{sym}:tp", COOLDOWN_H["tp"]):
        n += tg(f"🔴 VENTE 🛡️ POSITION\n{sym} — {price:,.2f} {d}\n"
                f"📊 RSI {k['r']:.0f} suracheté + essoufflement MACD — envisager d'alléger" + DISCLAIMER)
    if k["m20"] < k["m50"] and k["m20p"] >= k["m50p"] and cooled(st, f"{sym}:br", COOLDOWN_H["br"]):
        n += tg(f"🔴 VENTE 🛡️ POSITION\n{sym} — {price:,.2f} {d}\n"
                f"📊 Cassure de tendance : MA20 passe sous MA50" + DISCLAIMER)
    return n

# ── Passage de scan ─────────────────────────────────────────
def main():
    if not TOKEN or not CHAT:
        print("Secrets TELEGRAM_TOKEN / CHAT_ID manquants"); return
    st = load_state()
    univers = sorted(set(WATCHLIST) | set(POSITIONS))
    print(f"Scan de {len(univers)} titres — {datetime.now():%d/%m %H:%M}")

    data = yf.download(univers, period="1y", interval="1d", group_by="ticker",
                       auto_adjust=True, threads=True, progress=False)
    scanned = signaux = 0
    for sym in univers:
        try:
            df = data[sym].dropna(how="all") if len(univers) > 1 else data
            k = compute(df)
            if not k: continue
            scanned += 1
            if sym in POSITIONS:
                signaux += protege_position(sym, POSITIONS[sym], k, st)
            if sym in WATCHLIST and sym not in POSITIONS:
                signaux += chasse_opportunite(sym, k, st)
        except Exception as e:
            print(sym, "→", e)

    save_state(st)
    print(f"Terminé : {scanned} analysés, {signaux} signal(aux)")
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        tg(f"✅ Scanner opérationnel !\n{scanned} titres analysés, {signaux} signal(aux) à l'instant.\n"
           f"Prochains passages automatiques : toutes les 30 min, jours ouvrés — téléphone éteint compris. 🛰️")

if __name__ == "__main__":
    main()
