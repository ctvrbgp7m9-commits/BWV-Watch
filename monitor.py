#!/usr/bin/env python3
"""
bwv-watch - Ueberwacht bwv-muenchen.de darauf, ob die Bewerbung um
Mitgliedschaft / Wohnung wieder moeglich ist.

Aufruf:  python monitor.py
Test:    python monitor.py --test-notify
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

STATE_FILE = Path(os.environ.get("BWV_STATE_FILE", "state.json"))
HEARTBEAT_DAYS = 7          # Lebenszeichen-Intervall
TIMEOUT = 25
RETRIES = 3

USER_AGENT = os.environ.get(
    "BWV_USER_AGENT",
    "bwv-watch/1.0 (privater Verfuegbarkeits-Check; Kontakt: "
    + os.environ.get("BWV_CONTACT_EMAIL", "kontakt@example.com")
    + ")",
)

# mode "strict": jede Textaenderung meldet (Seite ist sonst statisch)
# mode "keywords": nur Keyword-/Formular-Treffer melden (Seite aendert sich oft)
PAGES = {
    "Interessenten": {
        "url": "https://bwv-muenchen.de/service/interessenten",
        "mode": "strict",
    },
    "Wohnungsangebote": {
        "url": "https://bwv-muenchen.de/aktuelles/wohnungsangebote",
        "mode": "keywords",
    },
    "Formulare & Downloads": {
        "url": "https://bwv-muenchen.de/service/formulare-downloads",
        "mode": "strict",
    },
    "Startseite": {
        "url": "https://bwv-muenchen.de/",
        "mode": "keywords",
    },
}

# Solange diese Formulierungen dastehen, ist ziemlich sicher zu.
CLOSED_MARKERS = [
    "aufnahmestopp",
    "kontingent ist derzeit",
    "ausgeschoepft",
    "bis auf weiteres nicht",
    "aktuell nicht moeglich",
    "nicht moeglich",
]

# Tauchen diese auf, ist mit hoher Wahrscheinlichkeit offen.
OPEN_MARKERS = [
    "jetzt bewerben",
    "bewerbung moeglich",
    "wieder moeglich",
    "bewerbungsbogen",
    "bewerbungsportal",
    "online-bewerbung",
    "online bewerben",
    "mitglied werden",
    "aufnahmeantrag",
    "interessentenbogen",
    "interessentenformular",
    "registrierung",
    "registrieren",
    "anmeldeportal",
    "warteliste",
]

# Verdaechtige Ziele in neuen Links (neues Portal liegt oft auf Subdomain)
LINK_HINTS = [
    "bewerb", "mitglied", "regist", "anmeld", "interessent",
    "portal", "aufnahme", "formular",
]

UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def norm(text: str) -> str:
    """Kleinschreibung, Umlaute aufloesen, Whitespace normalisieren."""
    text = text.lower()
    for k, v in UMLAUTS.items():
        text = text.replace(k, v)
    return re.sub(r"\s+", " ", text).strip()


def fetch(url: str) -> str:
    last = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "de-DE,de;q=0.9",
                },
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return r.text
        except Exception as exc:          # noqa: BLE001
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Abruf fehlgeschlagen: {url} -> {last}")


def extract(html: str, base_url: str) -> dict:
    """Relevanten Inhalt aus dem HTML herausloesen."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    # Navigation/Footer entfernen -> weniger Rauschen
    main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    for tag in main.find_all(["nav", "header", "footer"]):
        tag.decompose()

    text = norm(main.get_text(" "))

    links = sorted({
        requests.compat.urljoin(base_url, a["href"]).split("#")[0]
        for a in main.find_all("a", href=True)
        if not a["href"].startswith(("mailto:", "tel:", "javascript:"))
    })

    forms = len(main.find_all("form"))

    return {
        "text": text,
        "hash": hashlib.sha256(text.encode()).hexdigest()[:16],
        "links": links,
        "forms": forms,
    }


def analyse(name: str, cfg: dict, cur: dict, prev: dict | None) -> list[str]:
    """Gibt eine Liste von Alarmgruenden zurueck (leer = alles unveraendert)."""
    reasons: list[str] = []
    text = cur["text"]

    closed_now = [m for m in CLOSED_MARKERS if m in text]
    open_now = [m for m in OPEN_MARKERS if m in text]

    if prev is None:
        return []  # erster Lauf: nur Baseline speichern

    closed_before = [m for m in CLOSED_MARKERS if m in prev.get("text", "")]
    open_before = [m for m in OPEN_MARKERS if m in prev.get("text", "")]

    # 1) Sperrhinweis ist verschwunden -> staerkstes Signal
    if closed_before and not closed_now:
        reasons.append("Der Hinweis auf den Aufnahmestopp ist VERSCHWUNDEN.")

    # 2) Neue Bewerbungs-/Registrierungs-Formulierung
    neu = set(open_now) - set(open_before)
    if neu:
        reasons.append("Neue Schluesselwoerter im Text: " + ", ".join(sorted(neu)))

    # 3) Neues Formular auf der Seite
    if cur["forms"] > prev.get("forms", 0):
        reasons.append(
            f"Neues Formular gefunden ({prev.get('forms', 0)} -> {cur['forms']})."
        )

    # 4) Neue Links, die nach Bewerbung/Portal aussehen
    new_links = [l for l in cur["links"] if l not in set(prev.get("links", []))]
    hot = [l for l in new_links if any(h in l.lower() for h in LINK_HINTS)]
    if hot:
        reasons.append("Neue verdaechtige Links: " + " | ".join(hot[:5]))

    # 5) Fallback: bei statischen Seiten jede Textaenderung melden
    if not reasons and cfg["mode"] == "strict" and cur["hash"] != prev.get("hash"):
        reasons.append("Der Seitentext hat sich geaendert (Details pruefen).")

    # Bei 'keywords'-Seiten zusaetzlich neue Wohnungsangebote melden
    if cfg["mode"] == "keywords" and name == "Wohnungsangebote":
        if new_links and cur["hash"] != prev.get("hash"):
            reasons.append(f"{len(new_links)} neue(r) Eintrag/Links im Angebotsbereich.")

    return reasons


# --------------------------------------------------------------------------
# Benachrichtigung
# --------------------------------------------------------------------------

def notify(title: str, body: str, url: str = "", priority: str = "high") -> None:
    sent = False

    # ntfy.sh - ohne Account, App installieren + Topic abonnieren
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
        headers = {
            "Title": title.encode("utf-8"),
            "Priority": "urgent" if priority == "high" else "default",
            "Tags": "house,rotating_light" if priority == "high" else "heartbeat",
        }
        if url:
            headers["Click"] = url
        tok = os.environ.get("NTFY_TOKEN")
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        try:
            requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                          headers=headers, timeout=15).raise_for_status()
            sent = True
        except Exception as exc:                      # noqa: BLE001
            print(f"[warn] ntfy fehlgeschlagen: {exc}", file=sys.stderr)

    # Telegram
    tg_token = os.environ.get("TELEGRAM_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        try:
            requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={
                    "chat_id": tg_chat,
                    "text": f"*{title}*\n\n{body}\n\n{url}",
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
                timeout=15,
            ).raise_for_status()
            sent = True
        except Exception as exc:                      # noqa: BLE001
            print(f"[warn] Telegram fehlgeschlagen: {exc}", file=sys.stderr)

    # E-Mail via SMTP
    if os.environ.get("SMTP_HOST"):
        import smtplib
        from email.message import EmailMessage
        try:
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = os.environ["SMTP_FROM"]
            msg["To"] = os.environ["SMTP_TO"]
            msg.set_content(f"{body}\n\n{url}")
            port = int(os.environ.get("SMTP_PORT", 587))
            with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30) as s:
                s.starttls()
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
            sent = True
        except Exception as exc:                      # noqa: BLE001
            print(f"[warn] SMTP fehlgeschlagen: {exc}", file=sys.stderr)

    if not sent:
        print(f"[warn] KEIN Kanal konfiguriert!\n{title}\n{body}", file=sys.stderr)


# --------------------------------------------------------------------------
# Hauptlauf
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"pages": {}, "last_heartbeat": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-notify", action="store_true",
                    help="Nur eine Testnachricht senden")
    args = ap.parse_args()

    if args.test_notify:
        notify("bwv-watch Test", "Wenn du das liest, funktioniert der Kanal.",
               "https://bwv-muenchen.de/service/interessenten", "high")
        return 0

    state = load_state()
    now = datetime.now(timezone.utc)
    alarms: list[str] = []
    errors: list[str] = []

    for name, cfg in PAGES.items():
        try:
            cur = extract(fetch(cfg["url"]), cfg["url"])
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"{name}: {exc}")
            continue

        prev = state["pages"].get(name)
        reasons = analyse(name, cfg, cur, prev)

        if reasons:
            alarms.append(f"[{name}]\n" + "\n".join(f"  - {r}" for r in reasons)
                          + f"\n  {cfg['url']}")
            print(f"AENDERUNG {name}: {reasons}")
        else:
            print(f"ok  {name}  hash={cur['hash']}")

        state["pages"][name] = {
            "text": cur["text"][:20000],
            "hash": cur["hash"],
            "links": cur["links"],
            "forms": cur["forms"],
            "checked": now.isoformat(timespec="seconds"),
        }

    if alarms:
        notify(
            "bwv Muenchen: Aenderung erkannt!",
            "Auf der bwv-Website hat sich etwas geaendert.\n"
            "Sofort pruefen, ob eine Bewerbung/Mitgliedschaft moeglich ist!\n\n"
            + "\n\n".join(alarms),
            "https://bwv-muenchen.de/service/interessenten",
            "high",
        )

    # Lebenszeichen: sonst merkst du nie, wenn der Waechter still gestorben ist
    last_hb = state.get("last_heartbeat")
    due = (
        last_hb is None
        or datetime.fromisoformat(last_hb) < now - timedelta(days=HEARTBEAT_DAYS)
    )
    if due:
        notify(
            "bwv-watch laeuft (Lebenszeichen)",
            f"Stand {now:%d.%m.%Y %H:%M} UTC - alle Seiten erreichbar, "
            "keine Oeffnung erkannt.",
            "https://bwv-muenchen.de/service/interessenten",
            "low",
        )
        state["last_heartbeat"] = now.isoformat(timespec="seconds")

    if errors:
        print("[warn] " + "; ".join(errors), file=sys.stderr)
        # Dauerfehler faellt beim Lebenszeichen auf; einzelne Timeouts ignorieren

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
