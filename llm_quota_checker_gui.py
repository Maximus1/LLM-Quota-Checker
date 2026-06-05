#!/usr/bin/env python3
"""
LLM Quota Checker – GUI + Tray-Icon
====================================
Prüft stündlich alle konfigurierten LLM-Accounts auf verfügbares Kontingent.

Installation:
    pip install pystray pillow requests

Starten:
    python llm_quota_checker_gui.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import json
import os
import time
import requests
import re
from datetime import datetime, timedelta
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid

# ── Optionale Tray-Icon-Abhängigkeiten ───────────
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    print("Tipp: pip install pystray pillow  →  aktiviert das Tray-Icon")

# ─────────────────────────────────────────────────
#  Konstanten & Standardwerte
# ─────────────────────────────────────────────────
APP_NAME     = "LLM Quota Checker"
CONFIG_FILE  = os.path.join(os.path.expanduser("~"), ".llm_quota_checker.json")
DEFAULT_URL  = "https://api.cline.bot/api/v1/chat/completions"
DEFAULT_MINS = 60          # Prüfintervall in Minuten

TEST_PAYLOAD = {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1}


def new_account_id() -> str:
    """Erzeugt eine kurze eindeutige Account-ID."""
    return uuid.uuid4().hex[:8]


def provider_name_from_url(url: str) -> str:
    """Leitet einen lesbaren Provider-Namen aus der URL ab.
    Nutzt urllib.parse fuer sicheres Hostname-Parsing.
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host   = parsed.hostname or ""
        port   = parsed.port
    except Exception:
        return "API"

    def host_matches(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    # Lokale Adressen zuerst (Port als Zusatzinfo)
    if _detect_provider(url) == "local":
        if port == 1234:
            return "LM Studio"
        if port == 11434:
            return "Ollama"
        return "Local"

    # Bekannte Cloud-Provider (exakter Domain-Vergleich)
    KNOWN = [
        ("cline.bot",       "Cline"),
        ("openrouter.ai",   "OpenRouter"),
        ("nvidia.com",      "NVIDIA"),
        ("openai.com",      "OpenAI"),
        ("anthropic.com",   "Anthropic"),
        ("googleapis.com",  "Google"),
        ("together.ai",     "Together AI"),
        ("groq.com",        "Groq"),
        ("mistral.ai",      "Mistral"),
        ("cohere.com",      "Cohere"),
        ("deepseek.com",    "DeepSeek"),
    ]
    for domain, label in KNOWN:
        if host_matches(domain):
            return label

    # Fallback: zweite Subdomain-Ebene als Name
    parts = host.split(".")
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return host.capitalize() or "API"


def ensure_account_ids(accounts: list) -> list:
    """Stellt sicher, dass alle Accounts eine 'id' haben (Migration alter Daten)."""
    for acc in accounts:
        if not acc.get("id"):
            acc["id"] = new_account_id()
    return accounts

# Status: Label, Farbe, Tray-Symbol
STATUS_META = {
    "ok":      ("✔  Verfügbar",      "#22c55e", "✔"),
    "limited": ("✖  Limitiert",      "#ef4444", "✖"),
    "balance": ("💳 Kein Guthaben",  "#a855f7", "💳"),   # HTTP 402
    "error":   ("⚠  Fehler",         "#f59e0b", "⚠"),
    "unknown": ("–  Ungeprüft",      "#94a3b8", "–"),
}

# Gesamtstatus → Icon-Farbe
TRAY_COLORS = {
    "all_ok":  "#22c55e",
    "some_ok": "#f59e0b",
    "none_ok": "#ef4444",
    "unknown": "#64748b",
}

# ─────────────────────────────────────────────────
#  Hilfsfunktionen
# ─────────────────────────────────────────────────

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["accounts"] = ensure_account_ids(cfg.get("accounts", []))
            return cfg
        except Exception:
            pass
    return {"interval_minutes": DEFAULT_MINS, "accounts": []}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def parse_wait_time(message: str):
    """
    Extrahiert die Wartezeit aus Fehlermeldungen.
    Gibt (Anzeigetext, Gesamtsekunden) zurueck.
    """
    msg = str(message)
    m = re.search(r"(\d+)h\s*(\d+)m", msg, re.IGNORECASE)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        return f"{h}h {mn}m", h * 3600 + mn * 60
    m = re.search(r"(\d+)h", msg, re.IGNORECASE)
    if m:
        h = int(m.group(1))
        return f"{h}h", h * 3600
    m = re.search(r"(\d+)m", msg, re.IGNORECASE)
    if m:
        mn = int(m.group(1))
        return f"{mn}m", mn * 60
    m = re.search(r"(\d+)\s*second", msg, re.IGNORECASE)
    if m:
        secs = int(m.group(1))
        label = f"{secs // 60}m" if secs >= 60 else f"{secs}s"
        return label, secs
    return "?", 3600


def remaining_str(retry_until_iso):
    """Gibt die verbleibende Wartezeit als lesbaren String zurueck."""
    if not retry_until_iso:
        return ""
    try:
        until = datetime.fromisoformat(retry_until_iso)
        delta = (until - datetime.now()).total_seconds()
        if delta <= 0:
            return ""
        h  = int(delta // 3600)
        mn = int((delta % 3600) // 60)
        s  = int(delta % 60)
        if h:
            return f"noch {h}h {mn:02d}m"
        if mn:
            return f"noch {mn}m {s:02d}s"
        return f"noch {s}s"
    except Exception:
        return ""



def _strip_html(text: str) -> str:
    """Entfernt HTML-Tags aus Fehlermeldungen (z.B. LM Studio HTTP 500)."""
    import re
    # Titel aus HTML extrahieren falls vorhanden
    m = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    # Alle Tags entfernen
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:150] if clean else text[:150]


def _parse_error_body(resp) -> str:
    """Extrahiert die Fehlermeldung aus verschiedenen API-Antwortformaten."""
    try:
        data = resp.json()
    except Exception:
        text = resp.text[:500]
        # HTML-Antwort (z.B. LM Studio beim Laden eines Modells)
        if text.strip().startswith("<"):
            return _strip_html(text)
        return text[:300]

    # OpenAI-Format: {"error": {"message": "..."}}
    if "error" in data:
        err = data["error"]
        if isinstance(err, dict):
            return err.get("message", str(err))
        return str(err)

    # NVIDIA NIM Format: {"status": 429, "title": "...", "detail": "..."}
    if "title" in data or "detail" in data:
        title  = data.get("title", "")
        detail = data.get("detail", "")
        return f"{title}: {detail}".strip(": ")

    # Fallback: roher Text
    return str(data)[:300]


def check_account(account: dict) -> dict:
    """Sendet eine minimale Test-Anfrage und gibt Statusinformationen zurueck."""
    headers = {
        "Authorization": f"Bearer {account['api_key']}",
        "Content-Type":  "application/json",
    }
    payload = {**TEST_PAYLOAD, "model": account["model"]}
    try:
        resp = requests.post(account["url"], json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            return {"status": "ok", "detail": "", "retry_after_secs": 0}

        body = _parse_error_body(resp)

        if resp.status_code == 429:
            label, secs = parse_wait_time(body)
            # NVIDIA gibt keine Wartezeit – 5 Minuten als Fallback
            if secs == 3600 and "?" not in label:
                pass
            elif label == "?":
                label, secs = "Rate Limit", 300
            return {"status": "limited", "detail": label, "retry_after_secs": secs}

        if resp.status_code == 402:
            short = body.split("\n")[0][:120].strip()
            return {"status": "balance", "detail": short, "retry_after_secs": 0}

        if resp.status_code == 403:
            short = body.split("\n")[0][:120].strip()
            return {"status": "error",
                    "detail": f"Zugriff verweigert (403): {short}", "retry_after_secs": 0}

        return {"status": "error",
                "detail": f"HTTP {resp.status_code}: {body[:80]}", "retry_after_secs": 0}

    except requests.exceptions.Timeout:
        return {"status": "error", "detail": "Timeout (>15 s)", "retry_after_secs": 0}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:100], "retry_after_secs": 0}


def make_tray_image(hex_color: str) -> "Image.Image":
    """Erstellt ein einfarbiges Kreis-Icon für das Tray."""
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    m    = 6
    draw.ellipse([m, m, size - m, size - m], fill=hex_color)
    # Kleines "L" als Erkennungszeichen
    cx, cy = size // 2, size // 2
    draw.line([cx - 7, cy - 9, cx - 7, cy + 7], fill="white", width=4)
    draw.line([cx - 7, cy + 7, cx + 6, cy + 7], fill="white", width=4)
    return img


# ─────────────────────────────────────────────────
#  Modell-Erkennung
# ─────────────────────────────────────────────────

# OpenRouter-API: liefert alle Modelle inkl. Preis (kein Auth noetig)
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"

def _detect_provider(url: str) -> str:
    """Erkennt den API-Anbieter anhand der URL.
    Verwendet urllib.parse fuer sicheres Hostname-Parsing statt
    einfacher Substring-Suche (verhindert Spoofing durch URLs wie
    'evil-nvidia.com' oder 'nvidia.com.attacker.net').
    """
    from urllib.parse import urlparse
    import re
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return "generic"

    # Exakter Hostname-Vergleich: domain muss am Ende stehen (oder gleich sein)
    def host_matches(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    if host_matches("integrate.api.nvidia.com") or host_matches("api.nvidia.com") or host_matches("nvidia.com"):
        return "nvidia"
    if host_matches("openrouter.ai"):
        return "openrouter"

    # Lokale Adressen: Loopback + Link-Local + RFC-1918
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return "local"
    if re.match(r"^127\.\d+\.\d+\.\d+$", host):
        return "local"
    if re.match(r"^192\.168\.\d+\.\d+$", host):
        return "local"
    if re.match(r"^10\.\d+\.\d+\.\d+$", host):
        return "local"
    if re.match(r"^172\.(1[6-9]|2[0-9]|3[01])\.\d+\.\d+$", host):
        return "local"

    return "generic"


def fetch_model_list_from_openrouter(log_fn=None) -> list:
    """
    Laedt die Modellliste von OpenRouter (kein Auth noetig).
    Gibt eine Liste von Dicts zurueck: {id, label, input_price, free, free_suffix}
    """
    if log_fn:
        log_fn(f"-> Lade Modellliste von OpenRouter ...", "info")
    try:
        resp = requests.get(OPENROUTER_MODELS_URL, timeout=20)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json().get("data", [])
        models = []
        for m in data:
            mid = m.get("id", "")
            if not mid:
                continue
            price_str = (m.get("pricing") or {}).get("prompt", "0") or "0"
            try:
                input_price = float(price_str) * 1_000_000
            except (ValueError, TypeError):
                input_price = 0.0
            # Frei = OpenRouter-Preis $0 ODER explizites :free-Suffix
            # Kein Hardcoding – nur Daten aus der API
            has_free_suffix = mid.endswith(":free")
            is_free = has_free_suffix or (input_price == 0.0)
            models.append({
                "id":          mid,
                "label":       m.get("name", mid),
                "input_price": input_price,
                "free":        is_free,
                "free_suffix": has_free_suffix,
            })
        return sorted(models, key=lambda m: (not m["free"], m["id"]))
    except Exception as e:
        if log_fn:
            log_fn(f"Fehler OpenRouter-Modellliste: {e}", "error")
        return []


def fetch_model_list_from_nvidia(api_key: str, log_fn=None) -> list:
    """
    Laedt die Modellliste direkt von NVIDIA NIM (/v1/models).
    Benoetigt einen gueltigen API-Key.
    """
    if log_fn:
        log_fn("-> Lade Modellliste von NVIDIA NIM ...", "info")
    try:
        headers = {"Authorization": f"Bearer {api_key}",
                   "Content-Type": "application/json"}
        resp = requests.get(NVIDIA_MODELS_URL, headers=headers, timeout=20)
        if resp.status_code != 200:
            body = _parse_error_body(resp)
            raise RuntimeError(f"HTTP {resp.status_code}: {body[:120]}")
        data = resp.json().get("data", [])
        models = []
        for m in data:
            mid = m.get("id", "")
            if not mid:
                continue
            # NVIDIA gibt kein Preisfeld – alle als "credits required" kennzeichnen
            models.append({
                "id":          mid,
                "label":       m.get("name", mid) or mid,
                "input_price": -1,          # unbekannt / Credits
                "free":        False,        # NVIDIA nutzt Credits-System
                "free_suffix": False,
            })
        return sorted(models, key=lambda m: m["id"])
    except Exception as e:
        if log_fn:
            log_fn(f"Fehler NVIDIA-Modellliste: {e}", "error")
        return []


def models_url_from(completions_url: str) -> str:
    """Leitet die /v1/models-URL aus einer Completions-URL ab."""
    for suffix in ("/chat/completions", "/completions"):
        if completions_url.endswith(suffix):
            return completions_url[: -len(suffix)] + "/models"
    return completions_url.rstrip("/") + "/models"


def fetch_model_list_from_local(api_key: str, completions_url: str, log_fn=None) -> list:
    """
    Fragt den lokalen /v1/models-Endpoint ab (LM Studio, Ollama, Llamafile, etc.).
    Gibt nur die tatsaechlich geladenen/installierten Modelle zurueck.
    Kein Auth erforderlich bei den meisten lokalen Servern.
    """
    url = models_url_from(completions_url)
    if log_fn:
        log_fn(f"-> Frage lokalen Modell-Endpoint ab: {url}", "info")
    headers = {"Content-Type": "application/json"}
    # API-Key nur mitsenden wenn vorhanden (manche lokale Server ignorieren ihn)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            body = _parse_error_body(resp)
            raise RuntimeError(f"HTTP {resp.status_code}: {body[:120]}")
        data = resp.json()
        # OpenAI-Format: {"data": [...]}  oder direkt eine Liste
        raw = data.get("data", data) if isinstance(data, dict) else data
        models = []
        for m in raw:
            mid = m.get("id", "") if isinstance(m, dict) else str(m)
            if not mid:
                continue
            models.append({
                "id":          mid,
                "label":       m.get("name", mid) if isinstance(m, dict) else mid,
                "input_price": 0.0,    # lokal = kostenlos
                "free":        True,
                "free_suffix": False,
                "local":       True,
            })
        return sorted(models, key=lambda m: m["id"])
    except requests.exceptions.ConnectionError:
        if log_fn:
            log_fn(f"✖ Kein lokaler Server erreichbar unter {url}", "error")
        return []
    except Exception as e:
        if log_fn:
            log_fn(f"✖ Fehler beim Laden der lokalen Modellliste: {e}", "error")
        return []


def get_model_list(extra_ids=None) -> list:
    """
    Gibt Modell-IDs aus OpenRouter zurueck + optionale eigene IDs.
    Kein Fallback auf hardcodierte Listen – wenn OpenRouter nicht erreichbar,
    werden nur die manuell eingetragenen IDs verwendet.
    """
    ids  = []
    seen = set()
    models = fetch_model_list_from_openrouter()
    for m in models:
        ids.append(m["id"])
        seen.add(m["id"])
    for mid in (extra_ids or []):
        mid = mid.strip()
        if mid and mid not in seen:
            ids.append(mid)
            seen.add(mid)
    return ids


def probe_model(api_key: str, model_id: str, completions_url: str) -> dict:
    """Testet ein einzelnes Modell; gibt Status-Dict zurueck."""
    acc = {"api_key": api_key, "model": model_id, "url": completions_url}
    result = check_account(acc)
    return {"model": model_id, **result}


# ─────────────────────────────────────────────────
#  Dialog: Freie Modelle scannen
# ─────────────────────────────────────────────────

class ModelScanDialog(tk.Toplevel):
    """
    Scannt alle Modelle eines API-Keys, zeigt Status und Log live an.
    Erlaubt, verfuegbare Modelle direkt als Account hinzuzufuegen.
    """

    BG     = "#0f172a"
    BG_MID = "#1e293b"
    BG_L   = "#334155"
    FG     = "#f1f5f9"
    FG_DIM = "#64748b"
    ACCENT = "#3b82f6"

    COL_COLORS = {
        "ok":      "#22c55e",
        "limited": "#ef4444",
        "balance": "#a855f7",
        "error":   "#f59e0b",
        "unknown": "#94a3b8",
    }

    def __init__(self, parent, app, prefill_key="", prefill_url=""):
        super().__init__(parent)
        self.app        = app
        self.title("Freie Modelle entdecken")
        self.geometry("860x620")
        self.minsize(700, 480)
        self.configure(bg=self.BG)
        self._results   = []      # alle probe_model()-Ergebnisse
        self._all_iids  = []      # alle eingefügten Treeview-IIDs (auch detachte)
        self._scanning  = False
        self._stop_flag = threading.Event()

        self._build(prefill_key, prefill_url)
        self.transient(parent)
        self.grab_set()

    # ─────────────────────────── Layout ──────────
    def _build(self, key, url):
        # ── Eingabe ───────────────────────────────
        top = tk.Frame(self, bg=self.BG_MID, padx=14, pady=10)
        top.pack(fill="x")

        def lbl(p, t):
            return tk.Label(p, text=t, bg=p.cget("bg"), fg=self.FG_DIM,
                            font=("Helvetica", 9))

        lbl(top, "API Key").grid(row=0, column=0, sticky="w")
        self._key_var = tk.StringVar(value=key)
        tk.Entry(top, textvariable=self._key_var, show="●", width=36,
                 bg=self.BG_L, fg=self.FG, insertbackground=self.FG,
                 relief="flat", bd=5, font=("Consolas", 10),
                 ).grid(row=1, column=0, sticky="ew", padx=(0, 8))

        lbl(top, "Completions-URL").grid(row=0, column=1, sticky="w")
        self._url_var = tk.StringVar(value=url or DEFAULT_URL)
        tk.Entry(top, textvariable=self._url_var, width=38,
                 bg=self.BG_L, fg=self.FG, insertbackground=self.FG,
                 relief="flat", bd=5, font=("Consolas", 10),
                 ).grid(row=1, column=1, sticky="ew", padx=(0, 8))

        self._scan_btn = tk.Button(
            top, text="▶  Scan starten", command=self._start_scan,
            bg=self.ACCENT, fg="white", relief="flat", cursor="hand2",
            font=("Helvetica", 9, "bold"), padx=12, pady=5,
        )
        self._scan_btn.grid(row=1, column=2, sticky="w")
        top.columnconfigure(1, weight=1)

        # ── Zusätzliche Modelle (benutzerdefiniert) ─
        lbl(top, "Weitere Modell-IDs testen (eine pro Zeile, z. B. provider/model-name):").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        extra_frame = tk.Frame(top, bg=self.BG_L)
        extra_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 0))

        extra_scroll = ttk.Scrollbar(extra_frame, orient="vertical")
        self._extra_txt = tk.Text(
            extra_frame, height=3, wrap="none",
            bg="#020617", fg="#94a3b8", insertbackground=self.FG,
            font=("Consolas", 9), relief="flat", bd=4,
            yscrollcommand=extra_scroll.set,
        )
        extra_scroll.config(command=self._extra_txt.yview)
        extra_scroll.pack(side="right", fill="y")
        self._extra_txt.pack(fill="both", expand=True)

        # Platzhaltertext im Eingabefeld
        hint = "# Eigene Modell-IDs hier eintragen (eine pro Zeile, ohne #):\n# Beispiel: provider/model-name"
        self._extra_txt.insert("1.0", hint)
        self._extra_txt.config(fg="#334155")   # gedimmt als Platzhalter

        def _clear_hint(event):
            if self._extra_txt.cget("fg") == "#334155":
                self._extra_txt.delete("1.0", "end")
                self._extra_txt.config(fg="#94a3b8")
        self._extra_txt.bind("<FocusIn>", _clear_hint)

        # ── Fortschritt ───────────────────────────
        prog_frame = tk.Frame(self, bg=self.BG, padx=14, pady=4)
        prog_frame.pack(fill="x")
        self._prog_lbl = tk.Label(prog_frame, text="Bereit.", bg=self.BG,
                                   fg=self.FG_DIM, font=("Helvetica", 9), anchor="w")
        self._prog_lbl.pack(fill="x")
        self._prog_bar = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self._prog_bar.pack(fill="x", pady=(2, 0))

        # ── Container fuer Tabelle + Log ─────────────
        bot = tk.Frame(self, bg=self.BG_MID, padx=14, pady=8)
        bot.pack(side="bottom", fill="x")

        self._sum_lbl = tk.Label(bot, text="", bg=self.BG_MID,
                                  fg=self.FG_DIM, font=("Helvetica", 9))
        self._sum_lbl.pack(side="left")

        filter_frame = tk.Frame(bot, bg=self.BG_MID)
        filter_frame.pack(side="left", padx=12)

        self._show_ok      = tk.BooleanVar(value=True)
        self._show_limited = tk.BooleanVar(value=True)
        self._show_error   = tk.BooleanVar(value=True)

        for var, text, color in [
            (self._show_ok,      "✔ Verfügbar", "#22c55e"),
            (self._show_limited, "✖ Limitiert", "#ef4444"),
            (self._show_error,   "⚠ Fehler",   "#f59e0b"),
        ]:
            tk.Checkbutton(
                filter_frame, text=text, variable=var,
                command=self._apply_filter,
                bg=self.BG_MID, fg=color, selectcolor=self.BG_L,
                activebackground=self.BG_MID, activeforeground=color,
                font=("Helvetica", 9), relief="flat", cursor="hand2",
            ).pack(side="left", padx=4)

        tk.Button(
            bot, text="＋ Auswahl als Account hinzufügen",
            command=self._add_selected,
            bg="#16a34a", fg="white", relief="flat", cursor="hand2",
            font=("Helvetica", 9, "bold"), padx=10, pady=4,
        ).pack(side="right")

    # ─────────────────────────── Log-Hilfsmethoden ──
        # Grid-Layout: Tabelle bekommt allen freien Platz, Log fixe Hoehe
        main_frame = tk.Frame(self, bg=self.BG)
        main_frame.pack(fill="both", expand=True, padx=14, pady=(4, 0))
        main_frame.rowconfigure(0, weight=1)   # Tabelle wächst
        main_frame.rowconfigure(1, weight=0)   # Log-Header fix
        main_frame.rowconfigure(2, weight=0)   # Log-Body fix
        main_frame.columnconfigure(0, weight=1)

        # ── Ergebnis-Tabelle ──────────────────────
        tbl_frame = tk.Frame(main_frame, bg=self.BG)
        tbl_frame.grid(row=0, column=0, sticky="nsew")

        style = ttk.Style()
        style.configure("Scan.Treeview",
            background=self.BG_MID, fieldbackground=self.BG_MID,
            foreground=self.FG, rowheight=26,
            font=("Consolas", 9), borderwidth=0,
        )
        style.configure("Scan.Treeview.Heading",
            background=self.BG_L, foreground=self.FG_DIM,
            font=("Helvetica", 9, "bold"), relief="flat",
        )
        style.map("Scan.Treeview",
            background=[("selected", self.ACCENT)],
            foreground=[("selected", "white")],
        )

        vsb = ttk.Scrollbar(tbl_frame, orient="vertical")
        self._tree = ttk.Treeview(
            tbl_frame,
            columns=("status", "model", "detail"),
            show="headings",
            style="Scan.Treeview",
            yscrollcommand=vsb.set,
            selectmode="extended",
        )
        vsb.config(command=self._tree.yview)
        vsb.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True)

        for cid, heading, w in [
            ("status",  "Status",   110),
            ("model",   "Modell",   360),
            ("detail",  "Detail",   200),
        ]:
            self._tree.heading(cid, text=heading, anchor="w")
            self._tree.column(cid, width=w, minwidth=60, anchor="w")

        for st, color in self.COL_COLORS.items():
            self._tree.tag_configure(st, foreground=color)

        # Sortierbare Spalten aktivieren
        _attach_sort(self._tree)

        # ── Log-Header ────────────────────────────
        tk.Label(main_frame, text=" Log", bg=self.BG_L, fg=self.FG_DIM,
                 font=("Helvetica", 8, "bold"), anchor="w",
                 ).grid(row=1, column=0, sticky="ew", pady=(4, 0))

        # ── Log-Body ──────────────────────────────
        log_frame = tk.Frame(main_frame, bg=self.BG)
        log_frame.grid(row=2, column=0, sticky="ew")
        log_frame.columnconfigure(0, weight=1)

        log_scroll = ttk.Scrollbar(log_frame, orient="vertical")
        log_scroll.pack(side="right", fill="y")

        self._log = tk.Text(
            log_frame, height=7, state="disabled", wrap="none",
            bg="#020617", fg="#94a3b8", insertbackground=self.FG,
            font=("Consolas", 8), relief="flat", bd=0,
            yscrollcommand=log_scroll.set,
        )
        log_scroll.config(command=self._log.yview)
        self._log.pack(side="left", fill="both", expand=True)

        # Log-Farb-Tags
        self._log.tag_configure("ok",      foreground="#22c55e")
        self._log.tag_configure("limited", foreground="#ef4444")
        self._log.tag_configure("error",   foreground="#f59e0b")
        self._log.tag_configure("info",    foreground="#64748b")
        self._log.tag_configure("head",    foreground="#38bdf8", font=("Consolas", 8, "bold"))

        # ── Unterzeile zuerst packen → bleibt immer sichtbar ─────────
    def _log_write(self, text, tag="info"):
        """Schreibt eine Zeile in die Log-Box (immer im GUI-Thread aufrufen)."""
        self._log.config(state="normal")
        self._log.insert("end", text + "\n", tag)
        self._log.see("end")
        self._log.config(state="disabled")

    def _log_later(self, text, tag="info"):
        """Thread-sicherer Log-Schreibaufruf via after()."""
        self.after(0, self._log_write, text, tag)

    # ─────────────────────────── Scan ────────────
    def _start_scan(self):
        if self._scanning:
            self._stop_flag.set()
            return

        key = self._key_var.get().strip()
        url = self._url_var.get().strip()
        if not key or not url:
            messagebox.showwarning("Fehlende Eingabe",
                                   "Bitte API Key und URL ausfüllen.", parent=self)
            return

        # Reset
        self._results  = []
        self._all_iids = []
        self._tree.delete(*self._tree.get_children())
        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")
        self._stop_flag.clear()
        self._scanning = True
        self._sum_lbl.config(text="")
        self._scan_btn.config(text="⏹  Abbrechen", bg="#dc2626")
        self._prog_bar["value"] = 0
        self._prog_lbl.config(text="Lade Modell-Liste …")

        # Extra-Modell-IDs vor Thread-Start sammeln (GUI-Thread-sicher)
        self._extra_ids = self._get_extra_ids()
        threading.Thread(target=self._run_scan, args=(key, url), daemon=True).start()

    def _get_extra_ids(self) -> list:
        """Liest benutzerdefinierte Modell-IDs aus dem Textfeld."""
        raw = self._extra_txt.get("1.0", "end")
        ids = []
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ids.append(line.split()[0])   # ersten Token nehmen
        return ids

    def _run_scan(self, key, url):
        try:
            self._run_scan_inner(key, url)
        except Exception as e:
            import traceback
            self._log_later(f"✖ Unerwarteter Fehler: {e}", "error")
            self._log_later(traceback.format_exc(), "error")
            self._finish_scan()

    def _run_scan_inner(self, key, url):
        extra    = self._extra_ids
        provider = _detect_provider(url)

        # 1. Passende Modellliste laden
        if provider == "nvidia":
            native_models = fetch_model_list_from_nvidia(key, log_fn=self._log_later)
            or_models     = fetch_model_list_from_openrouter(log_fn=None)
        elif provider == "local":
            native_models = fetch_model_list_from_local(key, url, log_fn=self._log_later)
            or_models     = []   # kein OpenRouter-Preis für lokale Modelle
        else:
            native_models = fetch_model_list_from_openrouter(log_fn=self._log_later)
            or_models     = native_models

        or_index = {m["id"]: m for m in or_models}

        # Extra-IDs ergaenzen
        model_ids = [m["id"] for m in native_models]
        seen = set(model_ids)
        for mid in extra:
            if mid not in seen:
                model_ids.append(mid)
                seen.add(mid)

        total    = len(model_ids)
        free_ids = {mid for mid, m in or_index.items() if m["free"]}

        if provider == "nvidia":
            self._log_later(
                f"✔ {total} NVIDIA NIM Modelle geladen  –  Credits-System, kein Gratis-Tier", "head")
            self._log_later(
                "  ⓘ  NVIDIA: 1.000 kostenlose Inference-Credits nach Anmeldung, danach kostenpflichtig", "info")
            self._log_later(
                "  ⓘ  429 = Rate Limit (40 Req/Min)  |  402 = Credits erschöpft", "info")
        elif provider == "local":
            self._log_later(
                f"✔ {total} lokale Modelle gefunden  –  alle kostenlos (kein API-Aufruf nötig)", "head")
            self._log_later(
                "  ⓘ  Nur lokal installierte Modelle werden angezeigt", "info")
        else:
            gratis = [mid for mid in model_ids if mid in free_ids]
            self._log_later(
                f"✔ {total} Modelle geladen  –  {len(gratis)} laut OpenRouter $0", "head")
            if gratis:
                self._log_later(
                    f"  ★ $0-Modelle: {', '.join(gratis[:8])}"
                    + (" …" if len(gratis) > 8 else ""), "ok")
            self._log_later(
                "  ⓘ  HTTP 402 beim Test = tatsächlich kostenpflichtig (trotz $0-Preis möglich)", "info")

        self._ui(self._prog_lbl.config, {"text": f"{total} Modelle – starte Tests …"})

        if total == 0:
            self._log_later("Keine Modelle gefunden.", "error")
            self._finish_scan()
            return

        # 2a. Lokale Modelle: KEIN Probing – direkt als verfügbar markieren
        #     LM Studio lädt jedes angefragte Modell in den RAM → gefährlich!
        if provider == "local":
            for mid in model_ids:
                meta   = or_index.get(mid, {"local": True, "free": True,
                                            "free_suffix": False, "input_price": 0.0})
                result = {
                    "model":            mid,
                    "status":           "ok",
                    "detail":           "",
                    "retry_after_secs": 0,
                    "or_price":         "lokal – kostenlos",
                    "or_meta":          {**meta, "local": True},
                    "free_override":    True,
                }
                self._results.append(result)
                self._log_later(f"  ✔ ★  {mid}  (installiert, nicht getestet)", "ok")
                self.after(0, self._add_row, result)
            self.after(0, self._set_progress, 100, f"{total} lokale Modelle geladen")
            self._finish_scan()
            return

        # 2b. Remote-Modelle: normal proben
        done = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(probe_model, key, m, url): m for m in model_ids}
            for fut in as_completed(futures):
                if self._stop_flag.is_set():
                    self._log_later("⏹ Scan abgebrochen.", "info")
                    break
                try:
                    result = fut.result()
                except Exception as e:
                    model_id = futures[fut]
                    result = {"model": model_id, "status": "error",
                              "detail": str(e)[:80], "retry_after_secs": 0}

                self._results.append(result)
                done += 1
                pct = int(done / total * 100)

                st     = result["status"]
                sym    = {"ok": "✔", "limited": "✖", "error": "⚠"}.get(st, "–")
                detail = result.get("detail", "")
                star   = " ★" if result["model"] in free_ids else ""
                line   = f"  {sym}{star}  {result['model']}"
                if detail:
                    line += f"  ({detail})"
                self._log_later(line, st)

                if result["status"] == "balance":
                    result["or_price"]      = "kostenpflichtig"
                    result["free_override"] = False
                    result["or_meta"]       = {}
                else:
                    meta = or_index.get(result["model"])
                    if meta:
                        if meta.get("local"):
                            result["or_price"] = "lokal – kostenlos"
                        elif meta["input_price"] < 0:
                            result["or_price"] = "Credits erforderlich"
                        elif meta["free_suffix"]:
                            result["or_price"] = "kostenlos (:free)"
                        elif meta["free"]:
                            result["or_price"] = "kostenlos ($0, kein :free)"
                        else:
                            result["or_price"] = f"${meta['input_price']:.4f}/1M"
                        result["or_meta"] = meta
                    else:
                        result["or_price"] = ""
                        result["or_meta"]  = {}

                self.after(0, self._add_row, result)
                self.after(0, self._set_progress, pct, f"Teste … {done}/{total}")

        self._finish_scan()

    def _ui(self, fn, kwargs):
        """Ruft fn(**kwargs) sicher im GUI-Thread auf."""
        self.after(0, lambda: fn(**kwargs))

    def _finish_scan(self):
        self._scanning = False
        self.after(0, lambda: self._scan_btn.config(
            text="▶  Scan starten", bg=self.ACCENT))
        self.after(0, self._update_summary)

    # ─────────────────────────── Tabelle ─────────
    def _add_row(self, result):
        """Fuegt eine Zeile ein. Muss im GUI-Thread laufen."""
        st     = result["status"]
        # ★ nur zeigen wenn: ok UND wirklich kostenlos UND kein 402 bekannt
        or_meta     = result.get("or_meta", {})
        is_local    = or_meta.get("local", False) if isinstance(or_meta, dict) else False
        is_free     = (result.get("or_price", "") in ("kostenlos", "kostenlos (:free)", "lokal – kostenlos")
                       and result.get("free_override", True)
                       and st == "ok")
        free_suffix = or_meta.get("free_suffix", False) if isinstance(or_meta, dict) else False
        if st == "ok" and is_local:
            label = "✔  Verfügbar  ★"      # lokal = immer kostenlos
        elif is_free and free_suffix:
            label = "✔  Verfügbar  ★"      # explizit :free
        elif is_free:
            label = "✔  Verfügbar  (~★)"   # $0 aber kein :free-Suffix – unsicher
        else:
            label = {"ok":      "✔  Verfügbar",
                     "limited": "✖  Limitiert",
                     "balance": "💳 Kostenpflichtig",
                     "error":   "⚠  Fehler"}.get(st, "–")
        detail = result.get("detail", "")
        price  = result.get("or_price", "")
        if price and not detail:
            detail = price
        elif price and price not in detail:
            detail = f"{detail}  [{price}]"

        # Sicheres iid: Sonderzeichen ersetzen
        iid = result["model"].replace("/", "__")
        try:
            self._tree.insert("", "end", iid=iid, tags=(st,),
                              values=(label, result["model"], detail))
            self._all_iids.append(iid)
        except tk.TclError:
            # IID-Kollision (doppeltes Modell) → ignorieren
            pass
        self._apply_filter()

    def _apply_filter(self):
        """Zeigt/versteckt Zeilen anhand der Checkboxen.
        Iteriert über _all_iids statt get_children(), um detachte Items zu erfassen.
        """
        show = set()
        if self._show_ok.get():      show.add("ok")
        if self._show_limited.get(): show.add("limited")
        if self._show_error.get():   show.add("error")

        for iid in self._all_iids:
            try:
                tags = self._tree.item(iid, "tags")
                st   = tags[0] if tags else "unknown"
                if st in show:
                    # Sichtbar machen (reattach falls detacht)
                    try:
                        self._tree.reattach(iid, "", "end")
                    except tk.TclError:
                        pass   # war nie detacht – ignorieren
                else:
                    self._tree.detach(iid)
            except tk.TclError:
                pass   # iid existiert nicht mehr

    def _set_progress(self, pct, msg):
        self._prog_bar["value"] = pct
        self._prog_lbl.config(text=msg)

    def _update_summary(self):
        ok  = sum(1 for r in self._results if r["status"] == "ok")
        lim = sum(1 for r in self._results if r["status"] == "limited")
        bal = sum(1 for r in self._results if r["status"] == "balance")
        err = sum(1 for r in self._results if r["status"] == "error")
        total = len(self._results)
        self._sum_lbl.config(
            text=f"{total} getestet  ·  {ok} verfügbar  ·  {lim} limitiert  ·  {bal} kein Guthaben  ·  {err} Fehler")
        self._prog_lbl.config(text="Scan abgeschlossen.")
        self._log_write(
            f"─── Fertig: {ok} frei / {lim} limitiert / {err} Fehler ───", "head")

    # ─────────────────────────── Account-Import ──
    def _add_selected(self):
        # Alle sichtbaren selektierten Items, oder alle sichtbaren "ok"-Items
        selected_iids = list(self._tree.selection())
        if not selected_iids:
            selected_iids = [
                iid for iid in self._all_iids
                if self._tree.exists(iid) and
                   self._tree.parent(iid) == "" and   # nicht detacht
                   (self._tree.item(iid, "tags") or ("",))[0] == "ok"
            ]

        if not selected_iids:
            messagebox.showinfo("Nichts ausgewählt",
                                "Keine Modelle markiert oder verfügbar.", parent=self)
            return

        key          = self._key_var.get().strip()
        url          = self._url_var.get().strip()
        provider     = provider_name_from_url(url)
        added        = 0
        for iid in selected_iids:
            # Echte Modell-ID aus Wertspalte lesen
            vals     = self._tree.item(iid, "values")
            model_id = vals[1] if vals else iid.replace("__", "/")

            if any(a.get("api_key") == key and a.get("model") == model_id
                   for a in self.app.accounts):
                continue

            # Name = Provider (z.B. "Cline") – darf mehrfach vorkommen
            self.app.accounts.append({
                "id":          new_account_id(),
                "name":        provider,
                "api_key":     key,
                "model":       model_id,
                "url":         url,
                "status":      "unknown",
                "detail":      "",
                "last_check":  None,
                "retry_until": None,
            })
            added += 1

        if added:
            self.app.save()
            messagebox.showinfo("Hinzugefügt",
                                f"{added} Modell(e) als Account gespeichert.", parent=self)
        else:
            messagebox.showinfo("Keine neuen Einträge",
                                "Alle Modelle bereits vorhanden.", parent=self)


# ─────────────────────────────────────────────────
#  Dialog: Account hinzufügen / bearbeiten
# ─────────────────────────────────────────────────

class AccountDialog(tk.Toplevel):
    """Modaler Dialog zum Erfassen eines LLM-Accounts."""

    FIELDS = [
        ("Name",    "name",    False, "z. B. Cline 1 – mimo-v2.5"),
        ("API Key", "api_key", True,  "sk_…"),
        ("Modell",  "model",   False, "z. B. xiaomi/mimo-v2.5"),
        ("URL",     "url",     False, DEFAULT_URL),
    ]

    def __init__(self, parent, account: dict | None = None):
        super().__init__(parent)
        self.title("Account bearbeiten" if account else "Account hinzufügen")
        self.resizable(False, False)
        self.result: dict | None = None
        self._entries: dict[str, tk.Entry] = {}
        self._vars:    dict[str, tk.StringVar] = {}

        self._apply_style()
        self._build(account or {})

        self.transient(parent)
        self.grab_set()
        self.wait_window()

    # ── Styling ──────────────────────────────────
    def _apply_style(self):
        self.configure(bg="#1e293b")

    # ── Layout ───────────────────────────────────
    def _build(self, data: dict):
        outer = tk.Frame(self, bg="#1e293b", padx=20, pady=16)
        outer.pack(fill="both", expand=True)

        for i, (label, key, secret, placeholder) in enumerate(self.FIELDS):
            tk.Label(
                outer, text=label, bg="#1e293b", fg="#94a3b8",
                font=("Helvetica", 10, "bold"), anchor="w"
            ).grid(row=i * 2, column=0, columnspan=3, sticky="w", pady=(8, 0))

            var   = tk.StringVar(value=data.get(key, placeholder if not data else ""))
            entry = tk.Entry(
                outer, textvariable=var, show="●" if secret else "",
                font=("Consolas", 10), width=48,
                bg="#0f172a", fg="#e2e8f0", insertbackground="#e2e8f0",
                relief="flat", bd=6,
            )
            entry.grid(row=i * 2 + 1, column=0, sticky="ew", padx=(0, 4))
            self._entries[key] = entry
            self._vars[key]    = var

            if secret:
                tk.Button(
                    outer, text="👁", command=lambda e=entry: self._toggle(e),
                    bg="#334155", fg="#e2e8f0", relief="flat", cursor="hand2",
                    width=3, font=("Helvetica", 11),
                ).grid(row=i * 2 + 1, column=1)

        # Test-Button
        tk.Button(
            outer, text="🔌 Verbindung testen",
            command=self._test, bg="#334155", fg="#94a3b8",
            relief="flat", cursor="hand2", font=("Helvetica", 10),
        ).grid(row=len(self.FIELDS) * 2 + 1, column=0, sticky="w", pady=(12, 0))

        self._test_label = tk.Label(outer, text="", bg="#1e293b", font=("Helvetica", 9))
        self._test_label.grid(row=len(self.FIELDS) * 2 + 2, column=0, sticky="w")

        # Speichern / Abbrechen
        btn_frame = tk.Frame(outer, bg="#1e293b")
        btn_frame.grid(row=len(self.FIELDS) * 2 + 3, column=0, columnspan=3, pady=(16, 0), sticky="e")

        tk.Button(
            btn_frame, text="Speichern", command=self._save,
            bg="#3b82f6", fg="white", relief="flat", cursor="hand2",
            font=("Helvetica", 10, "bold"), padx=14, pady=5,
        ).pack(side="right", padx=4)

        tk.Button(
            btn_frame, text="Abbrechen", command=self.destroy,
            bg="#334155", fg="#e2e8f0", relief="flat", cursor="hand2",
            font=("Helvetica", 10), padx=14, pady=5,
        ).pack(side="right", padx=4)

        outer.columnconfigure(0, weight=1)

    # ── Aktionen ─────────────────────────────────
    def _toggle(self, entry: tk.Entry):
        entry.config(show="" if entry.cget("show") else "●")

    def _collect(self) -> dict:
        return {k: v.get().strip() for k, v in self._vars.items()}

    def _test(self):
        data = self._collect()
        if not all(data.get(k) for k in ("api_key", "model", "url")):
            self._test_label.config(text="⚠ Bitte API Key, Modell und URL ausfüllen.", fg="#f59e0b")
            return
        self._test_label.config(text="⏳ Prüfe …", fg="#94a3b8")
        self.update()

        def run():
            result = check_account(data)
            st     = result["status"]
            color  = STATUS_META[st][1]
            label  = STATUS_META[st][0]
            detail = result.get("detail", "")
            text   = f"{label}   {detail}".strip()
            self._test_label.config(text=text, fg=color)

        threading.Thread(target=run, daemon=True).start()

    def _save(self):
        data = self._collect()
        if not all(data.values()):
            messagebox.showwarning("Pflichtfelder", "Alle Felder müssen ausgefüllt sein.", parent=self)
            return
        self.result = data
        self.destroy()


# ─────────────────────────────────────────────────
#  Hauptfenster
# ─────────────────────────────────────────────────


def _make_sort_cmd(tree, col, state):
    """Erzeugt einen Sortier-Callback fuer Treeview-Spalten-Klick."""
    def _sort():
        reverse = state.get(col, False)
        rows = [(tree.set(iid, col), iid) for iid in tree.get_children("")]
        rows.sort(key=lambda x: x[0].lower(), reverse=reverse)
        for idx, (_, iid) in enumerate(rows):
            tree.move(iid, "", idx)
        state[col] = not reverse
        # Pfeil im Header aktualisieren
        for c in tree["columns"]:
            heading = tree.heading(c, "text").rstrip(" ▲▼")
            if c == col:
                arrow = " ▼" if reverse else " ▲"
                tree.heading(c, text=heading + arrow,
                             command=_make_sort_cmd(tree, c, state))
            else:
                tree.heading(c, text=heading,
                             command=_make_sort_cmd(tree, c, state))
    return _sort


def _attach_sort(tree):
    """Aktiviert sortierbare Spalten fuer ein Treeview-Widget."""
    state = {}
    for col in tree["columns"]:
        tree.heading(col, command=_make_sort_cmd(tree, col, state))

class MainWindow:
    """Tkinter-Hauptfenster mit Account-Liste und Verwaltungs-Toolbar."""

    BG_DARK   = "#0f172a"
    BG_MID    = "#1e293b"
    BG_LIGHT  = "#334155"
    FG_BRIGHT = "#f1f5f9"
    FG_DIM    = "#64748b"
    ACCENT    = "#3b82f6"

    def __init__(self, app: "App"):
        self.app = app
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("820x540")
        self.root.minsize(640, 400)
        self.root.configure(bg=self.BG_DARK)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_style()
        self._build_header()
        self._build_toolbar()
        self._build_table()
        self._build_statusbar()

        # Minimize-Button → In Tray minimieren
        if HAS_TRAY:
            self.root.bind("<Unmap>", self._on_unmap)

        self._schedule_refresh()

    # ── Tkinter-Style ─────────────────────────────
    def _build_style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        # Treeview
        s.configure("Accounts.Treeview",
            background=self.BG_MID, fieldbackground=self.BG_MID,
            foreground=self.FG_BRIGHT, rowheight=30,
            font=("Consolas", 10), borderwidth=0, relief="flat",
        )
        s.configure("Accounts.Treeview.Heading",
            background=self.BG_LIGHT, foreground=self.FG_DIM,
            font=("Helvetica", 9, "bold"), relief="flat",
        )
        s.map("Accounts.Treeview",
            background=[("selected", self.ACCENT)],
            foreground=[("selected", "white")],
        )
        s.configure("Vertical.TScrollbar",
            background=self.BG_LIGHT, troughcolor=self.BG_MID,
            arrowcolor=self.FG_DIM, borderwidth=0,
        )

    def _lbl(self, parent, text, fg=None, font=None, **kwargs):
        return tk.Label(parent, text=text,
                        bg=parent.cget("bg"), fg=fg or self.FG_BRIGHT,
                        font=font or ("Helvetica", 10), **kwargs)

    def _btn(self, parent, text, cmd, accent=False, small=False):
        return tk.Button(
            parent, text=text, command=cmd,
            bg=self.ACCENT if accent else self.BG_LIGHT,
            fg="white" if accent else self.FG_BRIGHT,
            activebackground="#2563eb" if accent else "#475569",
            activeforeground="white",
            relief="flat", cursor="hand2",
            font=("Helvetica", 9, "bold" if accent else "normal"),
            padx=10 if not small else 6,
            pady=4,
        )

    # ── Header ───────────────────────────────────
    def _build_header(self):
        hdr = tk.Frame(self.root, bg=self.BG_MID, pady=10, padx=16)
        hdr.pack(fill="x")

        self._lbl(hdr, "🔍  LLM Quota Checker",
                  font=("Helvetica", 14, "bold"), fg=self.FG_BRIGHT).pack(side="left")

        self.overall_lbl = self._lbl(hdr, "", fg="#94a3b8", font=("Helvetica", 10))
        self.overall_lbl.pack(side="right")

    # ── Toolbar ──────────────────────────────────
    def _build_toolbar(self):
        tb = tk.Frame(self.root, bg=self.BG_DARK, pady=8, padx=12)
        tb.pack(fill="x")

        self._btn(tb, "＋ Hinzufügen", self._add,    accent=True).pack(side="left", padx=3)
        self._btn(tb, "✏ Bearbeiten",  self._edit                ).pack(side="left", padx=3)
        self._btn(tb, "✖ Löschen",     self._delete              ).pack(side="left", padx=3)

        sep = tk.Frame(tb, bg=self.BG_LIGHT, width=1)
        sep.pack(side="left", fill="y", padx=10, pady=2)

        self._btn(tb, "⟳ Jetzt prüfen", self._manual_check, accent=False).pack(side="left", padx=3)

        sep2 = tk.Frame(tb, bg=self.BG_LIGHT, width=1)
        sep2.pack(side="left", fill="y", padx=10, pady=2)

        self._btn(tb, "🔍 Modelle scannen", self._open_scan).pack(side="left", padx=3)

        # Intervall-Einstellung
        right = tk.Frame(tb, bg=self.BG_DARK)
        right.pack(side="right")
        self._lbl(right, "Intervall (Min):", fg=self.FG_DIM).pack(side="left", padx=(0, 4))
        self.interval_var = tk.StringVar(value=str(self.app.config.get("interval_minutes", DEFAULT_MINS)))
        ie = tk.Entry(right, textvariable=self.interval_var, width=5,
                      bg=self.BG_LIGHT, fg=self.FG_BRIGHT, insertbackground=self.FG_BRIGHT,
                      relief="flat", bd=4, font=("Consolas", 10))
        ie.pack(side="left")
        ie.bind("<Return>", self._save_interval)
        ie.bind("<FocusOut>", self._save_interval)

    # ── Account-Tabelle ──────────────────────────
    def _build_table(self):
        frame = tk.Frame(self.root, bg=self.BG_DARK, padx=12)
        frame.pack(fill="both", expand=True, pady=(0, 4))

        scroll = ttk.Scrollbar(frame, orient="vertical")
        self.tree = ttk.Treeview(
            frame,
            columns=("name", "model", "status", "detail", "checked"),
            show="headings",
            style="Accounts.Treeview",
            yscrollcommand=scroll.set,
            selectmode="browse",
        )
        scroll.config(command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        cols = {
            "name":    ("Account",      200),
            "model":   ("Modell",       180),
            "status":  ("Status",       120),
            "detail":  ("Detail",       180),
            "checked": ("Geprüft um",   100),
        }
        for cid, (heading, width) in cols.items():
            self.tree.heading(cid, text=heading, anchor="w")
            self.tree.column(cid, width=width, minwidth=60, anchor="w")

        # Farb-Tags je Status
        for st, (_, color, _) in STATUS_META.items():
            self.tree.tag_configure(st, foreground=color)
        # balance-Zeilen zusätzlich kursiv (unterscheidet von normalem Fehler)
        self.tree.tag_configure("balance", foreground="#a855f7", font=("Consolas", 10, "italic"))

        # Sortierbare Spalten aktivieren
        _attach_sort(self.tree)

        # Doppelklick → Bearbeiten
        self.tree.bind("<Double-1>", lambda _: self._edit())

    # ── Statusleiste ─────────────────────────────
    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=self.BG_MID, pady=5, padx=12)
        bar.pack(fill="x", side="bottom")
        self.status_lbl = self._lbl(bar, "Bereit.", fg=self.FG_DIM, font=("Helvetica", 9))
        self.status_lbl.pack(side="left")
        self.next_lbl = self._lbl(bar, "", fg=self.FG_DIM, font=("Helvetica", 9))
        self.next_lbl.pack(side="right")

    # ── Refresh-Logik ────────────────────────────
    def _schedule_refresh(self):
        self._refresh_table()
        ok    = sum(1 for a in self.app.accounts if a.get("status") == "ok")
        total = len(self.app.accounts)

        if total == 0:
            self.overall_lbl.config(text="Keine Accounts konfiguriert")
        else:
            self.overall_lbl.config(
                text=f"{ok} / {total} verfügbar",
                fg=STATUS_META["ok"][1] if ok == total
                   else STATUS_META["limited"][1] if ok == 0
                   else STATUS_META["error"][1],
            )

        self.status_lbl.config(text=f"Letzte Prüfung: {self.app.last_check_str}")
        self.next_lbl.config(text=f"Nächste: {self.app.next_check_str}")
        self.root.after(2000, self._schedule_refresh)

    def _row_values(self, acc):
        """Berechnet (tags, values) fuer einen Account – ohne Tree-Zugriff."""
        now = datetime.now()
        st  = acc.get("status", "unknown")
        lbl = STATUS_META[st][0]

        retry_until_iso = acc.get("retry_until")
        detail = acc.get("detail", "")
        if retry_until_iso and st == "limited":
            try:
                until = datetime.fromisoformat(retry_until_iso)
                rem   = remaining_str(retry_until_iso)
                if until > now and rem:
                    detail = f"Cooldown – {rem}  (frei ab {until.strftime('%H:%M:%S')})"
            except Exception:
                pass
        elif st == "balance":
            detail = acc.get("detail", "Kein Guthaben – nur manuell prüfbar")

        values = (
            acc["name"],
            acc.get("model", ""),
            lbl,
            detail,
            acc.get("last_check") or "–",
        )
        return (st,), values

    def _refresh_table(self):
        """Aktualisiert Zeilen in-place – Sortierreihenfolge bleibt erhalten."""
        existing = set(self.tree.get_children(""))
        acc_ids  = {acc["id"] for acc in self.app.accounts}

        # Neue oder geänderte Zeilen einfügen / aktualisieren
        for acc in self.app.accounts:
            iid = acc["id"]
            tags, values = self._row_values(acc)
            if iid in existing:
                self.tree.item(iid, tags=tags, values=values)
            else:
                self.tree.insert("", "end", iid=iid, tags=tags, values=values)

        # Gelöschte Accounts aus Tabelle entfernen
        for iid in existing:
            if iid not in acc_ids:
                self.tree.delete(iid)

    def _selected_account(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return next((a for a in self.app.accounts if a["id"] == sel[0]), None)

    # ── CRUD-Aktionen ────────────────────────────
    def _add(self):
        dlg = AccountDialog(self.root)
        if dlg.result:
            acc = {**dlg.result, "id": new_account_id(),
                   "status": "unknown", "detail": "", "last_check": None, "retry_until": None}
            self.app.accounts.append(acc)
            self.app.save()
            self._refresh_table()

    def _edit(self):
        acc = self._selected_account()
        if not acc:
            messagebox.showinfo("Hinweis", "Bitte zuerst einen Account auswählen.", parent=self.root)
            return
        dlg = AccountDialog(self.root, acc)
        if dlg.result:
            # In-place aktualisieren – id bleibt stabil
            acc.update(dlg.result)
            self.app.save()
            self._refresh_table()

    def _delete(self):
        acc = self._selected_account()
        if not acc:
            messagebox.showinfo("Hinweis", "Bitte zuerst einen Account auswählen.", parent=self.root)
            return
        if messagebox.askyesno("Löschen", f'Account „{acc["name"]}" wirklich entfernen?',
                               parent=self.root):
            self.app.accounts = [a for a in self.app.accounts if a["id"] != acc["id"]]
            self.app.save()
            self._refresh_table()

    def _manual_check(self):
        self.status_lbl.config(text="⏳ Prüfe Accounts …")
        threading.Thread(target=lambda: self.app.run_checks(force=True), daemon=True).start()

    def _open_scan(self):
        """Öffnet den Modell-Scan-Dialog, vorbelegt mit dem Key des markierten Accounts."""
        acc = self._selected_account()
        key = acc["api_key"] if acc else ""
        url = acc["url"]     if acc else DEFAULT_URL
        ModelScanDialog(self.root, self.app, prefill_key=key, prefill_url=url)
        # Nach Schliessen Tabelle aktualisieren (ggf. neue Accounts)
        self._refresh_table()

    def _save_interval(self, _=None):
        try:
            mins = int(self.interval_var.get())
            if mins < 1:
                raise ValueError
            self.app.config["interval_minutes"] = mins
            self.app.save()
        except ValueError:
            messagebox.showwarning("Ungültige Eingabe", "Bitte eine ganze Zahl ≥ 1 eingeben.",
                                   parent=self.root)

    # ── Fenster-Steuerung ────────────────────────
    def _on_unmap(self, event):
        """Minimize-Button → ins Tray minimieren statt auf Taskleiste."""
        if event.widget is self.root and HAS_TRAY:
            # Kurz warten, bis der Fensterstatus aktualisiert ist
            self.root.after(100, self._check_minimize)

    def _check_minimize(self):
        if self.root.wm_state() == "iconic":
            self.root.withdraw()

    def _on_close(self):
        if HAS_TRAY:
            self.root.withdraw()   # In Tray minimieren
        else:
            self.app.quit()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def mainloop(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────
#  Tray-Icon-Manager
# ─────────────────────────────────────────────────

class TrayManager:
    """Verwaltet das System-Tray-Icon via pystray."""

    TOOLTIP_MAX = 127   # Windows-Limit für Tray-Tooltips

    def __init__(self, app: "App"):
        self.app    = app
        self._icon  = None
        self._ready = threading.Event()   # gesetzt sobald Icon läuft

    def start(self):
        if not HAS_TRAY:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Öffnen",       self._open, default=True),
            pystray.MenuItem("Jetzt prüfen", self._check),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Beenden",      self._quit),
        )
        self._icon = pystray.Icon(
            "llm_checker",
            make_tray_image(TRAY_COLORS["unknown"]),
            title=APP_NAME,
            menu=menu,
        )

        def setup(icon):
            icon.visible = True
            self._ready.set()   # Icon ist jetzt beschreibbar

        threading.Thread(
            target=lambda: self._icon.run(setup=setup),
            daemon=True,
        ).start()

    def update(self):
        """Aktualisiert Icon-Farbe und Tooltip – thread-sicher."""
        if not self._icon or not self._ready.is_set():
            return
        try:
            accounts = self.app.accounts
            statuses = [a.get("status", "unknown") for a in accounts]
            ok_count = statuses.count("ok")
            total    = len(statuses)

            if not total or all(s == "unknown" for s in statuses):
                color = TRAY_COLORS["unknown"]
            elif ok_count == total:
                color = TRAY_COLORS["all_ok"]
            elif ok_count == 0:
                color = TRAY_COLORS["none_ok"]
            else:
                color = TRAY_COLORS["some_ok"]

            lines = [f"LLM Checker – {ok_count}/{total} verfuegbar"]
            for acc in accounts:
                st  = acc.get("status", "unknown")
                sym = STATUS_META[st][2]
                lines.append(f"{sym} {acc['name']}")

            tooltip = "\n".join(lines)[: self.TOOLTIP_MAX]

            self._icon.icon  = make_tray_image(color)
            self._icon.title = tooltip
        except Exception as e:
            print(f"[Tray] Update-Fehler: {e}")

    def _open(self):
        # pystray-Callback läuft im Tray-Thread → GUI-Update via after()
        self.app.window.root.after(0, self.app.window.show)

    def _check(self):
        threading.Thread(target=self.app.run_checks, daemon=True).start()

    def _quit(self):
        self.app.quit()


# ─────────────────────────────────────────────────
#  Haupt-App-Orchestrator
# ─────────────────────────────────────────────────

class App:
    """Verbindet Checker, Fenster und Tray-Icon."""

    def __init__(self):
        self.config       = load_config()
        self.accounts     = self.config.get("accounts", [])
        self.last_check_str = "–"
        self.next_check_str = "–"
        self._stop_event  = threading.Event()

        self.window = MainWindow(self)
        self.tray   = TrayManager(self)

    def save(self):
        self.config["accounts"] = self.accounts
        save_config(self.config)

    def run_checks(self, force=False):
        """Prueft alle Accounts und aktualisiert Tray + GUI.
        Accounts mit aktivem retry_until werden uebersprungen (ausser force=True).
        """
        now     = datetime.now()
        now_str = now.strftime("%H:%M:%S")
        for acc in self.accounts:
            retry_until_iso = acc.get("retry_until")
            if not force and retry_until_iso:
                try:
                    until = datetime.fromisoformat(retry_until_iso)
                    if until > now:
                        continue   # noch gesperrt → ueberspringen
                    else:
                        acc["retry_until"] = None   # Sperre abgelaufen
                except Exception:
                    acc["retry_until"] = None

            result = check_account(acc)
            acc["status"]     = result["status"]
            acc["last_check"] = now_str

            if result["status"] == "limited":
                secs = result.get("retry_after_secs", 3600)
                until_dt = now + timedelta(seconds=secs)
                acc["retry_until"] = until_dt.isoformat()
                acc["detail"] = result.get("detail", "")
            elif result["status"] == "balance":
                # Kein Guthaben → dauerhaft überspringen bis manuell geprüft (force=True)
                # Wir setzen retry_until weit in die Zukunft (24h), damit es nicht
                # stündlich geprüft wird – nur "Jetzt prüfen" erzwingt einen neuen Check.
                until_dt = now + timedelta(hours=24)
                acc["retry_until"] = until_dt.isoformat()
                acc["detail"] = result.get("detail", "")
            else:
                acc["retry_until"] = None
                acc["detail"] = result.get("detail", "")

        self.last_check_str = now_str
        self.tray.update()

    def _checker_loop(self):
        """Hintergrund-Thread: prüft im konfigurierten Intervall."""
        time.sleep(3)   # Kurzer Startdelay
        while not self._stop_event.is_set():
            self.run_checks()
            interval_secs = self.config.get("interval_minutes", DEFAULT_MINS) * 60
            next_dt = datetime.now() + timedelta(seconds=interval_secs)
            self.next_check_str = next_dt.strftime("%H:%M:%S")
            # In kleinen Schritten warten (damit Intervall-Änderungen wirken)
            for _ in range(interval_secs):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    def quit(self):
        self._stop_event.set()
        self.window.root.quit()

    def run(self):
        # Tray starten
        self.tray.start()
        # Checker-Thread starten
        threading.Thread(target=self._checker_loop, daemon=True).start()
        # GUI-Event-Loop (blockierend)
        self.window.mainloop()


# ─────────────────────────────────────────────────
#  Einstiegspunkt
# ─────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.run()
