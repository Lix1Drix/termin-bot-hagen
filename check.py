#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Termin-Bot Hagen - Cloud-Version (laeuft auf GitHub Actions, einmal pro Aufruf).
Prueft fuer ausgewaehlte Anliegen, ob ein freier Termin im Zeitfenster liegt,
und schickt bei einem Treffer eine Telegram-Nachricht aufs Handy.

Konfiguration (Datum/Anliegen) steht unten als Konstanten.
Telegram-Token und Chat-ID kommen aus den Umgebungsvariablen (GitHub Secrets).
"""

import os
import re
import sys
import json
import time
import html
from datetime import datetime, timedelta

import requests

# ----------------------------- Einstellungen -------------------------------

# Zeitfenster: Termine AB FROM_DATE (einschliesslich) BIS VOR CUTOFF_DATE.
FROM_DATE   = "16.06.2026"   # Dienstag (zaehlt mit)
CUTOFF_DATE = "20.06.2026"   # Samstag, exklusiv -> letzter Tag = Freitag 19.06.

# Welche Anliegen pruefen (Id, Anzeigename). Ausserbetriebsetzung (5707) ist raus.
CONCERNS = [
    (5704, "Neuzulassung"),
    (5705, "Umschreibung mit Halterwechsel"),
    (5718, "Umschreibung von ausserhalb ohne Halterwechsel"),
    (5719, "Wiederzulassung"),
    (5716, "Importzulassung aus dem EU-Ausland"),
    (5717, "Importzulassung aus einem Nicht-EU-Staat"),
]

# --------------------------- (ab hier Technik) -----------------------------

BASE     = "https://terminvergabe.hagen.de"
BOOK_URL = BASE + "/select2?md=2"
MDT, LOC = "407", "497"
LAT, LONG = "51.351074", "7.567329"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
ALL_IDS = list(range(5704, 5720))   # 5704..5719 (alle Felder muessen mit)


def _clean(v):
    # entfernt Leerzeichen/Zeilenumbrueche UND ein evtl. vorangestelltes BOM (﻿),
    # das beim Setzen der Secrets unter Windows entstehen kann.
    return (v or "").strip("﻿ \t\r\n")


TOKEN = _clean(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
CHAT  = _clean(os.environ.get("TELEGRAM_CHAT_ID", ""))
STATE_FILE = os.environ.get("STATE_FILE", "state/notified.json")
TEST_PING  = _clean(os.environ.get("TEST_PING", "")).lower() in ("1", "true", "yes")

FROM   = datetime.strptime(FROM_DATE, "%d.%m.%Y")
CUTOFF = datetime.strptime(CUTOFF_DATE, "%d.%m.%Y")
LAST_DAY = CUTOFF - timedelta(days=1)

WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]   # deutsche Wochentage
FIRE_EFFECT = "5104841245755180586"               # Telegram-Nachrichteneffekt 🔥


def get_suggest(cid):
    """Fuehrt den 3-Schritt-Ablauf aus und gibt das HTML der Terminseite zurueck."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    try:
        s.get(BOOK_URL, timeout=30)
        cnc = "&".join((f"cnc-{i}=1" if i == cid else f"cnc-{i}=0") for i in ALL_IDS)
        loc_url = f"{BASE}/location?mdt={MDT}&select_cnc=1&{cnc}"
        s.get(loc_url, headers={"Referer": BOOK_URL}, timeout=30)
        r = s.post(loc_url,
                   data={"loc": LOC, "gps_lat": LAT, "gps_long": LONG,
                         "select_location": "Weiter"},
                   headers={"Referer": loc_url}, timeout=30)
        return r.text
    except Exception as e:
        print(f"  [FEHLER] {cid}: {e}")
        return ""


def free_days(html):
    """Liefert Liste (datum, [uhrzeiten]) fuer Tage mit freien Terminen."""
    res = []
    if not html:
        return res
    ms = list(re.finditer(r'<h3[^>]*title="[^"]*?(\d{2}\.\d{2}\.\d{4})"[^>]*>', html))
    for i, m in enumerate(ms):
        start = m.start()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(html)
        section = html[start:end]
        # freie Slots stecken in <form class="suggestion_form">...<button>HH:MM</button>
        times = re.findall(r'class="suggestion_form".*?>(\d{1,2}:\d{2})<', section, re.S)
        if times:
            res.append((datetime.strptime(m.group(1), "%d.%m.%Y"), times))
    return res


def telegram(text, button_url=None, effect_id=None):
    """Sendet eine HTML-Nachricht. Optional mit Buchungs-Button und Effekt.
    Faellt der Effekt aus, wird ohne Effekt erneut gesendet (Nachricht kommt sicher an)."""
    if not TOKEN or not CHAT:
        print("  [WARN] Telegram nicht konfiguriert (TOKEN/CHAT fehlen).")
        return False
    base = {
        "chat_id": CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if button_url:
        base["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "🔥  JETZT BUCHEN  🔥", "url": button_url}]]}
        )
    # 1. Versuch mit Effekt, 2. Versuch ohne (falls Effekt abgelehnt wird)
    attempts = [effect_id, None] if effect_id else [None]
    for eff in attempts:
        d = dict(base)
        if eff:
            d["message_effect_id"] = eff
        try:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                              data=d, timeout=20)
            if r.json().get("ok"):
                return True
            print("  [WARN] Telegram-Antwort:", r.text)
        except Exception as e:
            print("  [WARN] Telegram-Fehler:", e)
    return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_state(keys):
    try:
        d = os.path.dirname(STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(keys), f)
    except Exception as e:
        print("  [WARN] State konnte nicht gespeichert werden:", e)


def main():
    print(f"Pruefe Fenster {FROM:%d.%m.%Y} - {LAST_DAY:%d.%m.%Y} "
          f"({len(CONCERNS)} Anliegen)")
    notified = load_state()
    hits, summary, errors = [], [], 0

    for cid, name in CONCERNS:
        html = get_suggest(cid)
        if re.search(r"Kein(e)? g.ltige[rn]? (Anliegen|Standort|Mandant)", html):
            print(f"  [WARN] {name}: Sitzungs-/Schutzfehler")
            errors += 1
            continue
        fd = free_days(html)
        if not fd:
            summary.append(f"{name}: keine")
        else:
            earliest = min(d for d, _ in fd)
            summary.append(f"{name}: ab {earliest:%d.%m.}")
            for d, times in fd:
                if FROM <= d < CUTOFF:
                    hits.append((name, d, times))
        time.sleep(1.0)

    print("Status | " + " | ".join(summary) +
          (f" | Fehler: {errors}" if errors else ""))

    cur_keys = {f"{n}|{d:%Y-%m-%d}" for n, d, _ in hits}
    new = [(n, d, c) for n, d, c in hits if f"{n}|{d:%Y-%m-%d}" not in notified]

    if new:
        new.sort(key=lambda x: x[1])
        blocks = []
        for n, d, times in new:
            shown = times[:14]
            more = len(times) - len(shown)
            tline = ", ".join(shown) + (f"  …(+{more})" if more > 0 else "")
            blocks.append(f"🚗 <b>{html.escape(n)}</b>\n"
                          f"📅 <b>{WD[d.weekday()]}, {d:%d.%m.%Y}</b> · "
                          f"<b>{len(times)}</b> frei\n"
                          f"🕐 {tline}")
        msg = (
            "‼️🚨 <b>TERMIN-ALARM HAGEN</b> 🚨‼️\n"
            "━━━━━━━━━━━━━━━━━\n"
            "🎯 <b>Ein Termin ist FREI geworden!</b>\n\n"
            + "\n".join(blocks)
            + "\n📍 Freiheitstr. 3, 58119 Hagen\n\n"
            "⏱️ <i>Jede Sekunde zählt – sei schneller als die anderen!</i>\n"
            "━━━━━━━━━━━━━━━━━\n"
            "👇 <b>Sofort sichern:</b>"
        )
        print("TREFFER -> sende Telegram (Feuer-Effekt)")
        telegram(msg, button_url=BOOK_URL, effect_id=FIRE_EFFECT)
    else:
        print("Kein Treffer im Zeitfenster.")

    save_state(cur_keys)

    if TEST_PING:
        telegram("✅ <b>Cloud-Bot Hagen läuft</b>\n\n"
                 f"📅 Überwacht: <b>{FROM:%d.%m.}–{LAST_DAY:%d.%m.%Y}</b>\n"
                 "🔎 Aktueller Status:\n"
                 + html.escape("\n".join(summary)))

    # Wenn ALLE Anliegen blockiert wurden -> als Fehler melden (Action faellt auf)
    if errors == len(CONCERNS):
        print("FEHLER: alle Anliegen blockiert (evtl. Bot-Schutz der Seite).")
        sys.exit(1)


if __name__ == "__main__":
    main()
