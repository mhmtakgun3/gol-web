import os
import time
import json
import re
import threading
import fcntl
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template_string, request

# ============================================================
# GOL SİNYAL MERKEZİ - WEB ONLY
# PC'de çalışan bot mantığı temel alınmıştır.
# Telegram YOKTUR.
# ============================================================

app = Flask(__name__)

API_BASE = "https://v3.football.api-sports.io"
API_KEY = os.getenv("API_KEY", "").strip()

CHECK_SECONDS = int(os.getenv("CHECK_SECONDS", "30"))
FIRST_HALF_CHECK_SECONDS = int(os.getenv("FIRST_HALF_CHECK_SECONDS", "15"))
SIGNAL_LIMIT = int(os.getenv("SIGNAL_LIMIT", "65"))
# Yeni ayrı değişken, Render'da kalmış eski FIRST_HALF_LIMIT=65 ayarının
# İY 0,5 iyileştirmesini sessizce geçersiz kılmasını önler.
FIRST_HALF_LIMIT = int(os.getenv("FIRST_HALF_05_LIMIT", "58"))
FIRST_HALF_15_LIMIT = int(os.getenv("FIRST_HALF_15_LIMIT", "68"))
BOT_PICK_LIMIT = 78
BOT_PICK_MINUTE = 55
BOT_PICK_CONFIRM_SCANS = 2
BOT_PICK_LATE_MINUTE = 75
BOT_PICK_MIN_RECENT_PRESSURE = 45
BOT_PICK_LATE_MIN_RECENT_PRESSURE = 70
MOMENTUM_WINDOW_SECONDS = 300
MOMENTUM_LONG_SECONDS = 600
TREND_HISTORY_MAX = 24

# Canlı dış oran filtresi
LIVE_ODDS_MIN = 1.50

UEFA_COMPETITIONS = {2: "UEFA Champions League", 3: "UEFA Europa League", 848: "UEFA Europa Conference League"}
LIVE_ODDS_CACHE_TTL = int(os.getenv("LIVE_ODDS_CACHE_TTL", "30"))

# API çağrılarının sonsuza kadar beklememesi için
REQUEST_TIMEOUT = 15
LIVE_SNAPSHOT_FILE = "/tmp/gol_live_snapshot.json"
SHARED_STATE_FILE = "/tmp/gol_shared_state.json"
SCANNER_LOCK_FILE = "/tmp/gol_scanner.lock"

# Basit cache süreleri
LIVE_CACHE_TTL = 10
STATS_CACHE_TTL = 20

ALLOWED_LEAGUES = {
    # UEFA Avrupa Kupaları
    2,    # UEFA Champions League
    3,    # UEFA Europa League
    848,  # UEFA Europa Conference League

    # İNGİLTERE - Ligler + Kupalar
    39, 40, 41, 42,
    45,  # FA Cup
    48,  # League Cup / Carabao Cup
    46,  # EFL Trophy

    # İSPANYA - Ligler + Kupa
    140, 141, 435,
    143, # Copa del Rey

    # İTALYA - Ligler + Kupa
    135, 136, 137,  # 137 = Coppa Italia

    # ALMANYA - Ligler + Kupa
    78, 79, 80,
    81,  # DFB Pokal

    # FRANSA - Ligler + Kupa
    61, 62, 63,
    66,  # Coupe de France
    # TÜRKİYE
    203, 204, 205, 206,
    # HOLLANDA
    88, 89,
    # BELÇİKA
    144, 145,
    # PORTEKİZ
    94, 95,
    # İSKOÇYA
    179, 180, 181, 182,
    # AVUSTURYA
    218, 219,
    # İSVİÇRE
    207, 208,
    # YUNANİSTAN
    197,
    # POLONYA
    106, 107,
    # ÇEKYA
    345, 346,
    # DANİMARKA
    119, 120,
    # NORVEÇ
    103, 104,
    # İSVEÇ
    113, 114,
    # ABD
    253,
    # BREZİLYA
    71, 72,
    # ARJANTİN
    128,
    # MEKSİKA
    262, 263,
    # JAPONYA
    98, 99,
    # GÜNEY KORE
    292, 293,
    # AVUSTRALYA
    188,
    # SIRBİSTAN
    286,
    # HIRVATİSTAN
    210,
    # BULGARİSTAN
    172,
    # İRLANDA
    357,
    # FİNLANDİYA
    244,
    # İZLANDA
    164,
}

# Büyükler düzeyindeki milli takım organizasyonları. İsim kontrolü de
# kullanıldığı için API yeni sezonlarda kimliği değiştirmese bile yalnızca
# sabit ID listesine bağımlı kalmayız. U21/U19 ve kulüp turnuvaları dahil değil.
NATIONAL_TEAM_LEAGUE_IDS = {
    1,  # FIFA World Cup
    4,  # Euro Championship
    5,  # UEFA Nations League
    6,  # Africa Cup of Nations
    7,  # Asian Cup
    9,  # Copa America
}
NATIONAL_TEAM_LEAGUE_NAMES = {
    "friendlies",
    "world cup",
    "world cup - qualification africa",
    "world cup - qualification asia",
    "world cup - qualification concacaf",
    "world cup - qualification europe",
    "world cup - qualification oceania",
    "world cup - qualification south america",
    "euro championship",
    "euro championship - qualification",
    "uefa nations league",
    "africa cup of nations",
    "africa cup of nations - qualification",
    "asian cup",
    "asian cup - qualification",
    "copa america",
    "concacaf nations league",
    "concacaf gold cup",
    "ofc nations cup",
}
NATIONAL_TEAM_NAME_MARKERS = (
    "uefa nations league",
    "concacaf nations league",
    "world cup",
    "euro championship",
    "copa america",
    "africa cup of nations",
    "asian cup",
    "concacaf gold cup",
    "ofc nations cup",
)
NATIONAL_TEAM_EXCLUDED_MARKERS = (
    "club", "women", "u17", "u18", "u19", "u20", "u21", "u23", "youth",
)


def is_allowed_competition(league: Dict[str, Any], prematch: bool = False) -> bool:
    league_id = safe_int(league.get("id"), 0)
    if league_id in NATIONAL_TEAM_LEAGUE_IDS:
        return True
    league_name = re.sub(r"\s+", " ", str(league.get("name") or "").strip().lower())
    if league_name in NATIONAL_TEAM_LEAGUE_NAMES:
        return True
    # Elemelerin kıta/round ekleri sağlayıcıda dönem dönem değişebiliyor.
    # Büyükler turnuvasını kök adından tanı; genç/kadın/kulüp turnuvasını alma.
    if (not any(word in league_name for word in NATIONAL_TEAM_EXCLUDED_MARKERS)
            and any(marker in league_name for marker in NATIONAL_TEAM_NAME_MARKERS)):
        return True
    allowed = PREMATCH_LEAGUES if prematch else ALLOWED_LEAGUES
    return league_id in allowed

# Yalnızca maç önü analizinden çıkarılan ligler. Canlı Gol Merkezi bu ligleri
# taramaya devam eder; Romanya ve Güney Afrika ise yukarıdaki ortak listeden çıkarıldı.
PREMATCH_EXCLUDED_LEAGUES = {
    128,       # Arjantin
    219,       # Avusturya 2. lig
    71, 72,    # Brezilya ligleri
    172,       # Bulgaristan
    210,       # Hırvatistan
    62, 63,    # Fransa 2. ve 3. lig
    120,       # Danimarka 2. lig
    262, 263,  # Meksika ligleri
    107,       # Polonya 2. lig
    293,       # Güney Kore 2. lig
    435,       # İspanya 3. lig
}
PREMATCH_LEAGUES = ALLOWED_LEAGUES - PREMATCH_EXCLUDED_LEAGUES

# ------------------------------------------------------------
# Ortak durum
# ------------------------------------------------------------

memory_lock = threading.Lock()
cache_lock = threading.Lock()
status_lock = threading.Lock()

match_memory: Dict[int, Dict[str, Any]] = {}
bot_pick_history: List[Dict[str, Any]] = []

live_cache = {
    "ts": 0.0,
    "data": [],
    "all_live": [],
}

stats_cache: Dict[int, Dict[str, Any]] = {}

live_odds_cache = {
    "ts": 0.0,
    "data": [],
    "error": None,
}

# Maç önü istekleri sayfa açılışlarında tekrar API kotası tüketmesin.
prematch_cache: Dict[str, Dict[str, Any]] = {}
PREMATCH_FIXTURE_CACHE_SECONDS = 900
PREMATCH_FORM_CACHE_SECONDS = 1800
PREMATCH_ODDS_CACHE_SECONDS = 1800
PREMATCH_LINEUP_CACHE_SECONDS = 300
PREMATCH_MIN_ODD = 1.30
ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")

scanner_status = {
    "running": False,
    "last_scan_started": None,
    "last_scan_finished": None,
    "last_scan_duration": None,
    "last_scan_error": None,
    "api_live_count": 0,
    "eligible_live_count": 0,
    "analyzed_count": 0,
    "last_api_message": None,
}

scanner_started = False
scanner_start_lock = threading.Lock()



# ============================================================
# PROCESS'LER ARASI ORTAK DURUM
# ============================================================

def serialize_matches_for_web() -> List[Dict[str, Any]]:
    """Scanner process'indeki match_memory verisini web için JSON'a çevirir."""
    with memory_lock:
        items = []

        for fixture_id, item in match_memory.items():
            match = item.get("match") or {}
            stats = item.get("current_stats") or {}
            period_stats = item.get("period_stats") or stats

            expected_team_key = item.get("expected_team")
            expected_team_name = None

            if expected_team_key == "home":
                expected_team_name = match.get("home_team")
            elif expected_team_key == "away":
                expected_team_name = match.get("away_team")

            match_expected_key = item.get("match_expected_team")
            match_expected_name = None

            if match_expected_key == "home":
                match_expected_name = match.get("home_team")
            elif match_expected_key == "away":
                match_expected_name = match.get("away_team")
            elif match_expected_key == "both":
                match_expected_name = "İki takım da"

            items.append({
                "fixture_id": fixture_id,
                "league": match.get("league"),
                "country": match.get("country"),
                "home_team": match.get("home_team"),
                "away_team": match.get("away_team"),
                "home_logo": match.get("home_logo"),
                "away_logo": match.get("away_logo"),
                "minute": match.get("minute"),
                "status": match.get("status"),
                "home_goals": match.get("home_goals"),
                "away_goals": match.get("away_goals"),
                "signal": item.get("current_signal", 0),
                "signal_active": item.get("current_signal", 0) >= SIGNAL_LIMIT,
                "reasons": item.get("current_reasons", []),
                "first_half_signal": item.get("first_half_signal", 0),
                "first_half_active": (
                    match.get("status") == "1H"
                    and (match.get("minute") or 0) >= 10
                    and (match.get("home_goals") or 0) + (match.get("away_goals") or 0) <= 1
                    and ((match.get("home_goals") or 0) + (match.get("away_goals") or 0) == 0
                         or (match.get("minute") or 0) <= 38)
                    and item.get("first_half_signal", 0) >= (
                        FIRST_HALF_LIMIT
                        if (match.get("home_goals") or 0) + (match.get("away_goals") or 0) == 0
                        else FIRST_HALF_15_LIMIT
                    )
                ),
                "first_half_limit": (
                    FIRST_HALF_LIMIT
                    if (match.get("home_goals") or 0) + (match.get("away_goals") or 0) == 0
                    else FIRST_HALF_15_LIMIT
                ),
                "first_half_reasons": item.get("first_half_reasons", []),
                "expected_team": expected_team_name,
                "match_expected_team": match_expected_name,
                "home_goal_signal": item.get("home_goal_signal", 0),
                "away_goal_signal": item.get("away_goal_signal", 0),
                "btts_signal": item.get("btts_signal", 0),
                "btts_label": item.get("btts_label", "DÜŞÜK"),
                "period_number": item.get("period_number", 0),
                "period_started_minute": item.get("period_started_minute"),
                "bot_pick_score": item.get("bot_pick_score", 0),
                "bot_pick_text": item.get("bot_pick_text", ""),
                "bot_pick_reasons": item.get("bot_pick_reasons", []),
                "bot_pick_best": item.get("bot_pick_best", False),
                "bot_pick_confirm_count": item.get("bot_pick_confirm_count", 0),
                "bot_pick_confirm_required": BOT_PICK_CONFIRM_SCANS,
                "bot_pick_ready": item.get("bot_pick_ready", False),
                "momentum_score": item.get("momentum_score", 0),
                "momentum_label": item.get("momentum_label", "YENİ"),
                "momentum_home_score": item.get("momentum_home_score", 0),
                "momentum_away_score": item.get("momentum_away_score", 0),
                "momentum_delta_5m": item.get("momentum_delta_5m", {}),
                "momentum_delta_10m": item.get("momentum_delta_10m", {}),
                "scenario_adjustment": item.get("scenario_adjustment", 0),
                "live_odd": item.get("live_odd"),
                "live_odd_market": item.get("live_odd_market"),
                "live_odd_selection": item.get("live_odd_selection"),
                "live_odd_update": item.get("live_odd_update"),
                "odds_qualified": item.get("odds_qualified", False),
                "odds_min_required": LIVE_ODDS_MIN,
                "odds_error": item.get("odds_error"),
                "stats": {
                    "shots": stats.get("shots", 0),
                    "target": stats.get("target", 0),
                    "corners": stats.get("corners", 0),
                    "inside": stats.get("inside", 0),
                    "home": stats.get("home", {}),
                    "away": stats.get("away", {}),
                },
                "period_stats": {
                    "shots": period_stats.get("shots", 0),
                    "target": period_stats.get("target", 0),
                    "corners": period_stats.get("corners", 0),
                    "inside": period_stats.get("inside", 0),
                    "home": period_stats.get("home", {}),
                    "away": period_stats.get("away", {}),
                },
                "stats_error": item.get("stats_error"),
                "last_update": item.get("last_update"),
            })

    items.sort(
        key=lambda x: (
            x.get("bot_pick_best", False),
            x.get("bot_pick_score", 0),
            x.get("signal_active", False),
            x.get("first_half_active", False),
            x.get("signal", 0),
            x.get("first_half_signal", 0),
        ),
        reverse=True,
    )

    return items


def write_shared_state():
    """
    Scanner'ın son durumunu /tmp altında atomik olarak yazar.
    Web endpoint'leri API'ye gitmeden bu dosyayı okur.
    """
    try:
        with status_lock:
            status_copy = dict(scanner_status)

        matches_copy = serialize_matches_for_web()

        payload = {
            "written_at": time.time(),
            "status": status_copy,
            "matches": matches_copy,
            "bot_pick_stats": bot_pick_stats(),
            "bot_pick_history": list(bot_pick_history[-50:]),
        }

        temp_path = SHARED_STATE_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        os.replace(temp_path, SHARED_STATE_FILE)

    except Exception as exc:
        print(f"Shared state yazma hatası: {exc}", flush=True)


def read_shared_state() -> Optional[Dict[str, Any]]:
    try:
        with open(SHARED_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ============================================================
# API YARDIMCILARI
# ============================================================

def api_headers() -> Dict[str, str]:
    return {
        "x-apisports-key": API_KEY,
    }


def api_get(path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not API_KEY:
        return None, "API_KEY ortam değişkeni tanımlı değil."

    url = f"{API_BASE}{path}"

    try:
        response = requests.get(
            url,
            headers=api_headers(),
            params=params or {},
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            return None, f"HTTP {response.status_code}"

        try:
            data = response.json()
        except Exception:
            return None, "API yanıtı JSON olarak okunamadı."

        errors = data.get("errors")
        if errors:
            if isinstance(errors, dict):
                message = " | ".join(str(v) for v in errors.values())
            elif isinstance(errors, list):
                message = " | ".join(str(v) for v in errors)
            else:
                message = str(errors)

            if message.strip():
                return data, message.strip()

        return data, None

    except requests.Timeout:
        return None, "API isteği zaman aşımına uğradı."
    except requests.RequestException as exc:
        return None, f"API bağlantı hatası: {exc}"
    except Exception as exc:
        return None, f"Beklenmeyen API hatası: {exc}"


def get_live_matches() -> Tuple[List[Dict[str, Any]], Optional[str], int]:
    now = time.time()

    with cache_lock:
        if now - live_cache["ts"] < LIVE_CACHE_TTL:
            cached = list(live_cache["data"])
            all_cached = list(live_cache.get("all_live", []))
            return cached, None, len(all_cached)

    data, error = api_get("/fixtures", {"live": "all"})
    if data is None:
        return [], error, 0

    response = data.get("response", []) or []
    api_live_count = len(response)

    if error:
        return [], error, api_live_count

    filtered = []
    for match in response:
        league = match.get("league") or {}
        if is_allowed_competition(league):
            filtered.append(match)

    with cache_lock:
        live_cache["ts"] = now
        live_cache["data"] = list(filtered)
        live_cache["all_live"] = list(response)

    # Gunicorn birden fazla process açarsa RAM cache process'ler arasında paylaşılmaz.
    # Bu küçük snapshot aynı Render instance içindeki /tmp dosyasına yazılır.
    # Ekstra API isteği oluşturmaz.
    try:
        snapshot_payload = {
            "ts": now,
            "all_live": response,
        }
        temp_path = LIVE_SNAPSHOT_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot_payload, f, ensure_ascii=False)
        os.replace(temp_path, LIVE_SNAPSHOT_FILE)
    except Exception:
        pass

    return filtered, None, api_live_count


def get_stats(fixture_id: int) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    now = time.time()

    with cache_lock:
        cached = stats_cache.get(fixture_id)
        if cached and now - cached["ts"] < STATS_CACHE_TTL:
            return cached["data"], cached.get("error")

    data, error = api_get("/fixtures/statistics", {"fixture": fixture_id})

    if data is None:
        return None, error

    response = data.get("response", []) or []

    with cache_lock:
        stats_cache[fixture_id] = {
            "ts": now,
            "data": response,
            "error": error,
        }

    return response, error



def get_all_live_odds() -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Tüm canlı odds verisini tek API çağrısıyla alır.
    Sadece BOT adayı oluştuğunda çağrılır ve 60 sn cache kullanır.
    """
    now = time.time()

    with cache_lock:
        if now - float(live_odds_cache.get("ts", 0)) < LIVE_ODDS_CACHE_TTL:
            return list(live_odds_cache.get("data") or []), live_odds_cache.get("error")

    data, error = api_get("/odds/live", {})

    if data is None:
        return [], error

    response = data.get("response", []) or []

    with cache_lock:
        live_odds_cache["ts"] = now
        live_odds_cache["data"] = list(response)
        live_odds_cache["error"] = error

    return response, error


def _float_odd(value: Any) -> Optional[float]:
    try:
        odd = float(str(value).replace(",", "."))
        if odd > 1.0:
            return odd
    except Exception:
        pass
    return None


def _float_line(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    m = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _fixture_odds_entry(
    all_odds: List[Dict[str, Any]],
    fixture_id: int
) -> Optional[Dict[str, Any]]:
    for entry in all_odds:
        fid = safe_int(((entry.get("fixture") or {}).get("id")), 0)
        if fid == safe_int(fixture_id, 0):
            return entry
    return None


def _candidate_values_for_market(market: Dict[str, Any]) -> List[Dict[str, Any]]:
    values = market.get("values") or []
    # Eğer aynı seçimden birden fazla varsa API'nin main=True olanını tercih et.
    main_values = [v for v in values if v.get("main") is True and not v.get("suspended", False)]
    if main_values:
        return main_values
    return [v for v in values if not v.get("suspended", False)]


def find_live_odd_for_pick(
    fixture_odds: Optional[Dict[str, Any]],
    pick_text: str,
    home_team: str,
    away_team: str,
    home_goals: int,
    away_goals: int,
    total_over_line: Optional[float] = None,
    market_period: str = "match",
) -> Optional[Dict[str, Any]]:
    """
    BOT seçimini canlı odds marketiyle eşleştirir.

    Maçta bir gol daha:
      mevcut toplam gole göre Over (toplam_gol + 0.5)

    Takım gol atar:
      önce Next Goal / Next Team To Score marketi,
      sonra açıkça takım total-goal marketi aranır.

    KG Var:
      Both Teams To Score = Yes
    """
    if not fixture_odds:
        return None

    status = fixture_odds.get("status") or {}
    if status.get("blocked") or status.get("finished"):
        return None

    markets = fixture_odds.get("odds") or []
    matches = []

    pick_text_clean = (pick_text or "").strip()
    total_goals = safe_int(home_goals) + safe_int(away_goals)

    def add_match(market, value, quality=0):
        odd = _float_odd(value.get("odd"))
        if odd is None:
            return
        matches.append({
            "odd": odd,
            "market": market.get("name") or "",
            "selection": value.get("value") or "",
            "handicap": value.get("handicap"),
            "main": value.get("main"),
            "quality": quality,
            "update": fixture_odds.get("update"),
        })

    # --------------------------------------------------------
    # MAÇTA BİR GOL DAHA -> mevcut total + 0.5 OVER
    # --------------------------------------------------------
    if pick_text_clean == "Maçta bir gol daha":
        wanted_line = (float(total_over_line) if total_over_line is not None
                       else total_goals + 0.5)

        for market in markets:
            name = str(market.get("name") or "").lower()

            is_first_half = any(k in name for k in ("1st half", "first half", "1h ", "1h-"))
            if market_period == "first_half" and not is_first_half:
                continue
            if market_period != "first_half" and is_first_half:
                continue

            # Gol total marketi olsun; corner/card vb. olmasın.
            if not any(k in name for k in ("over/under", "total", "goals")):
                continue
            if any(k in name for k in ("corner", "card", "booking", "throw", "offside")):
                continue
            if "team" in name or "home" in name or "away" in name:
                continue

            for value in _candidate_values_for_market(market):
                val_text = str(value.get("value") or "").lower()
                if "over" not in val_text:
                    continue

                line = _float_line(value.get("handicap"))
                if line is None:
                    line = _float_line(value.get("value"))

                if line is not None and abs(line - wanted_line) <= 0.01:
                    add_match(market, value, quality=100)

    # --------------------------------------------------------
    # KG VAR
    # --------------------------------------------------------
    elif pick_text_clean == "KG Var":
        for market in markets:
            name = str(market.get("name") or "").lower()

            if not (
                ("both" in name and "score" in name)
                or "btts" in name
                or "both teams" in name
            ):
                continue

            for value in _candidate_values_for_market(market):
                val_text = str(value.get("value") or "").strip().lower()
                if val_text in ("yes", "evet", "y"):
                    add_match(market, value, quality=100)

    # --------------------------------------------------------
    # TAKIM GOL ATAR
    # --------------------------------------------------------
    else:
        side = None
        team_name = None
        current_team_goals = 0

        if home_team and pick_text_clean == f"{home_team} gol atar":
            side = "home"
            team_name = home_team
            current_team_goals = safe_int(home_goals)
        elif away_team and pick_text_clean == f"{away_team} gol atar":
            side = "away"
            team_name = away_team
            current_team_goals = safe_int(away_goals)

        if side:
            # Önce Next Goal marketi: doğrudan hangi takım sıradaki golü atar.
            for market in markets:
                name = str(market.get("name") or "").lower()
                if "next" not in name or "goal" not in name:
                    continue

                for value in _candidate_values_for_market(market):
                    val = str(value.get("value") or "").strip().lower()
                    team_lower = str(team_name or "").lower()

                    side_match = (
                        (side == "home" and val in ("home", "1"))
                        or (side == "away" and val in ("away", "2"))
                        or (team_lower and team_lower in val)
                    )
                    if side_match:
                        add_match(market, value, quality=120)

            # Fallback: takım total gol over mevcut+0.5
            wanted_line = current_team_goals + 0.5
            for market in markets:
                name = str(market.get("name") or "").lower()

                side_words = (
                    ("home", "team 1", "1st team")
                    if side == "home"
                    else ("away", "team 2", "2nd team")
                )

                if "goal" not in name and "total" not in name:
                    continue
                if not any(word in name for word in side_words):
                    continue
                if any(k in name for k in ("corner", "card", "booking")):
                    continue

                for value in _candidate_values_for_market(market):
                    val_text = str(value.get("value") or "").lower()
                    if "over" not in val_text:
                        continue

                    line = _float_line(value.get("handicap"))
                    if line is None:
                        line = _float_line(value.get("value"))

                    if line is not None and abs(line - wanted_line) <= 0.01:
                        add_match(market, value, quality=90)

    if not matches:
        return None

    # En açık/doğrudan market önce, aynı kalite içinde main sonra,
    # ardından oran.
    matches.sort(
        key=lambda x: (
            x.get("quality", 0),
            1 if x.get("main") is True else 0,
            x.get("odd", 0),
        ),
        reverse=True,
    )
    return matches[0]



# ============================================================
# İSTATİSTİK YARDIMCILARI
# ============================================================

def stat_value(stats: List[Dict[str, Any]], stat_name: str) -> float:
    for item in stats:
        if item.get("type") == stat_name:
            value = item.get("value")

            if value is None:
                return 0.0

            if isinstance(value, (int, float)):
                return float(value)

            text = str(value).strip()

            if text.endswith("%"):
                text = text[:-1].strip()

            try:
                return float(text)
            except ValueError:
                return 0.0

    return 0.0


def get_team_stats(team_block: Dict[str, Any]) -> Dict[str, float]:
    stats = team_block.get("statistics", []) or []

    return {
        "shots": stat_value(stats, "Total Shots"),
        "target": stat_value(stats, "Shots on Goal"),
        "corners": stat_value(stats, "Corner Kicks"),
        "inside": stat_value(stats, "Shots insidebox"),
    }


def get_total_stats(stats_response: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not stats_response or len(stats_response) < 2:
        return None

    home_block = stats_response[0]
    away_block = stats_response[1]

    home = get_team_stats(home_block)
    away = get_team_stats(away_block)

    return {
        "home": home,
        "away": away,
        "shots": home["shots"] + away["shots"],
        "target": home["target"] + away["target"],
        "corners": home["corners"] + away["corners"],
        "inside": home["inside"] + away["inside"],
    }


# ============================================================
# SİNYAL HESAPLAMA - PC BOT MANTIĞI
# ============================================================

def calculate_signal(
    total: Dict[str, Any],
    minute: int,
    home_goals: int,
    away_goals: int
) -> Tuple[int, List[str]]:

    score = 0
    reasons = []

    shots = total["shots"]
    target = total["target"]
    corners = total["corners"]
    inside = total["inside"]

    # ŞUT
    if shots >= 15:
        score += 20
        reasons.append("Şut sayısı çok yüksek")
    elif shots >= 11:
        score += 14
        reasons.append("Şut sayısı yüksek")
    elif shots >= 8:
        score += 8

    # İSABETLİ ŞUT
    if target >= 8:
        score += 25
        reasons.append("İsabetli şut çok yüksek")
    elif target >= 6:
        score += 18
        reasons.append("İsabetli şut yüksek")
    elif target >= 4:
        score += 10

    # KORNER
    if corners >= 9:
        score += 15
        reasons.append("Korner baskısı çok yüksek")
    elif corners >= 6:
        score += 9
    elif corners >= 4:
        score += 4

    # CEZA SAHASI İÇİ ŞUT
    if inside >= 10:
        score += 15
        reasons.append("Ceza sahası içi şut çok yüksek")
    elif inside >= 7:
        score += 10
        reasons.append("Ceza sahası içi şut yüksek")

    # DAKİKA
    if 55 <= minute <= 75:
        score += 5
    elif minute >= 76:
        score += 8

    # SKOR DURUMU
    if home_goals == 0 and away_goals == 0 and minute >= 55:
        score += 8
        reasons.append("Maç 0-0, gol baskısı artıyor")
    elif abs(home_goals - away_goals) == 1 and minute >= 60:
        score += 5
        reasons.append("Tek farklı skor, maç açık")

    return min(score, 100), reasons



def calculate_team_goal_signal(
    team_stats: Dict[str, float],
    minute: int,
    team_goals: int,
    opponent_goals: int
) -> int:
    """
    Takımın mevcut baskısını 0-100 arası bir GOL SİNYALİ puanına çevirir.
    Bu değer istatistiksel olarak kalibre edilmiş bahis olasılığı değildir;
    botun canlı maç baskı skorudur.
    """
    score = 0

    shots = team_stats.get("shots", 0)
    target = team_stats.get("target", 0)
    corners = team_stats.get("corners", 0)
    inside = team_stats.get("inside", 0)

    # Şut
    if shots >= 10:
        score += 20
    elif shots >= 7:
        score += 14
    elif shots >= 4:
        score += 8

    # İsabetli şut
    if target >= 5:
        score += 30
    elif target >= 3:
        score += 20
    elif target >= 2:
        score += 12
    elif target >= 1:
        score += 5

    # Korner
    if corners >= 6:
        score += 15
    elif corners >= 4:
        score += 10
    elif corners >= 2:
        score += 5

    # Ceza sahası içi şut
    if inside >= 7:
        score += 20
    elif inside >= 5:
        score += 14
    elif inside >= 3:
        score += 8

    # Maçın son bölümünde baskının önemi artar
    if minute >= 76:
        score += 8
    elif minute >= 55:
        score += 5

    # Geride olan takımın gol ihtiyacı
    if team_goals < opponent_goals and minute >= 60:
        score += 8
    elif team_goals == opponent_goals and minute >= 60:
        score += 5

    return min(score, 100)


def calculate_match_team_expectation(
    total: Dict[str, Any],
    minute: int,
    home_goals: int,
    away_goals: int
) -> Tuple[int, int, Optional[str]]:
    home_signal = calculate_team_goal_signal(
        total["home"], minute, home_goals, away_goals
    )
    away_signal = calculate_team_goal_signal(
        total["away"], minute, away_goals, home_goals
    )

    expected_team = None

    # Çok düşük baskıda takım tahmini üretme.
    # Anlamlı fark varsa daha güçlü tarafı seç.
    if max(home_signal, away_signal) >= 35:
        if home_signal >= away_signal + 8:
            expected_team = "home"
        elif away_signal >= home_signal + 8:
            expected_team = "away"
        elif max(home_signal, away_signal) >= 55:
            expected_team = "both"

    return home_signal, away_signal, expected_team


def calculate_btts_signal(
    home_signal: int,
    away_signal: int,
    minute: int,
    home_goals: int,
    away_goals: int
) -> Tuple[int, str]:
    """
    KG Var değerlendirmesi.
    Skor ve gol atması gereken takımın baskısı birlikte değerlendirilir.
    """
    # İki takım da zaten gol attıysa KG Var gerçekleşmiştir.
    if home_goals > 0 and away_goals > 0:
        return 100, "GERÇEKLEŞTİ"

    # Kalan süre azaldıkça henüz atılması gereken gol/goller için puanı azalt.
    if minute <= 60:
        time_factor = 1.00
    elif minute <= 70:
        time_factor = 0.90
    elif minute <= 80:
        time_factor = 0.78
    elif minute <= 90:
        time_factor = 0.65
    else:
        time_factor = 0.55

    if home_goals > 0 and away_goals == 0:
        # KG için deplasmanın golü gerekiyor.
        kg_score = round(away_signal * time_factor)
    elif away_goals > 0 and home_goals == 0:
        # KG için ev sahibinin golü gerekiyor.
        kg_score = round(home_signal * time_factor)
    else:
        # 0-0'da iki takımın da gol bulması gerektiğinden zayıf taraf belirleyici.
        kg_score = round(min(home_signal, away_signal) * time_factor * 0.85)

    kg_score = max(0, min(100, kg_score))

    if kg_score >= 65:
        label = "YÜKSEK"
    elif kg_score >= 45:
        label = "ORTA"
    else:
        label = "DÜŞÜK"

    return kg_score, label



def empty_total_stats() -> Dict[str, Any]:
    zero_team = {"shots": 0, "target": 0, "corners": 0, "inside": 0}
    return {
        "home": dict(zero_team),
        "away": dict(zero_team),
        "shots": 0,
        "target": 0,
        "corners": 0,
        "inside": 0,
    }


def subtract_total_stats(
    current: Dict[str, Any],
    baseline: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Gol sonrası yalnızca yeni oluşan aksiyonları hesaplar."""
    if not baseline:
        return current

    result = empty_total_stats()

    for side in ("home", "away"):
        for key in ("shots", "target", "corners", "inside"):
            result[side][key] = max(
                0,
                safe_int((current.get(side) or {}).get(key), 0)
                - safe_int((baseline.get(side) or {}).get(key), 0)
            )

    for key in ("shots", "target", "corners", "inside"):
        result[key] = result["home"][key] + result["away"][key]

    return result



def stats_delta(
    current: Dict[str, Any],
    previous: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    if not previous:
        return empty_total_stats()

    result = empty_total_stats()

    for side in ("home", "away"):
        for key in ("shots", "target", "corners", "inside"):
            result[side][key] = max(
                0,
                safe_int((current.get(side) or {}).get(key), 0)
                - safe_int((previous.get(side) or {}).get(key), 0)
            )

    for key in ("shots", "target", "corners", "inside"):
        result[key] = result["home"][key] + result["away"][key]

    return result


def momentum_points(delta: Dict[str, Any]) -> int:
    """
    Son dönemde üretilen YENİ aksiyonların baskı puanı.
    """
    return min(
        100,
        safe_int(delta.get("shots"), 0) * 6
        + safe_int(delta.get("target"), 0) * 16
        + safe_int(delta.get("corners"), 0) * 5
        + safe_int(delta.get("inside"), 0) * 8
    )


def calculate_momentum(
    current_stats: Dict[str, Any],
    trend_history: List[Dict[str, Any]],
    now_ts: float
) -> Dict[str, Any]:
    """
    Ekstra API çağrısı yapmadan, önceki tarama snapshot'ları ile
    son 5 ve 10 dakikadaki YENİ aksiyonları karşılaştırır.
    """
    if not trend_history:
        return {
            "score": 0,
            "label": "YENİ",
            "delta_5m": empty_total_stats(),
            "delta_10m": empty_total_stats(),
            "home_score": 0,
            "away_score": 0,
        }

    def choose_baseline(seconds: int) -> Dict[str, Any]:
        target_ts = now_ts - seconds
        eligible = [x for x in trend_history if float(x.get("ts", 0)) <= target_ts]
        if eligible:
            return eligible[-1].get("stats") or {}
        return trend_history[0].get("stats") or {}

    base_5 = choose_baseline(MOMENTUM_WINDOW_SECONDS)
    base_10 = choose_baseline(MOMENTUM_LONG_SECONDS)

    delta_5 = stats_delta(current_stats, base_5)
    delta_10 = stats_delta(current_stats, base_10)

    score_5 = momentum_points(delta_5)
    score_10 = momentum_points(delta_10)

    # Yakın dönem daha önemli; 10 dakika ise kalıcılığı kontrol eder.
    score = min(100, round(score_5 * 0.72 + score_10 * 0.28))

    home_score = min(
        100,
        safe_int(delta_5["home"].get("shots"), 0) * 6
        + safe_int(delta_5["home"].get("target"), 0) * 16
        + safe_int(delta_5["home"].get("corners"), 0) * 5
        + safe_int(delta_5["home"].get("inside"), 0) * 8
    )
    away_score = min(
        100,
        safe_int(delta_5["away"].get("shots"), 0) * 6
        + safe_int(delta_5["away"].get("target"), 0) * 16
        + safe_int(delta_5["away"].get("corners"), 0) * 5
        + safe_int(delta_5["away"].get("inside"), 0) * 8
    )

    if score >= 65:
        label = "ÇOK GÜÇLÜ"
    elif score >= 45:
        label = "YÜKSELİYOR"
    elif score >= 25:
        label = "ORTA"
    else:
        label = "ZAYIF"

    return {
        "score": score,
        "label": label,
        "delta_5m": delta_5,
        "delta_10m": delta_10,
        "home_score": home_score,
        "away_score": away_score,
    }


def calculate_match_scenario_adjustment(
    minute: int,
    home_goals: int,
    away_goals: int
) -> Tuple[int, List[str]]:
    """
    Aynı istatistiklerin farklı skor/dakikalarda aynı anlamı taşımamasını sağlar.
    """
    adjustment = 0
    reasons = []

    goal_diff = abs(home_goals - away_goals)
    total_goals = home_goals + away_goals

    if goal_diff <= 1:
        adjustment += 5
        reasons.append("Skor hâlâ rekabetçi")
    elif goal_diff == 2 and minute >= 75:
        adjustment -= 8
        reasons.append("İki farklı skor nedeniyle temkin")
    elif goal_diff >= 3:
        adjustment -= 15
        reasons.append("Maç kopmuş görünüyor")

    if (home_goals, away_goals) in ((0, 0), (1, 1), (1, 0), (0, 1)):
        adjustment += 3

    if 65 <= minute <= 85:
        adjustment += 5
        reasons.append("Gol için güçlü dakika aralığı")
    elif 86 <= minute <= 88:
        adjustment += 0
    elif minute >= 89:
        adjustment -= 10
        reasons.append("Kalan süre çok az")

    if total_goals >= 5:
        adjustment -= 4

    return adjustment, reasons



def calculate_bot_pick(
    evaluation_stats: Dict[str, Any],
    normal_signal: int,
    home_goal_signal: int,
    away_goal_signal: int,
    btts_signal: int,
    btts_label: str,
    minute: int,
    home_team: str,
    away_team: str,
    home_goals: int,
    away_goals: int,
    momentum: Dict[str, Any],
) -> Tuple[int, str, List[str], int]:
    """
    BOTUN SEÇİMİ artık:
    - genel gol sinyali
    - güçlü takım sinyali
    - maç temposu
    - son 5/10 dk momentum
    - skor/dakika senaryosu
    birlikte kullanır.
    """
    if minute < BOT_PICK_MINUTE:
        return 0, "", [], 0

    shots = evaluation_stats.get("shots", 0)
    target = evaluation_stats.get("target", 0)
    corners = evaluation_stats.get("corners", 0)
    inside = evaluation_stats.get("inside", 0)

    tempo_score = min(
        100,
        shots * 4 + target * 10 + corners * 3 + inside * 5
    )

    strongest_team_signal = max(home_goal_signal, away_goal_signal)
    momentum_score = safe_int(momentum.get("score"), 0)
    recent = momentum.get("delta_5m") or {}
    recent_target = safe_int(recent.get("target"), 0)
    recent_inside = safe_int(recent.get("inside"), 0)

    # Eski şut birikimi tek başına yeni gol seçimi üretemez.
    # Özellikle 75'ten sonra kalan süre azaldığı için hem güçlü güncel
    # baskı hem de yakın zamanda isabetli/ceza içi aksiyon gerekir.
    if minute >= BOT_PICK_LATE_MINUTE:
        if (momentum_score < BOT_PICK_LATE_MIN_RECENT_PRESSURE
                or recent_target < 1 or recent_inside < 1):
            return 0, "", ["Geç dakikada güncel isabetli şut ve ceza içi baskısı yetersiz"], 0
    elif (momentum_score < BOT_PICK_MIN_RECENT_PRESSURE
          or (recent_target < 1 and recent_inside < 2)):
        return 0, "", ["Son 5 dakikadaki gol aksiyonu yetersiz"], 0

    # Momentum artık ciddi ağırlığa sahip.
    score = round(
        normal_signal * 0.35
        + strongest_team_signal * 0.30
        + tempo_score * 0.15
        + momentum_score * 0.20
    )

    scenario_adjustment, scenario_reasons = calculate_match_scenario_adjustment(
        minute, home_goals, away_goals
    )
    score += scenario_adjustment

    # Eski toplam istatistik yüksek ama güncel aksiyon durmuşsa cezalandır.
    if momentum_score < 20:
        score -= 10
    elif momentum_score >= 65:
        score += 5

    score = max(0, min(100, score))

    reasons = []
    if target >= 5:
        reasons.append("İsabetli şut baskısı güçlü")
    if inside >= 7:
        reasons.append("Ceza sahası içi aksiyon yüksek")
    if shots >= 12:
        reasons.append("Maç temposu yüksek")
    if strongest_team_signal >= 70:
        reasons.append("Bir takımın gol radarı çok güçlü")
    if momentum_score >= 65:
        reasons.append("Son dakikalarda momentum çok güçlü")
    elif momentum_score >= 45:
        reasons.append("Son dakikalarda baskı yükseliyor")

    reasons.extend(scenario_reasons)

    # Takım seçimi için yalnızca toplam sinyal değil, son 5 dk takım momentumu da dikkate alınır.
    home_momentum = safe_int(momentum.get("home_score"), 0)
    away_momentum = safe_int(momentum.get("away_score"), 0)
    recent_home = recent.get("home") or {}
    recent_away = recent.get("away") or {}

    if (
        home_goal_signal >= 70
        and home_goal_signal > away_goal_signal + 12
        and home_momentum >= max(45, away_momentum)
        and safe_int(recent_home.get("target"), 0) >= 1
    ):
        pick_text = f"{home_team} gol atar"
    elif (
        away_goal_signal >= 70
        and away_goal_signal > home_goal_signal + 12
        and away_momentum >= max(45, home_momentum)
        and safe_int(recent_away.get("target"), 0) >= 1
    ):
        pick_text = f"{away_team} gol atar"
    elif btts_label != "GERÇEKLEŞTİ" and btts_signal >= 65 and momentum_score >= 35:
        pick_text = "KG Var"
    else:
        pick_text = "Maçta bir gol daha"

    return score, pick_text, reasons, scenario_adjustment


def resolve_open_picks_after_goal(
    fixture_id: int,
    minute: int,
    old_score: Tuple[int, int],
    new_score: Tuple[int, int],
) -> None:
    """
    Gol sonrası açık BOT seçimlerini seçim türüne göre sonuçlandırır.

    - "Maçta bir gol daha" -> herhangi bir yeni gol = WON
    - "<Ev sahibi> gol atar" -> sadece ev sahibi golü = WON
    - "<Deplasman> gol atar" -> sadece deplasman golü = WON
    - "KG Var" -> yeni skorla iki takım da gol attıysa = WON
    - Yanlış takım gol attıysa takım-gol seçimi LOST olur
    """
    now_iso = datetime.now().isoformat(timespec="seconds")

    old_home, old_away = old_score
    new_home, new_away = new_score

    home_goal_happened = new_home > old_home
    away_goal_happened = new_away > old_away

    for pick in bot_pick_history:
        if pick.get("fixture_id") != fixture_id or pick.get("status") != "OPEN":
            continue

        pick_text = (pick.get("pick_text") or "").strip()
        home_team = (pick.get("home_team") or "").strip()
        away_team = (pick.get("away_team") or "").strip()

        result = None

        if pick_text == "Maçta bir gol daha":
            if home_goal_happened or away_goal_happened:
                result = "WON"

        elif pick_text == "KG Var":
            if new_home > 0 and new_away > 0:
                result = "WON"
            # KG Var için tek gol henüz seçimi bitirmez; OPEN kalır.

        elif home_team and pick_text == f"{home_team} gol atar":
            if home_goal_happened:
                result = "WON"
            elif away_goal_happened:
                result = "LOST"

        elif away_team and pick_text == f"{away_team} gol atar":
            if away_goal_happened:
                result = "WON"
            elif home_goal_happened:
                result = "LOST"

        else:
            # Tanınmayan seçim tipi varsa herhangi bir golü otomatik WON sayma.
            result = None

        if result is not None:
            pick["status"] = result
            pick["result_minute"] = minute
            pick["result_score"] = list(new_score)
            pick["result_at"] = now_iso
            pick["minutes_to_result"] = max(
                0,
                safe_int(minute, 0) - safe_int(pick.get("minute"), 0)
            )


def bot_pick_stats() -> Dict[str, Any]:
    finished = [p for p in bot_pick_history if p.get("status") in ("WON", "LOST")]
    won = sum(1 for p in finished if p.get("status") == "WON")
    lost = sum(1 for p in finished if p.get("status") == "LOST")
    total = len(finished)
    success_rate = round((won / total) * 100, 1) if total else 0.0
    return {
        "total": total,
        "won": won,
        "lost": lost,
        "open": sum(1 for p in bot_pick_history if p.get("status") == "OPEN"),
        "success_rate": success_rate,
    }


def finalize_best_bot_pick() -> None:
    """
    BOT PICK kararı yalnızca futbol modeliyle verilir:
    78+ puan + 2 ardışık onay + momentum/skor-dakika modeli.

    Canlı oran artık zorunlu filtre değildir.
    Bulunursa sadece bilgi ve performans analizi için eklenir.
    """
    with memory_lock:
        for item in match_memory.values():
            item["bot_pick_best"] = False
            item["odds_qualified"] = False

        candidates = [
            item for item in match_memory.values()
            if item.get("bot_pick_score", 0) >= BOT_PICK_LIMIT
            and item.get("bot_pick_text")
            and item.get("bot_pick_ready", False)
        ]

        candidate_snapshots = []
        for item in candidates:
            match = item.get("match") or {}
            candidate_snapshots.append({
                "fixture_id": item.get("fixture_id"),
                "bot_pick_score": item.get("bot_pick_score", 0),
                "bot_pick_text": item.get("bot_pick_text", ""),
                "home_team": match.get("home_team") or "",
                "away_team": match.get("away_team") or "",
                "home_goals": safe_int(match.get("home_goals"), 0),
                "away_goals": safe_int(match.get("away_goals"), 0),
            })

    if not candidate_snapshots:
        return

    # Odds bilgi amaçlıdır; hata veya market bulunamaması seçimi engellemez.
    all_live_odds, odds_error = get_all_live_odds()

    for c in candidate_snapshots:
        entry = _fixture_odds_entry(all_live_odds, safe_int(c["fixture_id"], 0))
        odd_info = find_live_odd_for_pick(
            entry,
            c["bot_pick_text"],
            c["home_team"],
            c["away_team"],
            c["home_goals"],
            c["away_goals"],
        )

        with memory_lock:
            item = match_memory.get(safe_int(c["fixture_id"], 0))
            if item is None:
                continue

            item["odds_error"] = odds_error

            if odd_info:
                item["live_odd"] = odd_info.get("odd")
                item["live_odd_market"] = odd_info.get("market")
                item["live_odd_selection"] = odd_info.get("selection")
                item["live_odd_update"] = odd_info.get("update")
                item["odds_qualified"] = float(odd_info.get("odd") or 0) >= LIVE_ODDS_MIN
            else:
                item["live_odd"] = None
                item["live_odd_market"] = None
                item["live_odd_selection"] = None
                item["live_odd_update"] = None
                item["odds_qualified"] = False

    # Odds değil, BOT puanı en güçlü adayı belirler.
    best_ref = max(candidate_snapshots, key=lambda x: x.get("bot_pick_score", 0))
    best_fixture_id = safe_int(best_ref["fixture_id"], 0)

    with memory_lock:
        best = match_memory.get(best_fixture_id)
        if best is None:
            return

        best["bot_pick_best"] = True

        match = best.get("match") or {}
        fixture_id = best.get("fixture_id")
        score = tuple(best.get("score") or (0, 0))
        period_key = f"{fixture_id}:{score[0]}-{score[1]}"

        if best.get("bot_pick_record_key") == period_key:
            return

        best["bot_pick_record_key"] = period_key

        live_odd = best.get("live_odd")
        if live_odd is None:
            odds_status = "NOT_FOUND"
        elif float(live_odd) >= LIVE_ODDS_MIN:
            odds_status = "QUALIFIED"
        else:
            odds_status = "BELOW_MIN"

        bot_pick_history.append({
            "id": f"{period_key}:{int(time.time())}",
            "fixture_id": fixture_id,
            "home_team": match.get("home_team"),
            "away_team": match.get("away_team"),
            "league": match.get("league"),
            "minute": match.get("minute"),
            "pick_score": best.get("bot_pick_score", 0),
            "pick_text": best.get("bot_pick_text", ""),
            "pick_type": (
                "MATCH_GOAL"
                if best.get("bot_pick_text") == "Maçta bir gol daha"
                else "BTTS"
                if best.get("bot_pick_text") == "KG Var"
                else "TEAM_GOAL"
            ),
            "pick_reasons": list(best.get("bot_pick_reasons") or []),
            "pick_scoreline": list(score),
            "period_number": best.get("period_number", 0),
            "confirmation_scans": best.get("bot_pick_confirm_count", 0),
            "momentum_score": best.get("momentum_score", 0),
            "momentum_label": best.get("momentum_label", "YENİ"),
            "momentum_delta_5m": best.get("momentum_delta_5m", {}),
            "momentum_delta_10m": best.get("momentum_delta_10m", {}),
            "scenario_adjustment": best.get("scenario_adjustment", 0),
            "normal_signal": best.get("current_signal", 0),
            "home_goal_signal": best.get("home_goal_signal", 0),
            "away_goal_signal": best.get("away_goal_signal", 0),
            "btts_signal": best.get("btts_signal", 0),
            "period_stats": best.get("period_stats", {}),
            "full_stats": best.get("current_stats", {}),
            "live_odd": live_odd,
            "live_odd_market": best.get("live_odd_market"),
            "live_odd_selection": best.get("live_odd_selection"),
            "odds_status": odds_status,
            "odds_min_reference": LIVE_ODDS_MIN,
            "status": "OPEN",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })

        if len(bot_pick_history) > 200:
            del bot_pick_history[:-200]


def refresh_live_odds_for_signal_cards() -> None:
    """İY ve maç geneli oynanabilir hedefi olan normal kartlara da oran ekle.

    Tüm maçlar için ayrı çağrı yapmak yerine /odds/live tek kez alınır. BOT PICK
    kartlarının oranı finalize_best_bot_pick tarafından yönetildiği için burada
    yalnız normal sinyal kartları güncellenir.
    """
    snapshots = []
    with memory_lock:
        for item in match_memory.values():
            match = item.get("match") or {}
            minute = safe_int(match.get("minute"), 0)
            status = str(match.get("status") or "")
            if item.get("bot_pick_best") or status not in ("1H", "2H"):
                continue

            home_goals = safe_int(match.get("home_goals"), 0)
            away_goals = safe_int(match.get("away_goals"), 0)
            goal = safe_int(item.get("current_signal"), 0)
            pressure = safe_int(item.get("momentum_score"), 0)
            bot_score = safe_int(item.get("bot_pick_score"), 0)
            btts = safe_int(item.get("btts_signal"), 0)
            home_team = str(match.get("home_team") or "")
            away_team = str(match.get("away_team") or "")

            pick_text = ""
            target_line = None
            market_period = "match"
            total_goals = home_goals + away_goals

            if status == "1H":
                first_half_score = safe_int(item.get("first_half_signal"), 0)
                first_half_limit = FIRST_HALF_LIMIT if total_goals == 0 else FIRST_HALF_15_LIMIT
                valid_line = (10 <= minute <= 45 and total_goals == 0) or (
                    10 <= minute <= 38 and total_goals == 1)
                if valid_line and first_half_score >= first_half_limit:
                    pick_text = "Maçta bir gol daha"
                    target_line = total_goals + 0.5
                    market_period = "first_half"
            elif not (home_goals > 0 and away_goals > 0) and btts >= 70:
                pick_text = "KG Var"
            elif goal >= 65 or pressure >= 65 or bot_score >= 65:
                more_goals = 2 if (bot_score >= 78 or (goal >= 65 and pressure >= 65)) else 1
                if bot_score >= 88 and goal >= 80 and pressure >= 75:
                    more_goals = 3
                pick_text = "Maçta bir gol daha"
                target_line = home_goals + away_goals + more_goals - 0.5
            else:
                expected = str(item.get("match_expected_team") or item.get("expected_team") or "")
                if expected:
                    pick_text = f"{expected} gol atar"

            if not pick_text:
                item["live_odd"] = None
                item["live_odd_market"] = None
                item["live_odd_selection"] = None
                item["live_odd_update"] = None
                item["odds_qualified"] = False
                continue

            snapshots.append({
                "fixture_id": safe_int(item.get("fixture_id"), 0),
                "pick_text": pick_text, "target_line": target_line,
                "market_period": market_period,
                "home_team": home_team, "away_team": away_team,
                "home_goals": home_goals, "away_goals": away_goals,
            })

    if not snapshots:
        return

    all_live_odds, odds_error = get_all_live_odds()
    for snapshot in snapshots:
        odd_info = find_live_odd_for_pick(
            _fixture_odds_entry(all_live_odds, snapshot["fixture_id"]),
            snapshot["pick_text"], snapshot["home_team"], snapshot["away_team"],
            snapshot["home_goals"], snapshot["away_goals"],
            snapshot["target_line"], snapshot["market_period"],
        )
        with memory_lock:
            item = match_memory.get(snapshot["fixture_id"])
            if item is None or item.get("bot_pick_best"):
                continue
            item["odds_error"] = odds_error
            item["live_odd"] = odd_info.get("odd") if odd_info else None
            item["live_odd_market"] = odd_info.get("market") if odd_info else None
            item["live_odd_selection"] = odd_info.get("selection") if odd_info else None
            item["live_odd_update"] = odd_info.get("update") if odd_info else None
            item["odds_qualified"] = bool(
                odd_info and float(odd_info.get("odd") or 0) >= LIVE_ODDS_MIN)


def calculate_first_half_signal(
    total: Dict[str, Any],
    minute: int,
    home_goals: int,
    away_goals: int
) -> Tuple[int, List[str], Optional[str]]:

    goals_scored = home_goals + away_goals
    # 0-0'da İY 0,5; tek gol sonrasında İY 1,5 değerlendirilir.
    # Gerçekleşmiş pazarı tekrar önerme ve ikinci golü çok geç dakikada açma.
    if minute < 10 or minute > 45 or goals_scored >= 2 or (goals_scored == 1 and minute > 38):
        return 0, [], None

    score = 0
    reasons = []

    shots = total["shots"]
    target = total["target"]
    corners = total["corners"]
    inside = total["inside"]

    # Zayıf ve tesadüfi hücumları ele: en az bir isabet ve devam eden üretim şart.
    if target < 1 or (shots < 4 and inside < 3):
        return 0, [], None
    if goals_scored == 1 and (target < 2 or (shots < 5 and inside < 3)):
        return 0, [], None

    # ŞUT
    if shots >= 10:
        score += 22
        reasons.append("İlk yarıda şut sayısı çok yüksek")
    elif shots >= 7:
        score += 17
        reasons.append("İlk yarıda şut sayısı yüksek")
    elif shots >= 4:
        score += 10

    # İSABETLİ ŞUT
    if target >= 5:
        score += 26
        reasons.append("İlk yarıda isabetli şut çok yüksek")
    elif target >= 3:
        score += 20
        reasons.append("İlk yarıda isabetli şut yüksek")
    elif target >= 2:
        score += 14
    elif target >= 1:
        score += 6

    # KORNER
    if corners >= 5:
        score += 13
        reasons.append("İlk yarıda korner baskısı çok yüksek")
    elif corners >= 3:
        score += 9
    elif corners >= 2:
        score += 5

    # CEZA SAHASI İÇİ
    if inside >= 7:
        score += 18
        reasons.append("İlk yarıda ceza sahası içi şut çok yüksek")
    elif inside >= 5:
        score += 14
    elif inside >= 3:
        score += 9
    elif inside >= 2:
        score += 5

    # 0-0
    if home_goals == 0 and away_goals == 0:
        score += 6
        reasons.append("İlk yarı 0-0, gol baskısı değerlendiriliyor")

    # 10-20. dakikada bu rakamlar yüksek tempoya işaret eder; erken güçlü
    # maçları 25-30. dakikayı beklemeden yakala.
    if minute <= 20 and target >= 2 and inside >= 3:
        score += 10
        reasons.append("İlk 20 dakikada yüksek hücum temposu")
    elif minute <= 20 and shots >= 5 and target >= 1:
        score += 5

    # Bir gol olduysa yalnızca golden sonraki istatistikler buraya gelir.
    if goals_scored == 1 and target >= 3 and inside >= 4:
        score += 8
        reasons.append("İlk golden sonra ikinci gol baskısı güçlü")

    home = total["home"]
    away = total["away"]

    home_pressure = (
        home["shots"] * 1.0
        + home["target"] * 2.5
        + home["corners"] * 1.2
        + home["inside"] * 1.5
    )

    away_pressure = (
        away["shots"] * 1.0
        + away["target"] * 2.5
        + away["corners"] * 1.2
        + away["inside"] * 1.5
    )

    expected_team = None

    if away_pressure > 0 and home_pressure > away_pressure * 1.20:
        expected_team = "home"
    elif home_pressure > 0 and away_pressure > home_pressure * 1.20:
        expected_team = "away"

    return min(score, 100), reasons, expected_team


# ============================================================
# MAÇ ANALİZİ
# ============================================================

def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def build_match_snapshot(match: Dict[str, Any]) -> Dict[str, Any]:
    fixture = match.get("fixture") or {}
    league = match.get("league") or {}
    teams = match.get("teams") or {}
    goals = match.get("goals") or {}

    home_team = (teams.get("home") or {}).get("name", "Ev Sahibi")
    away_team = (teams.get("away") or {}).get("name", "Deplasman")

    return {
        "fixture_id": fixture.get("id"),
        "league_id": league.get("id"),
        "league": league.get("name", "Bilinmeyen Lig"),
        "country": league.get("country", ""),
        "home_team": home_team,
        "away_team": away_team,
        "minute": safe_int(((fixture.get("status") or {}).get("elapsed")), 0),
        "status": ((fixture.get("status") or {}).get("short")) or "",
        "home_goals": safe_int(goals.get("home"), 0),
        "away_goals": safe_int(goals.get("away"), 0),
        "home_logo": (teams.get("home") or {}).get("logo"),
        "away_logo": (teams.get("away") or {}).get("logo"),
    }


def analyze_match(match: Dict[str, Any]) -> bool:
    snapshot = build_match_snapshot(match)

    fixture_id = snapshot.get("fixture_id")
    if not fixture_id:
        return False

    # İstatistik API çağrısı lock dışında yapılır.
    stats_response, stats_error = get_stats(int(fixture_id))

    if stats_error:
        with memory_lock:
            existing = match_memory.get(int(fixture_id))
            if existing:
                existing["stats_error"] = stats_error
                existing["match"] = snapshot
                existing["last_update"] = datetime.now().isoformat(timespec="seconds")
        return False

    total = get_total_stats(stats_response or [])

    # PC bot mantığı: istatistiği olmayan maç atlanır.
    if total is None:
        return False

    home_goals = snapshot["home_goals"]
    away_goals = snapshot["away_goals"]
    minute = snapshot["minute"]
    current_score = (home_goals, away_goals)

    with memory_lock:
        existing = match_memory.get(int(fixture_id))

    # İlk görülüşte hafızaya al, sinyali ikinci taramadan itibaren üret.
    if existing is None:
        with memory_lock:
            match_memory[int(fixture_id)] = {
                "fixture_id": int(fixture_id),
                "baseline": total,
                "score": current_score,
                "signal_sent": False,
                "first_half_sent": False,
                "current_signal": 0,
                "current_reasons": [],
                "current_stats": total,
                "first_half_signal": 0,
                "first_half_reasons": [],
                "expected_team": None,
                "match_expected_team": None,
                "home_goal_signal": 0,
                "away_goal_signal": 0,
                "btts_signal": 0,
                "btts_label": "DÜŞÜK",
                "period_baseline": None,
                "period_stats": total,
                "period_number": 0,
                "period_started_minute": snapshot["minute"],
                "bot_pick_score": 0,
                "bot_pick_text": "",
                "bot_pick_reasons": [],
                "bot_pick_best": False,
                "bot_pick_record_key": None,
                "bot_pick_confirm_count": 0,
                "bot_pick_confirm_text": "",
                "bot_pick_ready": False,
                "trend_history": [],
                "momentum_score": 0,
                "momentum_label": "YENİ",
                "momentum_delta_5m": empty_total_stats(),
                "momentum_delta_10m": empty_total_stats(),
                "momentum_home_score": 0,
                "momentum_away_score": 0,
                "scenario_adjustment": 0,
                "live_odd": None,
                "live_odd_market": None,
                "live_odd_selection": None,
                "live_odd_update": None,
                "odds_qualified": False,
                "odds_error": None,
                "match": snapshot,
                "stats_error": None,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "last_update": datetime.now().isoformat(timespec="seconds"),
            }
        return True

    # Önce skor değişimini belirle. Gol olduysa yeni bir analiz dönemi başlat.
    with memory_lock:
        current = match_memory.get(int(fixture_id))
        if current is None:
            return False

        previous_score = tuple(current.get("score", current_score))
        score_changed = previous_score != current_score

        if score_changed:
            resolve_open_picks_after_goal(int(fixture_id), minute, previous_score, current_score)
            current["score"] = current_score
            current["signal_sent"] = False
            current["first_half_sent"] = False
            current["period_baseline"] = total
            current["period_number"] = safe_int(current.get("period_number"), 0) + 1
            current["period_started_minute"] = minute
            current["bot_pick_record_key"] = None
            current["bot_pick_confirm_count"] = 0
            current["bot_pick_confirm_text"] = ""
            current["bot_pick_ready"] = False
            current["trend_history"] = []
            current["live_odd"] = None
            current["live_odd_market"] = None
            current["live_odd_selection"] = None
            current["live_odd_update"] = None
            current["odds_qualified"] = False
            current["odds_error"] = None

        period_baseline = current.get("period_baseline")
        period_number = safe_int(current.get("period_number"), 0)

    # İlk gol öncesi toplam maç verisi kullanılır.
    # Bir gol olduktan sonra ise SADECE golden sonraki yeni aksiyonlar kullanılır.
    evaluation_total = (
        subtract_total_stats(total, period_baseline)
        if period_number > 0
        else total
    )

    normal_signal, normal_reasons = calculate_signal(
        evaluation_total,
        minute,
        home_goals,
        away_goals,
    )

    # İY 0,5 için maçın toplam ilk yarı istatistiği; bir gol olduktan sonra
    # İY 1,5 için yalnızca golden sonra oluşan yeni baskı kullanılır.
    first_half_total = evaluation_total if home_goals + away_goals == 1 else total
    first_half_signal, first_half_reasons, expected_team = calculate_first_half_signal(
        first_half_total,
        minute,
        home_goals,
        away_goals,
    )

    home_goal_signal, away_goal_signal, match_expected_team = calculate_match_team_expectation(
        evaluation_total,
        minute,
        home_goals,
        away_goals,
    )

    btts_signal, btts_label = calculate_btts_signal(
        home_goal_signal,
        away_goal_signal,
        minute,
        home_goals,
        away_goals,
    )

    now_ts = time.time()

    with memory_lock:
        current_for_trend = match_memory.get(int(fixture_id))
        trend_history = list((current_for_trend or {}).get("trend_history") or [])

    momentum = calculate_momentum(
        evaluation_total,
        trend_history,
        now_ts,
    )

    bot_pick_score, bot_pick_text, bot_pick_reasons, scenario_adjustment = calculate_bot_pick(
        evaluation_total,
        normal_signal,
        home_goal_signal,
        away_goal_signal,
        btts_signal,
        btts_label,
        minute,
        snapshot["home_team"],
        snapshot["away_team"],
        home_goals,
        away_goals,
        momentum,
    )

    with memory_lock:
        current = match_memory.get(int(fixture_id))
        if current is None:
            return False

        current["match"] = snapshot
        current["current_stats"] = total
        current["period_stats"] = evaluation_total
        current["current_signal"] = normal_signal
        current["current_reasons"] = normal_reasons
        current["first_half_signal"] = first_half_signal
        current["first_half_reasons"] = first_half_reasons
        current["expected_team"] = expected_team
        current["match_expected_team"] = match_expected_team
        current["home_goal_signal"] = home_goal_signal
        current["away_goal_signal"] = away_goal_signal
        current["btts_signal"] = btts_signal
        current["btts_label"] = btts_label
        current["bot_pick_score"] = bot_pick_score
        current["bot_pick_text"] = bot_pick_text
        current["bot_pick_reasons"] = bot_pick_reasons
        current["momentum_score"] = momentum.get("score", 0)
        current["momentum_label"] = momentum.get("label", "YENİ")
        current["momentum_delta_5m"] = momentum.get("delta_5m", empty_total_stats())
        current["momentum_delta_10m"] = momentum.get("delta_10m", empty_total_stats())
        current["momentum_home_score"] = momentum.get("home_score", 0)
        current["momentum_away_score"] = momentum.get("away_score", 0)
        current["scenario_adjustment"] = scenario_adjustment

        # 2 ardışık tarama onayı:
        # aynı seçim metni eşik üstünde kalırsa sayaç artar.
        if bot_pick_score >= BOT_PICK_LIMIT and bot_pick_text:
            if current.get("bot_pick_confirm_text") == bot_pick_text:
                current["bot_pick_confirm_count"] = safe_int(
                    current.get("bot_pick_confirm_count"), 0
                ) + 1
            else:
                current["bot_pick_confirm_text"] = bot_pick_text
                current["bot_pick_confirm_count"] = 1
        else:
            current["bot_pick_confirm_count"] = 0
            current["bot_pick_confirm_text"] = ""

        current["bot_pick_ready"] = (
            safe_int(current.get("bot_pick_confirm_count"), 0)
            >= BOT_PICK_CONFIRM_SCANS
        )

        # Bu taramayı trend geçmişine ekle.
        trend = list(current.get("trend_history") or [])
        trend.append({
            "ts": now_ts,
            "minute": minute,
            "stats": evaluation_total,
            "normal_signal": normal_signal,
            "home_goal_signal": home_goal_signal,
            "away_goal_signal": away_goal_signal,
            "bot_pick_score": bot_pick_score,
        })
        current["trend_history"] = trend[-TREND_HISTORY_MAX:]

        current["stats_error"] = None
        current["last_update"] = datetime.now().isoformat(timespec="seconds")

        if normal_signal >= SIGNAL_LIMIT:
            current["signal_sent"] = True

        first_half_required = (
            FIRST_HALF_LIMIT if home_goals + away_goals == 0 else FIRST_HALF_15_LIMIT
        )
        if first_half_signal >= first_half_required:
            current["first_half_sent"] = True

    return True


def remove_finished_matches(active_fixture_ids: set):
    with memory_lock:
        stale_ids = [
            fixture_id
            for fixture_id in match_memory.keys()
            if fixture_id not in active_fixture_ids
        ]

        for fixture_id in stale_ids:
            for pick in bot_pick_history:
                if pick.get("fixture_id") == fixture_id and pick.get("status") == "OPEN":
                    pick["status"] = "LOST"
                    pick["result_at"] = datetime.now().isoformat(timespec="seconds")
            match_memory.pop(fixture_id, None)

    with cache_lock:
        stale_stats = [
            fixture_id
            for fixture_id in stats_cache.keys()
            if fixture_id not in active_fixture_ids
        ]
        for fixture_id in stale_stats:
            stats_cache.pop(fixture_id, None)


# ============================================================
# SCANNER THREAD
# ============================================================

def scanner_loop():
    # Gunicorn birden fazla worker açsa bile yalnızca tek process scanner olur.
    # Lock dosyası açık kaldığı sürece kilit tutulur.
    try:
        scanner_lock_handle = open(SCANNER_LOCK_FILE, "w")
        fcntl.flock(scanner_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("ℹ️ Scanner başka bir worker/process tarafından zaten çalıştırılıyor.", flush=True)
        return

    print("✅ Scanner kilidi alındı. API taramasını yalnızca bu process yapacak.", flush=True)

    while True:
        scan_started_ts = time.time()
        scan_started_iso = datetime.now().isoformat(timespec="seconds")

        with status_lock:
            scanner_status["running"] = True
            scanner_status["last_scan_started"] = scan_started_iso
            scanner_status["last_scan_error"] = None
            scanner_status["last_api_message"] = None

        # Web anında "tarama yapılıyor" durumunu görebilsin.
        write_shared_state()

        analyzed_count = 0
        should_sleep = True
        next_sleep = CHECK_SECONDS

        try:
            print("\n" + "=" * 60, flush=True)
            print("📡 CANLI MAÇLAR TARAMASI BAŞLADI", flush=True)
            print("=" * 60, flush=True)

            live_matches, live_error, api_live_count = get_live_matches()

            print(f"📡 API canlı maç sayısı: {api_live_count}", flush=True)
            print(f"✅ Uygun liglerde canlı maç: {len(live_matches)}", flush=True)

            # 10-38. dakikada aktif ilk yarı maçı varsa İY pazarlarını daha
            # sık yenile; diğer zamanlarda normal API temposunu koru.
            if any(
                ((m.get("fixture") or {}).get("status") or {}).get("short") == "1H"
                and 10 <= safe_int(((m.get("fixture") or {}).get("status") or {}).get("elapsed"), 0) <= 38
                for m in live_matches
            ):
                next_sleep = min(CHECK_SECONDS, FIRST_HALF_CHECK_SECONDS)

            with status_lock:
                scanner_status["api_live_count"] = api_live_count
                scanner_status["eligible_live_count"] = len(live_matches)

            if live_error:
                with status_lock:
                    scanner_status["last_scan_error"] = live_error
                    scanner_status["last_api_message"] = live_error
                    scanner_status["analyzed_count"] = 0

                print(f"❌ API hatası: {live_error}", flush=True)

            else:
                active_fixture_ids = set()

                for match in live_matches:
                    fixture_id = ((match.get("fixture") or {}).get("id"))
                    if fixture_id:
                        active_fixture_ids.add(int(fixture_id))

                    try:
                        if analyze_match(match):
                            analyzed_count += 1
                    except Exception as exc:
                        with status_lock:
                            scanner_status["last_scan_error"] = f"Maç analiz hatası: {exc}"
                        print(f"❌ Maç analiz hatası: {exc}", flush=True)

                remove_finished_matches(active_fixture_ids)
                finalize_best_bot_pick()
                refresh_live_odds_for_signal_cards()

                with status_lock:
                    scanner_status["analyzed_count"] = analyzed_count

                print(f"📊 Analiz edilen maç: {analyzed_count}", flush=True)

        except Exception as exc:
            with status_lock:
                scanner_status["last_scan_error"] = str(exc)
            print(f"❌ Scanner hatası: {exc}", flush=True)

        finally:
            duration = round(time.time() - scan_started_ts, 2)

            with status_lock:
                scanner_status["running"] = False
                scanner_status["last_scan_finished"] = datetime.now().isoformat(timespec="seconds")
                scanner_status["last_scan_duration"] = duration

            # Tamamlanmış taramanın status + maç verisini tüm worker'lara paylaş.
            write_shared_state()

            print(f"⏱ Tarama süresi: {duration} saniye", flush=True)
            print(f"😴 {next_sleep} saniye sonra yeni tarama başlayacak.", flush=True)

        time.sleep(next_sleep)

def ensure_scanner_started():
    global scanner_started

    with scanner_start_lock:
        if scanner_started:
            return

        scanner_started = True
        thread = threading.Thread(
            target=scanner_loop,
            daemon=True,
            name="gol-scanner",
        )
        thread.start()


# ============================================================
# WEB API
# ============================================================

def prematch_day(raw: str) -> Optional[date]:
    try:
        requested = date.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    today = datetime.now(ISTANBUL_TZ).date()
    return requested if today <= requested <= today + timedelta(days=7) else None


def prematch_api_cached(key: str, path: str, params: Dict[str, Any], ttl: int
                        ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    with cache_lock:
        cached = prematch_cache.get(key)
        if cached and time.time() - cached["ts"] < ttl:
            return cached["data"], None
    data, error = api_get(path, params)
    if error or data is None:
        return None, error or "Veri alınamadı."
    response = data.get("response")
    if not isinstance(response, list):
        return None, "Beklenmeyen fikstür yanıtı."
    with cache_lock:
        prematch_cache[key] = {"ts": time.time(), "data": response}
        if len(prematch_cache) > 300:
            oldest = min(prematch_cache, key=lambda item: prematch_cache[item]["ts"])
            prematch_cache.pop(oldest, None)
    return response, None


def prematch_fixtures(day: date) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    items, error = prematch_api_cached(
        f"day:{day.isoformat()}", "/fixtures",
        {"date": day.isoformat(), "timezone": "Europe/Istanbul"},
        PREMATCH_FIXTURE_CACHE_SECONDS,
    )
    if error:
        return None, error
    return [
        item for item in (items or [])
        if is_allowed_competition(item.get("league") or {}, prematch=True)
        and ((item.get("fixture") or {}).get("status") or {}).get("short") in ("NS", "TBD")
    ], None


def _prematch_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    return {
        "count": n,
        "scored": round(sum(x["scored"] for x in rows) / n, 2) if n else None,
        "conceded": round(sum(x["conceded"] for x in rows) / n, 2) if n else None,
        "wins": sum(x["win"] for x in rows),
        "draws": sum(x["draw"] for x in rows),
        "losses": sum(x["scored"] < x["conceded"] for x in rows),
        "btts": sum(x["btts"] for x in rows),
        "over15": sum(x["over15"] for x in rows),
        "over25": sum(x["over25"] for x in rows),
        "under25": sum(x["under25"] for x in rows),
        "over35": sum(x["over35"] for x in rows),
        "under35": sum(x["under35"] for x in rows),
        "scored2": sum(x["scored2"] for x in rows),
        "conceded2": sum(x["conceded"] >= 2 for x in rows),
    }


def prematch_team_form(team_id: int, kickoff: str
                       ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    # Genel son 8 ile gerçek son 5 iç/dış saha maçını ayrı örneklemlerden al.
    items, error = prematch_api_cached(
        f"form:{team_id}", "/fixtures",
        {"team": team_id, "last": 30, "timezone": "Europe/Istanbul"},
        PREMATCH_FORM_CACHE_SECONDS,
    )
    if error:
        return None, error
    finished = []
    for item in items or []:
        fixture = item.get("fixture") or {}
        status = (fixture.get("status") or {}).get("short")
        played_at = fixture.get("date") or ""
        try:
            match_time = datetime.fromisoformat(played_at)
            kickoff_time = datetime.fromisoformat(kickoff)
        except (TypeError, ValueError):
            continue
        if (status not in ("FT", "AET", "PEN") or match_time >= kickoff_time
                or kickoff_time - match_time > timedelta(days=365)):
            continue
        sides = item.get("teams") or {}
        goals = item.get("goals") or {}
        home = safe_int((sides.get("home") or {}).get("id"))
        away = safe_int((sides.get("away") or {}).get("id"))
        if team_id not in (home, away) or goals.get("home") is None or goals.get("away") is None:
            continue
        is_home = home == team_id
        scored = safe_int(goals.get("home" if is_home else "away"))
        conceded = safe_int(goals.get("away" if is_home else "home"))
        finished.append({
            "date": played_at, "venue": "home" if is_home else "away",
            "scored": scored, "conceded": conceded,
            "win": scored > conceded, "btts": scored > 0 and conceded > 0,
            "over15": scored + conceded >= 2,
            "over25": scored + conceded >= 3,
            "under25": scored + conceded <= 2,
            "over35": scored + conceded >= 4,
            "under35": scored + conceded <= 3,
            "scored2": scored >= 2,
            "draw": scored == conceded,
        })
    finished.sort(key=lambda x: x["date"], reverse=True)
    recent = finished[:8]
    if len(recent) < 5:
        return {"count": len(recent), "insufficient": True}, None
    home_five = [x for x in finished if x["venue"] == "home"][:5]
    away_five = [x for x in finished if x["venue"] == "away"][:5]
    return {
        "count": len(recent),
        "all": _prematch_metrics(recent),
        "home": _prematch_metrics(home_five),
        "away": _prematch_metrics(away_five),
    }, None


def prematch_h2h(home_id: int, away_id: int, kickoff: str
                 ) -> Tuple[Dict[str, Any], Optional[str]]:
    items, error = prematch_api_cached(
        f"h2h:{home_id}:{away_id}", "/fixtures/headtohead",
        {"h2h": f"{home_id}-{away_id}", "last": 8, "timezone": "Europe/Istanbul"},
        PREMATCH_FORM_CACHE_SECONDS,
    )
    if error:
        return {"count": 0, "insufficient": True}, error
    rows = []
    for item in items or []:
        fixture, teams, goals = item.get("fixture") or {}, item.get("teams") or {}, item.get("goals") or {}
        status, played_at = (fixture.get("status") or {}).get("short"), fixture.get("date") or ""
        try:
            if datetime.fromisoformat(played_at) >= datetime.fromisoformat(kickoff):
                continue
        except (TypeError, ValueError):
            continue
        if status not in ("FT", "AET", "PEN") or goals.get("home") is None or goals.get("away") is None:
            continue
        fixture_home = safe_int((teams.get("home") or {}).get("id"))
        fixture_away = safe_int((teams.get("away") or {}).get("id"))
        if home_id not in (fixture_home, fixture_away) or away_id not in (fixture_home, fixture_away):
            continue
        home_is_fixture_home = fixture_home == home_id
        scored = safe_int(goals.get("home" if home_is_fixture_home else "away"))
        conceded = safe_int(goals.get("away" if home_is_fixture_home else "home"))
        rows.append({
            "date": played_at, "scored": scored, "conceded": conceded,
            "win": scored > conceded, "draw": scored == conceded,
            "btts": scored > 0 and conceded > 0,
            "over15": scored + conceded >= 2, "over25": scored + conceded >= 3,
            "under25": scored + conceded <= 2, "over35": scored + conceded >= 4,
            "under35": scored + conceded <= 3, "scored2": scored >= 2,
        })
    rows.sort(key=lambda x: x["date"], reverse=True)
    recent = rows[:5]
    return {"count": len(recent), "insufficient": len(recent) < 2,
            "all": _prematch_metrics(recent) if recent else _prematch_metrics([])}, None


def _lineup_shape(entry: Dict[str, Any]) -> Dict[str, Any]:
    starters = []
    positions = {"G": 0, "D": 0, "M": 0, "F": 0}
    for item in entry.get("startXI") or []:
        player = item.get("player") or {}
        pos = str(player.get("pos") or "").upper()[:1]
        if pos in positions:
            positions[pos] += 1
        starters.append({
            "id": player.get("id"), "name": player.get("name"), "pos": pos or "?",
        })
    formation = str(entry.get("formation") or "").strip()
    first_band = safe_int(formation.split("-")[0], 0) if formation else 0
    posture = 0
    if positions["F"] >= 3 or first_band == 3:
        posture += 1
    if positions["F"] <= 1 and first_band >= 5:
        posture -= 1
    return {
        "team_id": safe_int((entry.get("team") or {}).get("id")),
        "team": (entry.get("team") or {}).get("name"),
        "formation": formation or "Bilinmiyor", "starters": starters,
        "positions": positions, "posture": posture,
        "confirmed": len(starters) >= 10,
    }


def prematch_lineups(fixture_id: int, home_id: int, away_id: int
                      ) -> Tuple[Dict[str, Any], Optional[str]]:
    items, error = prematch_api_cached(
        f"lineups:{fixture_id}", "/fixtures/lineups", {"fixture": fixture_id},
        PREMATCH_LINEUP_CACHE_SECONDS,
    )
    if error:
        return {"confirmed": False, "home": None, "away": None}, error
    shaped = [_lineup_shape(item) for item in (items or [])]
    home = next((item for item in shaped if item["team_id"] == home_id), None)
    away = next((item for item in shaped if item["team_id"] == away_id), None)
    confirmed = bool(home and away and home["confirmed"] and away["confirmed"])
    return {"confirmed": confirmed, "home": home, "away": away}, None


def lineup_market_note(market: str, lineups: Dict[str, Any]) -> Tuple[int, str]:
    if not lineups.get("confirmed"):
        return 0, "Kadrolar henüz açıklanmadığı için bu seçim form ve iç/dış saha verileriyle üretildi."
    home, away = lineups["home"], lineups["away"]
    hp, ap = home["posture"], away["posture"]
    hf, af = home["positions"]["F"], away["positions"]["F"]
    base = (f"Onaylı dizilişler {home['formation']} ve {away['formation']}; "
            f"ilk 11'lerde {hf} ve {af} hücum oyuncusu görünüyor. ")
    if market in ("OVER_2_5", "OVER_3_5", "BTTS_YES"):
        fit = hp + ap
        verdict = ("İki kadronun hücum yerleşimi gol tercihini destekliyor." if fit > 0
                   else "Kadro dizilişleri gol tercihini güçlendirmiyor." if fit < 0
                   else "Kadro dizilişleri gol tercihi açısından nötr.")
    elif market in ("UNDER_2_5", "UNDER_3_5", "BTTS_NO"):
        fit = -(hp + ap)
        verdict = ("Daha temkinli kadro yerleşimi alt/KG YOK tercihini destekliyor." if fit > 0
                   else "Hücum ağırlıklı yerleşim bu tercihin riskini artırıyor." if fit < 0
                   else "Kadro dizilişleri bu tercih açısından nötr.")
    elif market in ("HOME_OVER_1_5", "HOME", "DC_1X"):
        fit = hp - max(ap, 0)
        verdict = ("Ev sahibinin kadro yerleşimi seçimi destekliyor." if fit > 0
                   else "Rakibin hücum yerleşimi seçimin riskini artırıyor." if fit < 0
                   else "Kadro yerleşimleri bu seçim açısından nötr.")
    elif market in ("AWAY_OVER_1_5", "AWAY", "DC_X2"):
        fit = ap - max(hp, 0)
        verdict = ("Deplasman takımının kadro yerleşimi seçimi destekliyor." if fit > 0
                   else "Ev sahibinin hücum yerleşimi seçimin riskini artırıyor." if fit < 0
                   else "Kadro yerleşimleri bu seçim açısından nötr.")
    else:
        fit, verdict = 0, "Kadro dizilişleri bu seçim açısından nötr."
    return max(-1, min(1, fit)), base + verdict


def prematch_confidence(market: str, rank: int, lineups: Dict[str, Any],
                        lineup_fit: int) -> int:
    """Model seçim puanı; gerçek kazanma olasılığı veya garanti değildir."""
    market_bonus = {
        # Maç sonucu, gol/çifte şans pazarlarından daha oynaktır. Kadro
        # onayı yokken sırf form sıralamasıyla 80+ güvene çıkmasın.
        "HOME": -4, "AWAY": -4,
        "HOME_OVER_1_5": 2, "AWAY_OVER_1_5": 2,
        "DC_1X": 2, "DC_X2": 2,
        "OVER_3_5": -1, "OVER_1_5": -4,
    }.get(market, 0)
    score = 72 + min(6, max(0, rank)) * 2 + market_bonus
    if lineups.get("confirmed"):
        score += 2 + lineup_fit * 2
    return max(60, min(90, int(round(score))))


def h2h_market_context(market: str, h2h: Dict[str, Any]) -> Tuple[int, str]:
    """H2H verisini en fazla bir kademe etkili yardımcı sinyal olarak kullan."""
    if h2h.get("insufficient") or h2h.get("count", 0) < 2:
        return 0, "İki takım arasında yeterli yakın dönem karşılaşması yok."
    m, n = h2h["all"], h2h["all"]["count"]
    positive = negative = False
    if market in ("OVER_1_5", "OVER_2_5", "OVER_3_5"):
        key = {"OVER_1_5": "over15", "OVER_2_5": "over25", "OVER_3_5": "over35"}[market]
        positive, negative = m[key] / n >= .60, m[key] / n <= .25
    elif market in ("UNDER_2_5", "UNDER_3_5"):
        key = "under25" if market == "UNDER_2_5" else "under35"
        positive, negative = m[key] / n >= .60, m[key] / n <= .25
    elif market == "BTTS_YES":
        positive, negative = m["btts"] / n >= .60, m["btts"] / n <= .25
    elif market == "BTTS_NO":
        positive, negative = m["btts"] / n <= .40, m["btts"] / n >= .75
    elif market in ("HOME", "DC_1X"):
        rate = (m["wins"] + (m["draws"] if market == "DC_1X" else 0)) / n
        positive, negative = rate >= .60, rate <= .25
    elif market in ("AWAY", "DC_X2"):
        rate = (m["losses"] + (m["draws"] if market == "DC_X2" else 0)) / n
        positive, negative = rate >= .60, rate <= .25
    elif market == "HOME_OVER_1_5":
        positive, negative = m["scored2"] / n >= .60, m["scored2"] / n <= .25
    elif market == "AWAY_OVER_1_5":
        positive, negative = m["conceded2"] / n >= .60, m["conceded2"] / n <= .25
    effect = 1 if positive else -1 if negative else 0
    note = (f"İkili rekabet son {n} maç: ev sahibi {m['wins']} galibiyet, "
            f"{m['draws']} beraberlik; KG {m['btts']}/{n}, 2,5 ÜST {m['over25']}/{n}.")
    return effect, note


def prematch_suggestions(home: Dict[str, Any], away: Dict[str, Any],
                         lineups: Optional[Dict[str, Any]] = None,
                         h2h: Optional[Dict[str, Any]] = None
                         ) -> List[Dict[str, Any]]:
    if home.get("insufficient") or away.get("insufficient"):
        return []
    h, a = home["all"], away["all"]
    hv, av = home["home"], away["away"]
    picks: List[Dict[str, Any]] = []
    home_goals = h["scored"] + h["conceded"]
    away_goals = a["scored"] + a["conceded"]
    enough_venue = hv["count"] >= 2 and av["count"] >= 2
    venue_goals = ((hv["scored"] + hv["conceded"] + av["scored"] + av["conceded"]) / 2
                   if enough_venue else 0)

    if (enough_venue and h["over25"] / h["count"] >= .50
            and a["over25"] / a["count"] >= .50
            and (home_goals + away_goals) / 2 >= 2.75 and venue_goals >= 2.6):
        picks.append({"market": "OVER_2_5", "label": "2,5 ÜST",
                      "reason": f"Son maçlarda 3+ gol: {h['over25']}/{h['count']} ve {a['over25']}/{a['count']}; iç/dış saha toplam gol ortalaması {venue_goals:.1f}.", "rank": 4})
    if (enough_venue and h["under25"] / h["count"] >= .625
            and a["under25"] / a["count"] >= .625
            and (home_goals + away_goals) / 2 <= 2.35 and venue_goals <= 2.5):
        picks.append({"market": "UNDER_2_5", "label": "2,5 ALT",
                      "reason": f"Son maçlarda en fazla 2 gol: {h['under25']}/{h['count']} ve {a['under25']}/{a['count']}; iç/dış saha ortalaması {venue_goals:.1f}.", "rank": 4})
    if (enough_venue and h["btts"] / h["count"] >= .50
            and a["btts"] / a["count"] >= .50
            and hv["btts"] / hv["count"] >= .50 and av["btts"] / av["count"] >= .50
            and min(h["scored"], a["scored"], hv["conceded"], av["conceded"]) >= .8):
        picks.append({"market": "BTTS_YES", "label": "KG VAR",
                      "reason": f"Karşılıklı gol: {h['btts']}/{h['count']} ve {a['btts']}/{a['count']}; iç/dış sahada da iki takım gol yiyor.", "rank": 4})
    if (enough_venue and h["btts"] / h["count"] <= .38
            and a["btts"] / a["count"] <= .38
            and (h["scored"] <= .9 or a["scored"] <= .9)):
        picks.append({"market": "BTTS_NO", "label": "KG YOK",
                      "reason": f"Son maçlarda KG: {h['btts']}/{h['count']} ve {a['btts']}/{a['count']}; takımlardan birinin gol ortalaması düşük.", "rank": 3})

    # Takım golü: maç toplamının yüksek olması tek başına yeterli değil.
    if (hv["count"] >= 3 and h["scored2"] / h["count"] >= .50
            and hv["scored2"] / hv["count"] >= .50
            and hv["wins"] / hv["count"] >= .50
            and hv["scored"] >= 1.6 and av["conceded"] is not None
            and av["count"] >= 2 and av["conceded"] >= 1.2):
        picks.append({"market": "HOME_OVER_1_5", "label": "Ev sahibi 1,5 gol ÜST",
                      "reason": f"Ev sahibi son {h['count']} maçının {h['scored2']} tanesinde, iç sahadaki {hv['count']} maçının {hv['scored2']} tanesinde en az 2 gol attı; rakip dışarıda maç başına {av['conceded']:.1f} gol yiyor.", "rank": 5})
    if (av["count"] >= 3 and a["scored2"] / a["count"] >= .50
            and av["scored2"] / av["count"] >= .50
            and av["wins"] / av["count"] >= .50
            and av["scored"] >= 1.6 and hv["conceded"] is not None
            and hv["count"] >= 2 and hv["conceded"] >= 1.2):
        picks.append({"market": "AWAY_OVER_1_5", "label": "Deplasman 1,5 gol ÜST",
                      "reason": f"Deplasman takımı son {a['count']} maçının {a['scored2']} tanesinde, dışarıdaki {av['count']} maçının {av['scored2']} tanesinde en az 2 gol attı; rakip evinde maç başına {hv['conceded']:.1f} gol yiyor.", "rank": 5})

    if (enough_venue and h["under35"] / h["count"] >= .75
            and a["under35"] / a["count"] >= .75 and venue_goals <= 3.1):
        picks.append({"market": "UNDER_3_5", "label": "3,5 ALT",
                      "reason": f"Dört golün altında kalan maç: {h['under35']}/{h['count']} ve {a['under35']}/{a['count']}; iç/dış saha toplam ortalaması {venue_goals:.1f}.", "rank": 2})
    if (enough_venue and h["over35"] / h["count"] >= .375
            and a["over35"] / a["count"] >= .375 and venue_goals >= 3.4):
        picks.append({"market": "OVER_3_5", "label": "3,5 ÜST",
                      "reason": f"Dört veya daha fazla gol: {h['over35']}/{h['count']} ve {a['over35']}/{a['count']}; iç/dış saha toplam ortalaması {venue_goals:.1f}.", "rank": 3})

    if hv["count"] >= 3 and av["count"] >= 3:
        home_edge = hv["scored"] - hv["conceded"]
        away_edge = av["scored"] - av["conceded"]
        home_wins, away_wins = hv["wins"] / hv["count"], av["wins"] / av["count"]
        if (home_wins >= .50 and away_wins <= .40
                and home_edge - away_edge >= .7 and h["wins"] / h["count"] >= .375):
            picks.append({"market": "HOME", "label": "MS 1",
                          "reason": f"İç saha galibiyeti {hv['wins']}/{hv['count']}, rakibin deplasman galibiyeti {av['wins']}/{av['count']}; gol farkı ev sahibinden yana.", "rank": 5})
        elif (away_wins >= .50 and home_wins <= .40
              and away_edge - home_edge >= .7 and a["wins"] / a["count"] >= .375):
            picks.append({"market": "AWAY", "label": "MS 2",
                          "reason": f"Deplasman galibiyeti {av['wins']}/{av['count']}, ev sahibinin iç saha galibiyeti {hv['wins']}/{hv['count']}; gol farkı deplasmandan yana.", "rank": 5})
        if (hv["wins"] + hv["draws"]) / hv["count"] >= .67 and away_wins <= .33 and home_edge >= away_edge:
            picks.append({"market": "DC_1X", "label": "1X",
                          "reason": f"Ev sahibi iç sahada {hv['wins'] + hv['draws']}/{hv['count']} kez kaybetmedi; rakibin dış saha galibiyeti {av['wins']}/{av['count']}.", "rank": 2})
        if (av["wins"] + av["draws"]) / av["count"] >= .67 and home_wins <= .33 and away_edge >= home_edge:
            picks.append({"market": "DC_X2", "label": "X2",
                          "reason": f"Deplasman takımı dış sahada {av['wins'] + av['draws']}/{av['count']} kez kaybetmedi; ev sahibinin iç saha galibiyeti {hv['wins']}/{hv['count']}.", "rank": 2})

    if (not any(p["market"] == "OVER_2_5" for p in picks)
            and h["over15"] / h["count"] >= .625 and a["over15"] / a["count"] >= .625
            and home_goals >= 2.4 and away_goals >= 2.4):
        picks.append({"market": "OVER_1_5", "label": "1,5 ÜST",
                      "reason": f"En az iki gol: {h['over15']}/{h['count']} ve {a['over15']}/{a['count']}; daha yüksek gol çizgisi için işaret zayıf.", "rank": 1})
    lineup_data = lineups or {"confirmed": False}
    for pick in picks:
        h2h_fit, h2h_note = h2h_market_context(pick["market"], h2h or {})
        pick["h2h_fit"] = h2h_fit
        pick["h2h_note"] = h2h_note
        pick["reason"] += " " + h2h_note
        pick["rank"] += h2h_fit
        fit, note = lineup_market_note(pick["market"], lineup_data)
        pick["lineup_fit"] = fit
        pick["lineup_note"] = note
        pick["rank"] += fit
    return sorted(picks, key=lambda p: p["rank"], reverse=True)


def prematch_pick_explanation(market: str, home_name: str, away_name: str,
                              home: Dict[str, Any], away: Dict[str, Any]) -> str:
    """Seçimi yalnızca kullanılan son maç ve iç/dış saha verisiyle açıkla."""
    h, a = home["all"], away["all"]
    hv, av = home["home"], away["away"]
    h_total = h["scored"] + h["conceded"]
    a_total = a["scored"] + a["conceded"]
    venue_total = (hv["scored"] + hv["conceded"] + av["scored"] + av["conceded"]) / 2
    if market == "OVER_2_5":
        return (f"{home_name} son {h['count']} maçının {h['over25']} tanesinde, "
                f"{away_name} ise {a['count']} maçının {a['over25']} tanesinde 3 veya daha fazla gol gördü. "
                f"Maç başı toplam gol ortalamaları sırasıyla {h_total:.1f} ve {a_total:.1f}; "
                f"ev sahibinin iç saha ({hv['count']} maç) ve rakibinin deplasman ({av['count']} maç) "
                f"verilerinin birleşik ortalaması {venue_total:.1f}. "
                "İki takımda da üç gol çizgisini destekleyen eğilim olduğu için 2,5 ÜST seçildi.")
    if market == "UNDER_2_5":
        return (f"{home_name} son {h['count']} maçının {h['under25']} tanesini, "
                f"{away_name} ise {a['count']} maçının {a['under25']} tanesini en fazla 2 golle tamamladı. "
                f"Maç başı toplam gol ortalamaları {h_total:.1f} ve {a_total:.1f}; "
                f"iç saha/deplasman birleşik ortalaması {venue_total:.1f} ({hv['count']} ve {av['count']} maç). "
                "Düşük gol eğilimi iki tarafta da görüldüğü için 2,5 ALT seçildi.")
    if market in ("OVER_3_5", "UNDER_3_5"):
        key = "over35" if market == "OVER_3_5" else "under35"
        outcome = "en az 4 golle" if market == "OVER_3_5" else "en fazla 3 golle"
        return (f"{home_name} son {h['count']} maçının {h[key]} tanesini, "
                f"{away_name} {a['count']} maçının {a[key]} tanesini {outcome} bitirdi. "
                f"İç saha/deplasman birleşik gol ortalaması {venue_total:.1f} "
                f"({hv['count']} ve {av['count']} maç). Bu ortak eğilim 3,5 çizgisi için işaret veriyor.")
    if market in ("HOME_OVER_1_5", "AWAY_OVER_1_5"):
        attacking, attack_venue, opponent_venue, team = (
            (h, hv, av, home_name) if market == "HOME_OVER_1_5"
            else (a, av, hv, away_name))
        venue_name = "iç sahada" if market == "HOME_OVER_1_5" else "deplasmanda"
        return (f"{team} son {attacking['count']} maçının {attacking['scored2']} tanesinde "
                f"en az 2 gol attı; {venue_name} {attack_venue['count']} maçının "
                f"{attack_venue['scored2']} tanesinde bunu başardı. "
                f"Bu sahada maç başına {attack_venue['scored']:.1f} gol atıyor; "
                f"buradaki {attack_venue['count']} maçının {attack_venue['wins']} tanesini kazandı. "
                f"Rakibi kendi sahası/deplasmanında {opponent_venue['count']} maçta "
                f"ortalama {opponent_venue['conceded']:.1f} gol yedi. "
                "Takımın kendi gol üretimi bu seçimi destekliyor.")
    if market == "BTTS_YES":
        return (f"İki takımın da gol attığı maç sayısı: {home_name} {h['btts']}/{h['count']}, "
                f"{away_name} {a['btts']}/{a['count']}. İç sahada {home_name} "
                f"{hv['count']} maçta ortalama {hv['conceded']:.1f} gol yedi; "
                f"deplasmanda {away_name} {av['count']} maçta ortalama {av['conceded']:.1f} gol yedi. "
                "Her iki takımın karşılıklı gol geçmişi ve gol yeme ortalaması KG VAR seçimini destekliyor.")
    if market == "BTTS_NO":
        weaker = home_name if h["scored"] <= a["scored"] else away_name
        weaker_rate = min(h["scored"], a["scored"])
        return (f"Karşılıklı gol son maçlarda {home_name} için {h['btts']}/{h['count']}, "
                f"{away_name} için {a['btts']}/{a['count']} kaldı. "
                f"{weaker} maç başına yalnızca {weaker_rate:.1f} gol atıyor. "
                "İki takımda da KG sıklığı düşük ve en az bir tarafın gol üretimi sınırlı olduğu için KG YOK seçildi.")
    if market in ("HOME", "DC_1X"):
        return (f"{home_name} iç sahadaki {hv['count']} maçının {hv['wins']} tanesini kazandı, "
                f"{hv['wins'] + hv['draws']} tanesinde yenilmedi. "
                f"{away_name} deplasmandaki {av['count']} maçının {av['wins']} tanesini kazandı. "
                f"İç saha gol farkı {hv['scored'] - hv['conceded']:+.1f}, "
                f"deplasman gol farkı {av['scored'] - av['conceded']:+.1f}; "
                + ("ev sahibinin galibiyet eğilimi MS 1 seçimini destekliyor." if market == "HOME"
                   else "ev sahibinin yenilmeme eğilimi 1X seçimini destekliyor."))
    if market in ("AWAY", "DC_X2"):
        return (f"{away_name} deplasmandaki {av['count']} maçının {av['wins']} tanesini kazandı, "
                f"{av['wins'] + av['draws']} tanesinde yenilmedi. "
                f"{home_name} iç sahadaki {hv['count']} maçının {hv['wins']} tanesini kazandı. "
                f"Deplasman gol farkı {av['scored'] - av['conceded']:+.1f}, "
                f"ev sahibinin iç saha gol farkı {hv['scored'] - hv['conceded']:+.1f}; "
                + ("deplasman galibiyet eğilimi MS 2 seçimini destekliyor." if market == "AWAY"
                   else "deplasman takımının yenilmeme eğilimi X2 seçimini destekliyor."))
    if market == "OVER_1_5":
        return (f"{home_name} son {h['count']} maçının {h['over15']} tanesinde, "
                f"{away_name} {a['count']} maçının {a['over15']} tanesinde en az 2 gol gördü. "
                f"Maç başı toplam gol ortalamaları {h_total:.1f} ve {a_total:.1f}. "
                "Üç gol çizgisi için ortak işaret yeterli olmadığı için yalnızca 1,5 ÜST seçildi.")
    return ""


PREMATCH_BOOKMAKERS = (
    "pinnacle", "pinnaclesports", "bet365", "betfair", "betfairexchange",
    "unibet", "bwin", "williamhill", "1xbet", "marathonbet", "10bet",
    "888sport", "betway", "betsson", "nordicbet", "betano", "22bet",
)


def prematch_fixture_odds(fixture_id: int
                          ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    key = f"prematch_odds:{fixture_id}"
    with cache_lock:
        cached = prematch_cache.get(key)
        if cached and time.time() - cached["ts"] < PREMATCH_ODDS_CACHE_SECONDS:
            return cached["data"], None
    all_entries = []
    for page in (1, 2):
        data, error = api_get("/odds", {"fixture": fixture_id, "page": page})
        if error or data is None:
            if page == 1:
                return None, error or "Maç önü oran verisi alınamadı."
            break
        entries = data.get("response")
        if not isinstance(entries, list):
            return None, "Beklenmeyen oran yanıtı."
        all_entries.extend(entries)
        total_pages = safe_int((data.get("paging") or {}).get("total"), 1)
        if page >= total_pages:
            break
    with cache_lock:
        prematch_cache[key] = {"ts": time.time(), "data": all_entries}
    return all_entries, None


def prematch_odd_for_market(entries: List[Dict[str, Any]], market_code: str
                            ) -> Optional[Dict[str, Any]]:
    wanted_total = {
        "OVER_1_5": ("over", 1.5), "OVER_2_5": ("over", 2.5),
        "UNDER_2_5": ("under", 2.5),
        "OVER_3_5": ("over", 3.5), "UNDER_3_5": ("under", 3.5),
        "HOME_OVER_1_5": ("over", 1.5),
        "AWAY_OVER_1_5": ("over", 1.5),
    }
    results = []
    for entry in entries:
        for bookmaker in entry.get("bookmakers") or []:
            book_name = str(bookmaker.get("name") or "")
            book_key = re.sub(r"[^a-z0-9]", "", book_name.lower())
            if book_key not in PREMATCH_BOOKMAKERS:
                continue
            for bet in bookmaker.get("bets") or []:
                name = str(bet.get("name") or "").strip().lower()
                is_result = name in ("match winner", "1x2", "full time result", "fulltime result")
                is_double = name == "double chance"
                is_btts = name in ("both teams score", "both teams to score", "btts")
                is_total = name in ("goals over/under", "total goals", "over/under")
                is_home_total = name in (
                    "home team over/under", "home team total goals",
                    "home team goals over/under", "home goals over/under",
                    "home team total", "home total goals",
                )
                is_away_total = name in (
                    "away team over/under", "away team total goals",
                    "away team goals over/under", "away goals over/under",
                    "away team total", "away total goals",
                )
                for value in bet.get("values") or []:
                    selection = str(value.get("value") or "").strip()
                    normalized = re.sub(r"\s+", "", selection.lower())
                    matches = False
                    right_total = (is_home_total if market_code == "HOME_OVER_1_5"
                                   else is_away_total if market_code == "AWAY_OVER_1_5"
                                   else is_total)
                    if market_code in wanted_total and right_total:
                        direction, line = wanted_total[market_code]
                        found_line = _float_line(value.get("handicap"))
                        if found_line is None:
                            found_line = _float_line(selection)
                        matches = normalized.startswith(direction) and found_line is not None and abs(found_line - line) < .01
                    elif market_code == "BTTS_YES" and is_btts:
                        matches = normalized in ("yes", "evet")
                    elif market_code == "BTTS_NO" and is_btts:
                        matches = normalized in ("no", "hayır", "hayir")
                    elif market_code == "HOME" and is_result:
                        matches = normalized in ("home", "1")
                    elif market_code == "AWAY" and is_result:
                        matches = normalized in ("away", "2")
                    elif market_code == "DC_1X" and is_double:
                        matches = normalized in ("home/draw", "1x", "homedraw")
                    elif market_code == "DC_X2" and is_double:
                        matches = normalized in ("draw/away", "x2", "drawaway")
                    if not matches:
                        continue
                    odd = _float_odd(value.get("odd"))
                    if odd is None or odd < PREMATCH_MIN_ODD:
                        continue
                    results.append({
                        "odd": round(odd, 2), "bookmaker": book_name,
                        "market_name": bet.get("name"), "selection": selection,
                        "updated": entry.get("update"),
                        "book_priority": PREMATCH_BOOKMAKERS.index(book_key),
                    })
    if not results:
        return None
    # Önceden belirlenen yabancı sağlayıcı sırası; uç bir en yüksek oranı seçme.
    results.sort(key=lambda quote: (quote["book_priority"], quote["odd"]))
    chosen = results[0]
    chosen.pop("book_priority", None)
    return chosen


def prematch_result_market_check(entries: List[Dict[str, Any]], market_code: str,
                                 quote: Dict[str, Any]) -> Tuple[bool, str]:
    """MS seçiminde aynı sağlayıcının 1/2 fiyatlarını karşılaştır."""
    if market_code not in ("HOME", "AWAY"):
        return True, ""
    wanted_book = re.sub(r"[^a-z0-9]", "", str(quote.get("bookmaker") or "").lower())
    home_odd = away_odd = None
    for entry in entries:
        for bookmaker in entry.get("bookmakers") or []:
            book_key = re.sub(r"[^a-z0-9]", "", str(bookmaker.get("name") or "").lower())
            if book_key != wanted_book:
                continue
            for bet in bookmaker.get("bets") or []:
                name = str(bet.get("name") or "").strip().lower()
                if name not in ("match winner", "1x2", "full time result", "fulltime result"):
                    continue
                for value in bet.get("values") or []:
                    selection = re.sub(r"\s+", "", str(value.get("value") or "").lower())
                    odd = _float_odd(value.get("odd"))
                    if selection in ("home", "1"):
                        home_odd = odd
                    elif selection in ("away", "2"):
                        away_odd = odd
                if home_odd is not None and away_odd is not None:
                    break
    if home_odd is None or away_odd is None:
        return False, "MS piyasa karşılaştırması tamamlanamadı."
    selected = home_odd if market_code == "HOME" else away_odd
    opponent = away_odd if market_code == "HOME" else home_odd
    note = f" Piyasa kontrolü: seçilen taraf {selected:.2f}, rakip {opponent:.2f}."
    # 3.23'e karşı 1.60 gibi açık favori terslerini ve belirgin fiyat
    # ayrışmalarını ele; küçük farklarda modele hareket alanı bırak.
    if (opponent <= 1.90 and selected >= 2.50) or selected >= opponent * 1.45:
        return False, note.strip()
    return True, note


@app.route("/api/prematch/fixtures")
def api_prematch_fixtures():
    day = prematch_day(request.args.get("date", ""))
    if day is None:
        return jsonify({"error": "Bugünden sonraki 7 gün içinde geçerli bir tarih seç."}), 400
    matches, error = prematch_fixtures(day)
    if error:
        return jsonify({"error": error}), 503
    result = []
    for match in matches or []:
        fixture = match.get("fixture") or {}
        teams = match.get("teams") or {}
        league = match.get("league") or {}
        result.append({
            "id": fixture.get("id"), "kickoff": fixture.get("date"),
            "home": (teams.get("home") or {}).get("name"),
            "away": (teams.get("away") or {}).get("name"),
            "league_id": league.get("id"), "league": league.get("name"),
            "country": league.get("country"),
        })
    result.sort(key=lambda x: x.get("kickoff") or "")
    return jsonify({"date": day.isoformat(), "count": len(result), "matches": result})


@app.route("/api/prematch/analyze")
def api_prematch_analyze():
    day = prematch_day(request.args.get("date", ""))
    fixture_id = safe_int(request.args.get("fixture"), 0)
    if day is None or fixture_id <= 0:
        return jsonify({"error": "Geçerli tarih ve maç seç."}), 400
    matches, error = prematch_fixtures(day)
    if error:
        return jsonify({"error": error}), 503
    match = next((m for m in matches or []
                  if safe_int((m.get("fixture") or {}).get("id")) == fixture_id), None)
    if match is None:
        return jsonify({"error": "Bu tarihte başlamamış bir maç bulunamadı."}), 404
    fixture = match.get("fixture") or {}
    try:
        if datetime.fromisoformat(fixture.get("date") or "").astimezone(ISTANBUL_TZ) <= datetime.now(ISTANBUL_TZ):
            return jsonify({"error": "Maç başladı; maç önü analizi kapandı."}), 409
    except ValueError:
        return jsonify({"error": "Maç başlangıç saati bulunamadı."}), 422
    teams = match.get("teams") or {}
    home_team, away_team = teams.get("home") or {}, teams.get("away") or {}
    home_id, away_id = safe_int(home_team.get("id")), safe_int(away_team.get("id"))
    if not home_id or not away_id:
        return jsonify({"error": "Takım bilgileri eksik."}), 422
    home, home_error = prematch_team_form(home_id, fixture.get("date") or "")
    away, away_error = prematch_team_form(away_id, fixture.get("date") or "")
    if home_error or away_error:
        return jsonify({"error": home_error or away_error}), 503
    h2h, h2h_error = prematch_h2h(home_id, away_id, fixture.get("date") or "")
    lineups, lineup_error = prematch_lineups(fixture_id, home_id, away_id)
    suggestions = prematch_suggestions(home or {}, away or {}, lineups, h2h)
    odds_entries, odds_error = prematch_fixture_odds(fixture_id) if suggestions else ([], None)
    priced = []
    if odds_entries is not None:
        for suggestion in suggestions:
            quote = prematch_odd_for_market(odds_entries, suggestion["market"])
            market_note = ""
            if quote and suggestion["market"] in ("HOME", "AWAY"):
                market_ok, market_note = prematch_result_market_check(
                    odds_entries, suggestion["market"], quote)
                if not market_ok:
                    quote = None
            # 1,5 ÜST yalnızca gerçek bir alternatif kalmadığında ve daha
            # anlamlı bir fiyatla gösterilir; ekranı kolay seçimlerle doldurma.
            if suggestion["market"] == "OVER_1_5" and quote and quote["odd"] < 1.50:
                quote = None
            if quote:
                priced.append({
                    "market": suggestion["market"],
                    "label": suggestion["label"], "reason": suggestion["reason"],
                    "explanation": prematch_pick_explanation(
                        suggestion["market"], home_team.get("name") or "Ev sahibi",
                        away_team.get("name") or "Deplasman", home, away)
                        + " İkili rekabet: " + suggestion["h2h_note"]
                        + " Kadro etkisi: " + suggestion["lineup_note"] + market_note,
                    "lineup_fit": suggestion["lineup_fit"],
                    "confidence": prematch_confidence(
                        suggestion["market"], suggestion["rank"], lineups,
                        suggestion["lineup_fit"]),
                    "quote": quote,
                })
    if any(item["label"] != "1,5 ÜST" for item in priced):
        priced = [item for item in priced if item["label"] != "1,5 ÜST"]
    # İlişkili iki seçimin kendi gerçek oranlarını ve açıklamalarını birlikte koru.
    paired_markets = (
        ("KG VAR", "2,5 ÜST"), ("1X", "3,5 ALT"),
        ("1X", "3,5 ÜST"), ("X2", "3,5 ALT"), ("X2", "3,5 ÜST"),
    )
    # MS seçimini ilk üçe zorla sokma; model sırası ve fiyat koşulu belirlesin.
    displayed = priced[:5]
    for first, second in paired_markets:
        if first in {x["label"] for x in displayed}:
            partner = next((x for x in priced if x["label"] == second), None)
            if partner and partner not in displayed:
                displayed.append(partner)
        elif second in {x["label"] for x in displayed}:
            partner = next((x for x in priced if x["label"] == first), None)
            if partner and partner not in displayed:
                displayed.append(partner)
    priced = displayed[:5]
    labels = {item["label"] for item in priced}
    joint_notes = []
    if {"KG VAR", "2,5 ÜST"} <= labels:
        joint_notes.append("KG VAR ve 2,5 ÜST birlikte de gerçekleşebilir: iki takımın karşılıklı gol verisi ve maçların 3+ gole çıkma sıklığı ayrı ayrı eşikleri karşılıyor. İki seçim için gösterilen oranlar birbirinden bağımsızdır; birleşik oran değildir.")
    for side in ("1X", "X2"):
        for goal in ("3,5 ALT", "3,5 ÜST"):
            if {side, goal} <= labels:
                joint_notes.append(f"{side} ve {goal} birlikte de gerçekleşebilir: yenilmeme ve gol çizgisi eğilimleri ayrı ayrı koşulları sağlıyor. Gösterilen oranlar tekli piyasalara aittir.")
    if odds_error:
        odds_note = f"Oran sağlayıcısı yanıt vermedi: {odds_error}"
    elif suggestions and not priced:
        odds_note = "Veri eğilimi bulundu; uygun yabancı oran bulunamadı (genel alt sınır 1,30; 1,5 ÜST için 1,50)."
    elif not suggestions:
        odds_note = "Veri eğilimi yeterli değil; oran araması yapılmadı."
    else:
        odds_note = "Oranlar bilgi amaçlıdır; sağlayıcıda değişebilir."
    return jsonify({
        "id": fixture_id, "home": home_team.get("name"), "away": away_team.get("name"),
        "home_form": home, "away_form": away,
        "h2h": h2h, "h2h_error": h2h_error,
        "suggestions": priced,
        "joint_notes": joint_notes,
        "lineups": lineups,
        "lineup_note": ("Onaylı ilk 11 analize dahil edildi."
                        if lineups.get("confirmed")
                        else "Kadrolar henüz açıklanmadı; analiz form verileriyle hazırlandı."),
        "lineup_error": lineup_error,
        "decision": "PAS" if not priced else "EĞİLİM",
        "candidate_count": len(suggestions), "odds_note": odds_note,
        "note": ("Son maç formu, iç/dış saha verisi, onaylı ilk 11 ve diziliş birlikte değerlendirildi."
                 if lineups.get("confirmed") else
                 "Son maç formu ve iç/dış saha verisi değerlendirildi; kadrolar açıklanınca analiz güncellenir."),
    })


@app.route("/api/prematch/result")
def api_prematch_result():
    fixture_id = safe_int(request.args.get("fixture"), 0)
    if fixture_id <= 0:
        return jsonify({"error": "Geçerli maç numarası gerekli."}), 400
    items, error = prematch_api_cached(
        f"result:{fixture_id}", "/fixtures", {"id": fixture_id}, 300)
    if error:
        return jsonify({"error": error}), 503
    if not items:
        return jsonify({"error": "Maç sonucu bulunamadı."}), 404
    item = items[0]
    status = ((item.get("fixture") or {}).get("status") or {}).get("short")
    goals = item.get("goals") or {}
    fulltime = ((item.get("score") or {}).get("fulltime") or {})
    finished = status in ("FT", "AET", "PEN")
    home_goals = fulltime.get("home")
    away_goals = fulltime.get("away")
    if home_goals is None or away_goals is None:
        home_goals, away_goals = goals.get("home"), goals.get("away")
    return jsonify({
        "fixture": fixture_id, "status": status, "finished": finished,
        "home_goals": home_goals, "away_goals": away_goals,
    })


@app.route("/api/live-leagues")
def api_live_leagues():
    """
    TEŞHİS ENDPOINT'İ
    Ekstra API çağrısı YAPMAZ.
    Scanner'ın zaten aldığı /fixtures?live=all cevabını cache'ten okur.
    """
    all_live = []
    snapshot_ts = 0.0
    source = "memory"

    # Önce paylaşılan /tmp snapshot'ını oku.
    try:
        with open(LIVE_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        all_live = payload.get("all_live", []) or []
        snapshot_ts = float(payload.get("ts", 0.0) or 0.0)
        source = "shared_file"
    except Exception:
        # Dosya henüz oluşmadıysa mevcut process RAM cache'ine dön.
        with cache_lock:
            all_live = list(live_cache.get("all_live", []))
            snapshot_ts = float(live_cache.get("ts", 0.0) or 0.0)
            source = "memory"

    cache_age = None
    if snapshot_ts > 0:
        cache_age = round(max(0.0, time.time() - snapshot_ts), 1)

    leagues = {}

    for match in all_live:
        fixture = match.get("fixture") or {}
        league = match.get("league") or {}
        teams = match.get("teams") or {}
        goals = match.get("goals") or {}

        league_id = league.get("id")
        if league_id is None:
            continue

        if league_id not in leagues:
            leagues[league_id] = {
                "league_id": league_id,
                "league": league.get("name", ""),
                "country": league.get("country", ""),
                "allowed": is_allowed_competition(league),
                "match_count": 0,
                "matches": [],
            }

        leagues[league_id]["match_count"] += 1

        if len(leagues[league_id]["matches"]) < 5:
            leagues[league_id]["matches"].append({
                "fixture_id": fixture.get("id"),
                "home": (teams.get("home") or {}).get("name"),
                "away": (teams.get("away") or {}).get("name"),
                "minute": ((fixture.get("status") or {}).get("elapsed")),
                "score": f"{goals.get('home', 0)}-{goals.get('away', 0)}",
            })

    result = sorted(
        leagues.values(),
        key=lambda x: (x["allowed"], x["match_count"], x["country"], x["league"]),
        reverse=True,
    )

    return jsonify({
        "ok": True,
        "note": "Bu endpoint ekstra API isteği yapmaz; scanner'ın mevcut canlı maç snapshot'ını gösterir.",
        "source": source,
        "cache_age_seconds": cache_age,
        "total_live_matches": len(all_live),
        "unique_live_leagues": len(result),
        "allowed_live_leagues": sum(1 for x in result if x["allowed"]),
        "leagues": result,
    })


@app.route("/api/live-ticker")
def api_live_ticker():
    """Scanner'ın mevcut fikstür snapshot'ından canlı skor ve son golü döndürür."""
    all_live = []
    snapshot_ts = 0.0
    try:
        with open(LIVE_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        all_live = payload.get("all_live", []) or []
        snapshot_ts = float(payload.get("ts", 0.0) or 0.0)
    except Exception:
        with cache_lock:
            all_live = list(live_cache.get("all_live", []))
            snapshot_ts = float(live_cache.get("ts", 0.0) or 0.0)

    matches = []
    for match in all_live:
        league = match.get("league") or {}
        if not is_allowed_competition(league):
            continue
        fixture = match.get("fixture") or {}
        teams = match.get("teams") or {}
        goals = match.get("goals") or {}
        status = fixture.get("status") or {}
        goal_events = []
        for event in match.get("events") or []:
            if str(event.get("type") or "").lower() != "goal":
                continue
            detail = str(event.get("detail") or "")
            if "missed" in detail.lower():
                continue
            goal_events.append(event)
        last_goal = goal_events[-1] if goal_events else None
        last_goal_data = None
        if last_goal:
            event_time = last_goal.get("time") or {}
            last_goal_data = {
                "team": (last_goal.get("team") or {}).get("name"),
                "player": (last_goal.get("player") or {}).get("name"),
                "assist": (last_goal.get("assist") or {}).get("name"),
                "minute": event_time.get("elapsed"),
                "extra": event_time.get("extra"),
                "detail": last_goal.get("detail"),
            }
        matches.append({
            "fixture_id": fixture.get("id"),
            "status": status.get("short"),
            "minute": status.get("elapsed"),
            "league": league.get("name"),
            "country": league.get("country"),
            "home": (teams.get("home") or {}).get("name"),
            "away": (teams.get("away") or {}).get("name"),
            "home_goals": safe_int(goals.get("home"), 0),
            "away_goals": safe_int(goals.get("away"), 0),
            "last_goal": last_goal_data,
        })
    matches.sort(key=lambda x: (safe_int(x.get("minute"), 0), x.get("league") or ""), reverse=True)
    return jsonify({
        "count": len(matches),
        "matches": matches,
        "snapshot_age_seconds": (round(max(0.0, time.time() - snapshot_ts), 1)
                                 if snapshot_ts else None),
    })


@app.route("/api/status")
def api_status():
    # API çağrısı YAPMAZ. Scanner'ın /tmp ortak state dosyasını okur.
    shared = read_shared_state()

    if shared and isinstance(shared.get("status"), dict):
        data = dict(shared["status"])
        state_age = round(max(0.0, time.time() - float(shared.get("written_at", 0.0))), 1)
        data["state_source"] = "shared_file"
        data["state_age_seconds"] = state_age
    else:
        # Scanner henüz ilk state'i yazmadıysa local başlangıç durumunu göster.
        with status_lock:
            data = dict(scanner_status)
        data["state_source"] = "local_fallback"
        data["state_age_seconds"] = None

    if shared and isinstance(shared.get("bot_pick_stats"), dict):
        data["bot_pick_stats"] = shared.get("bot_pick_stats")
    else:
        data["bot_pick_stats"] = bot_pick_stats()

    data.update({
        "ok": True,
        "api_key_configured": bool(API_KEY),
        "check_seconds": CHECK_SECONDS,
        "first_half_check_seconds": FIRST_HALF_CHECK_SECONDS,
        "signal_limit": SIGNAL_LIMIT,
        "first_half_limit": FIRST_HALF_LIMIT,
        "first_half_15_limit": FIRST_HALF_15_LIMIT,
        "bot_pick_limit": BOT_PICK_LIMIT,
        "bot_pick_confirm_scans": BOT_PICK_CONFIRM_SCANS,
        "momentum_window_seconds": MOMENTUM_WINDOW_SECONDS,
        "live_odds_min": LIVE_ODDS_MIN,
        "live_odds_cache_ttl": LIVE_ODDS_CACHE_TTL,
        "allowed_league_count": len(ALLOWED_LEAGUES),
    })

    return jsonify(data)


@app.route("/api/matches")
def api_matches():
    # API çağrısı YAPMAZ. Scanner'ın tamamladığı son analiz snapshot'ını okur.
    shared = read_shared_state()

    if shared and isinstance(shared.get("matches"), list):
        items = shared["matches"]
        source = "shared_file"
        state_age = round(max(0.0, time.time() - float(shared.get("written_at", 0.0))), 1)
    else:
        items = serialize_matches_for_web()
        source = "local_fallback"
        state_age = None

    return jsonify({
        "count": len(items),
        "matches": items,
        "bot_pick_stats": (shared or {}).get("bot_pick_stats", bot_pick_stats()),
        "bot_pick_history": (shared or {}).get("bot_pick_history", list(bot_pick_history[-50:])),
        "source": source,
        "state_age_seconds": state_age,
    })


# ============================================================
# WEB ARAYÜZÜ
# ============================================================

PAGE = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gol Sinyal Merkezi</title>
<style>
:root{
  --bg:#04111d;
  --panel:#081a2a;
  --panel2:#0b2033;
  --line:#164561;
  --text:#f4f8fc;
  --muted:#9ab3c8;
  --green:#14da83;
  --green2:#55efad;
  --yellow:#f6c925;
  --orange:#ff932f;
  --red:#ff3f4f;
  --blue:#20a9ff;
  --cyan:#25d6ef;
  --purple:#c84cff;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:linear-gradient(180deg,#04101b 0%,#061522 100%);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}
body{overflow-x:hidden;padding-bottom:42px}
.wrap{width:min(1880px,calc(100% - 26px));margin:0 auto;padding:16px 0 22px}

/* HEADER */
.header{
  display:flex;align-items:center;justify-content:space-between;gap:16px;
  padding-bottom:13px;border-bottom:1px solid #0f3851
}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.logo{
  width:48px;height:48px;border-radius:50%;display:grid;place-items:center;
  background:radial-gradient(circle at 35% 30%,#2d8ff3,#164d8c 70%);
  border:1px solid #4397e5;font-size:30px;box-shadow:0 0 20px rgba(32,169,255,.18)
}
.brand-text{min-width:0}
.title{font-size:26px;font-weight:900;letter-spacing:-.3px;white-space:nowrap}
.subtitle{font-size:12px;color:#b4c8d9;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:600px}
.header-right{display:flex;align-items:center;gap:9px;flex-wrap:wrap;justify-content:flex-end}
.status-pill,.info-pill,.icon-btn{
  border:1px solid #123d59;background:#092038;color:#d9e8f5;border-radius:10px;
  padding:9px 12px;font-size:11px
}
.status-pill{background:#063d28;border-color:#0d7548;color:#57f0a2;font-weight:900}
.icon-btn{cursor:pointer;padding:9px 11px;font-size:15px}
.icon-btn:hover{border-color:#2b789d}

/* FILTERS */
.toolbar{
  display:flex;align-items:center;justify-content:space-between;gap:12px;
  margin:12px 0
}
.tabs{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.tab{
  border:1px solid #174765;background:#092038;color:#d9e7f3;
  padding:9px 13px;border-radius:9px;font-size:11px;cursor:pointer;transition:.15s
}
.tab:hover{border-color:#2881aa}
.tab.active{
  background:linear-gradient(180deg,#19d981,#11ad65);
  border-color:#38ef9b;color:#062315;font-weight:900
}
.controls{display:flex;gap:8px;align-items:center}
select{
  min-width:240px;background:#092038;border:1px solid #174765;color:#eef5fb;
  padding:9px 12px;border-radius:9px;font-size:11px
}
.view-btn{
  width:42px;height:36px;border:1px solid #174765;background:#092038;color:#dceaf6;
  border-radius:9px;cursor:pointer;font-size:16px
}
.view-btn.active{background:#10a963;border-color:#24df85;color:white}

/* ERROR */
#errorBox{
  display:none;background:#3a1620;border:1px solid #873245;color:#ffdce2;
  border-radius:10px;padding:10px 12px;font-size:12px;margin-bottom:10px
}

/* GRID */
.grid{
  display:grid;
  grid-template-columns:repeat(4,minmax(0,1fr));
  gap:11px
}
.card{
  position:relative;overflow:hidden;
  background:linear-gradient(145deg,#081c2d,#061522);
  border:1px solid #145273;border-radius:12px;padding:12px 12px 10px;
  min-height:252px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.02)
}
.card::before{
  content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:#1b83b7
}
.card.very::before{background:var(--green)}
.card.strong::before{background:var(--yellow)}
.card.mid::before{background:var(--orange)}
.card.weak::before{background:var(--red)}
.card.bot{
  border-color:#a83cff;
  box-shadow:0 0 0 1px rgba(200,76,255,.32),0 0 22px rgba(200,76,255,.12)
}
.card.bot::before{width:4px;background:var(--purple)}

/* Öne çıkan BOT PICK, geniş okunan maç özeti */
.card.featured{
  grid-column:span 1;
  background:#1c283e;border:1px solid #405879;border-radius:18px;
  padding:18px 20px;min-height:0;color:#e8edf7;
  box-shadow:0 14px 32px rgba(0,0,0,.2)
}
.card.featured:not(.bot){padding:13px 14px}
.card.featured:not(.bot) .featured-title{font-size:14px}
.card.featured:not(.bot) .featured-info{gap:7px;font-size:11px}
.card.featured:not(.bot) .featured-teams{font-size:12px}
.card.featured:not(.bot) .featured-pick{font-size:12px}
.card.featured:not(.bot) .featured-divider{margin:10px 0}
.card.featured.bot{grid-column:span 2;border-color:#8662b1;box-shadow:0 0 0 1px rgba(155,102,218,.18),0 14px 32px rgba(0,0,0,.2)}
.card.featured::before{display:none}
.featured-head{display:flex;justify-content:space-between;align-items:center;gap:12px}
.featured-title{font-size:18px;font-weight:900;color:#62ddb1;overflow-wrap:anywhere}
.featured-status{border:1px solid #485b78;border-radius:24px;padding:6px 11px;color:#c9d7e8;font-size:11px;white-space:nowrap}
.featured-divider{height:1px;background:#46536c;margin:15px 0}
.featured-info{display:grid;gap:11px;font-size:13px;line-height:1.45}
.featured-teams{font-size:15px;font-weight:800;overflow-wrap:anywhere}
.featured-info b{color:#f2f5fc}
.pressure-dots{display:inline-flex;gap:5px;vertical-align:middle;margin:0 5px}
.pressure-dot{width:12px;height:12px;border-radius:50%;background:#48566c}
.pressure-dot.filled{background:#f38048;box-shadow:0 0 8px rgba(243,128,72,.35)}
.featured-foot{display:flex;justify-content:space-between;align-items:flex-end;gap:12px}
.featured-pick{font-size:16px;font-weight:900;overflow-wrap:anywhere}
.featured-odd{white-space:nowrap;font-weight:900;font-size:19px}
.featured-odd small{font-size:10px;color:#aabbd1;font-weight:700;margin-right:5px}
.featured-odd.unavailable{font-size:13px;color:#aabbd1}

/* CARD HEAD */
.card-head{
  display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px
}
.type{
  font-size:12px;font-weight:900;letter-spacing:.1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis
}
.type.bot-t{color:#e18cff}
.type.green-t{color:#4af0a0}
.type.blue-t{color:#48c4ff}
.type.yellow-t{color:#ffd64d}
.type.orange-t{color:#ffae63}
.type.red-t{color:#ff6b74}
.live-badge{
  color:#5bf2a6;border:1px solid #14a965;background:#073a27;
  padding:4px 8px;border-radius:7px;font-size:10px;font-weight:900
}

/* LEAGUE + MINUTE */
.meta{
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  font-size:10px;color:#b8cada;margin-bottom:9px
}
.league{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.minute{
  color:#21e789;font-size:14px;font-weight:900;white-space:nowrap
}

/* TEAMS */
.teams{
  display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);
  align-items:center;gap:8px;margin-bottom:10px
}
.team{
  display:flex;align-items:center;gap:7px;min-width:0;font-size:12px;font-weight:800
}
.team.away{justify-content:flex-end;text-align:right}
.team-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge-logo{
  width:23px;height:23px;object-fit:contain;flex:0 0 23px
}
.fake-ball{font-size:17px;opacity:.75}
.score{
  font-size:21px;font-weight:900;white-space:nowrap;background:#07121e;
  padding:4px 9px;border-radius:8px;min-width:68px;text-align:center
}

/* SIGNAL BADGES */
.signals{
  display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px
}
.signal{
  border-radius:7px;padding:6px 4px;text-align:center;font-weight:900;
  font-size:10px;background:#0b1d2e;border:1px solid #21465d
}
.signal.green{color:#50f0a5;border-color:#13b66e;background:#072a1c}
.signal.yellow{color:#ffd74a;border-color:#b99014;background:#282006}
.signal.orange{color:#ffae68;border-color:#b45c1f;background:#2c1608}
.signal.red{color:#ff6b74;border-color:#bc303b;background:#2b0d12}
.signal.blue{color:#5fc7ff;border-color:#1682ba;background:#09233a}
.signal.purple{color:#e397ff;border-color:#9f42ca;background:#250d31}
.signal.gray{color:#aebfd0;border-color:#29465c}

/* TEAM SIGNALS */
.team-line{
  font-size:10px;color:#b9cad9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  padding-bottom:8px;border-bottom:1px solid #12354d;margin-bottom:8px
}

/* STATS */
.stats{
  display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-bottom:9px
}
.stat{
  background:#0a1d2f;border:1px solid #12354d;border-radius:7px;
  padding:5px 4px;text-align:center;color:#c7d5e2;font-size:9px;white-space:nowrap
}

/* BOTTOM */
.bottom{
  display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:end;gap:8px
}
.play-label{
  color:#778fa3;font-size:8px;font-weight:900;letter-spacing:.5px;margin-bottom:3px
}
.pick{
  font-size:11px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis
}
.odd-wrap{text-align:right}
.odd-caption{font-size:8px;color:#8399ab;margin-bottom:3px}
.odd{
  min-width:64px;border-radius:7px;padding:6px 8px;font-size:15px;font-weight:900;
  text-align:center;border:1px solid #295574;background:#092038;color:#dce9f4
}
.odd.good{border-color:#13b96e;background:#072b1d;color:#81f0b3}
.odd.low{border-color:#b33a46;background:#2a0d12;color:#ff8990}

/* EMPTY */
.empty{
  grid-column:1/-1;border:1px dashed #24516c;border-radius:12px;
  color:#8ea9bf;text-align:center;padding:42px
}

/* FOOTER */
.footer{
  margin-top:12px;padding-top:10px;border-top:1px solid #12364e;
  display:flex;justify-content:space-between;gap:10px;align-items:center;
  color:#a5bacb;font-size:10px;flex-wrap:wrap
}
.legend{display:flex;gap:14px;flex-wrap:wrap}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}

/* CANLI SKOR BANDI */
.live-ticker{position:fixed;left:0;right:0;bottom:0;height:38px;z-index:1000;display:flex;align-items:center;background:#020b13;border-top:1px solid #17618a;box-shadow:0 -5px 18px rgba(0,0,0,.35);overflow:hidden}
.ticker-label{height:100%;display:flex;align-items:center;gap:6px;padding:0 14px;background:#e7193f;color:#fff;font-size:11px;font-weight:950;letter-spacing:.5px;flex:0 0 auto;z-index:2}
.ticker-dot{width:7px;height:7px;border-radius:50%;background:#fff;box-shadow:0 0 0 4px rgba(255,255,255,.16);animation:tickerPulse 1.2s infinite}
.ticker-window{overflow:hidden;white-space:nowrap;flex:1}
.ticker-track{display:inline-flex;align-items:center;gap:30px;padding-left:100%;font-size:12px;font-weight:800;color:#edf7ff;animation:tickerMove 90s linear infinite}
.ticker-item{display:inline-flex;align-items:center;gap:7px}.ticker-minute{color:#55efad}.ticker-score{color:#ffd75d;font-size:14px}.ticker-league{color:#7f9ab0;font-size:10px}
.goal-toast{position:fixed;left:50%;bottom:54px;z-index:1100;transform:translate(-50%,20px);min-width:min(520px,calc(100% - 24px));padding:15px 18px;border:1px solid #3ef09b;border-radius:14px;background:linear-gradient(135deg,#073a28,#09223a);box-shadow:0 12px 40px rgba(0,0,0,.55),0 0 28px rgba(20,218,131,.24);opacity:0;pointer-events:none;transition:.3s;text-align:center}
.goal-toast.show{opacity:1;transform:translate(-50%,0)}.goal-toast-title{color:#62f2ab;font-size:19px;font-weight:950}.goal-toast-body{margin-top:5px;color:#fff;font-size:13px;font-weight:800}
@keyframes tickerMove{to{transform:translateX(-100%)}}@keyframes tickerPulse{50%{opacity:.35}}

@media(max-width:1450px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1080px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.header{align-items:flex-start}.subtitle{max-width:420px}}
@media(max-width:760px){
  .wrap{width:calc(100% - 14px)}
  .header{flex-direction:column}.header-right{justify-content:flex-start}
  .toolbar{flex-direction:column;align-items:stretch}.controls{justify-content:space-between}
  select{min-width:0;flex:1}.grid{grid-template-columns:1fr}.title{font-size:22px}
  .card.featured.bot{grid-column:span 1}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="brand">
      <div class="logo">⚽</div>
      <div class="brand-text">
        <div class="title">Gol Sinyal Merkezi</div>
        <div class="subtitle">Canlı istatistiklerle en güçlü gol sinyallerini senin için bulur.</div>
      </div>
    </div>

    <div class="header-right">
      <a class="info-pill" href="/tahmin" style="text-decoration:none;color:inherit">📅 Maç Önü Tahminleri</a>
      <div class="status-pill" id="systemState">● Sistem Aktif</div>
      <div class="info-pill" id="lastScan">Son güncelleme: -</div>
      <div class="info-pill" id="scanEvery">↻ 30 sn'de bir</div>
      <button class="icon-btn" id="notificationBtn" onclick="requestNotifications()" title="Bildirimler">🔔</button>
    </div>
  </div>

  <div id="errorBox"></div>

  <div class="toolbar">
    <div class="tabs" id="leagueTabs">
      <button class="tab active" data-league="ALL">Tümü</button>
    </div>
    <div class="controls">
      <select id="sortMode">
        <option value="bot_desc">Sıralama: BOT Puanı (Yüksekten)</option>
        <option value="goal_desc">Gol Sinyali (Yüksekten)</option>
        <option value="pressure_desc">Baskı (Yüksekten)</option>
        <option value="minute_desc">Dakika (Yüksekten)</option>
      </select>
      <button class="view-btn active" title="Kart görünümü">▦</button>
    </div>
  </div>

  <div class="grid" id="matches"></div>

  <div class="footer">
    <div class="legend">
      <span><span class="dot" style="background:#14da83"></span>80+ Çok güçlü</span>
      <span><span class="dot" style="background:#f6c925"></span>65–79 Güçlü</span>
      <span><span class="dot" style="background:#ff932f"></span>45–64 Orta</span>
      <span><span class="dot" style="background:#ff3f4f"></span>0–44 Zayıf</span>
      <span>🧠 BOT PICK</span>
    </div>
    <div>Gol Sinyal Merkezi v4.10 &nbsp; | &nbsp; Gerçek istatistik, akıllı analiz.</div>
  </div>
</div>

<div class="live-ticker" aria-label="Canlı maç skorları">
  <div class="ticker-label"><span class="ticker-dot"></span> CANLI</div>
  <div class="ticker-window"><div class="ticker-track" id="tickerTrack"><span>Canlı skorlar yükleniyor…</span></div></div>
</div>
<div class="goal-toast" id="goalToast"><div class="goal-toast-title">⚽ GOL!</div><div class="goal-toast-body" id="goalToastBody"></div></div>

<script>
let currentMatches = [];
let currentLeague = "ALL";
let currentBotPickStats = null;
let liveScoreMemory = new Map();
let tickerInitialized = false;
let goalToastTimer = null;

function esc(v){
  return String(v ?? "")
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;");
}

function homeGoalsOf(m){
  if(m.home_goals!==undefined && m.home_goals!==null) return Number(m.home_goals||0);
  if(Array.isArray(m.score)) return Number(m.score[0]||0);
  if(m.goals && m.goals.home!==undefined) return Number(m.goals.home||0);
  return 0;
}
function awayGoalsOf(m){
  if(m.away_goals!==undefined && m.away_goals!==null) return Number(m.away_goals||0);
  if(Array.isArray(m.score)) return Number(m.score[1]||0);
  if(m.goals && m.goals.away!==undefined) return Number(m.goals.away||0);
  return 0;
}
function bttsDoneOf(m){
  return homeGoalsOf(m)>0 && awayGoalsOf(m)>0;
}

function leagueLabel(m){
  const c=m.country||"", l=m.league||"";
  return c&&l ? `${c} • ${l}` : (l||c||"Lig");
}

function colorClass(v){
  v=Number(v||0);
  if(v>=80) return "green";
  if(v>=65) return "yellow";
  if(v>=45) return "orange";
  return "red";
}
function pressureClass(v){
  v=Number(v||0);
  if(v>=80) return "green";
  if(v>=65) return "blue";
  if(v>=45) return "yellow";
  return "red";
}
function botClass(v){
  v=Number(v||0);
  if(v>=78) return "purple";
  if(v>=65) return "yellow";
  if(v>=45) return "orange";
  return "gray";
}
function cardLevel(m){
  if(m.bot_pick_best) return "bot";
  const score=Math.max(Number(m.signal||0),Number(m.momentum_score||0),Number(m.bot_pick_score||0));
  if(score>=80) return "very";
  if(score>=65) return "strong";
  if(score>=45) return "mid";
  return "weak";
}

function categoryFor(m){
  const goal=Number(m.signal||0);
  const pressure=Number(m.momentum_score||0);
  const bot=Number(m.bot_pick_score||0);
  const btts=Number(m.btts_signal||0);
  const firstHalf=Number(m.first_half_signal||0);
  const firstHalfLimit=Number(m.first_half_limit||65);

  if(m.bot_pick_best) return {icon:"🧠",text:"BOT PICK",cls:"bot-t"};
  const firstHalfLine=firstHalfMarket(m);
  if(firstHalfLine && firstHalf>=firstHalfLimit) return {icon:"⏱️",text:`İY ${firstHalfLine} ÜST`,cls:"blue-t"};
  const bttsDone=bttsDoneOf(m);
  if(btts>=70 && !bttsDone) return {icon:"⚽",text:"KARŞILIKLI GOL VAR",cls:"green-t"};
  if(pressure>=80) return {icon:"🔥",text:"ÇOK YÜKSEK BASKI",cls:"red-t"};
  if(goal>=80) return {icon:"🎯",text:"YÜKSEK GOL SİNYALİ",cls:"green-t"};
  if(pressure>=65) return {icon:"📌",text:"YÜKSEK BASKI",cls:"blue-t"};
  if(goal>=65 || bot>=65) return {icon:"⚡",text:"GOL POTANSİYELİ",cls:"yellow-t"};
  if(goal>=45 || pressure>=45) return {icon:"📈",text:"GOL TAKİBİ",cls:"orange-t"};
  return {icon:"🔎",text:"İZLENİYOR",cls:"blue-t"};
}

function firstHalfMarket(m){
  const minute=Number(m.minute||0);
  const goals=homeGoalsOf(m)+awayGoalsOf(m);
  if(m.status!=="1H" || minute<10 || minute>45) return "";
  if(goals===0) return "0,5";
  if(goals===1 && minute<=38) return "1,5";
  return "";
}

function renderLeagueTabs(ms){
  const box=document.getElementById("leagueTabs");
  const leagues=[...new Set(ms.map(leagueLabel).filter(Boolean))].sort();

  box.innerHTML=
    `<button class="tab ${currentLeague==="ALL"?"active":""}" data-league="ALL">Tümü (${ms.length})</button>`+
    leagues.map(l=>`<button class="tab ${currentLeague===l?"active":""}" data-league="${esc(l)}">${esc(l)}</button>`).join("");

  box.querySelectorAll(".tab").forEach(btn=>{
    btn.onclick=()=>{
      currentLeague=btn.dataset.league;
      renderLeagueTabs(currentMatches);
      renderMatches();
    };
  });
}

function sortMatches(list){
  const mode=document.getElementById("sortMode").value;
  return [...list].sort((a,b)=>{
    if(mode==="goal_desc") return Number(b.signal||0)-Number(a.signal||0);
    if(mode==="pressure_desc") return Number(b.momentum_score||0)-Number(a.momentum_score||0);
    if(mode==="minute_desc") return Number(b.minute||0)-Number(a.minute||0);
    return Number(b.bot_pick_score||0)-Number(a.bot_pick_score||0);
  });
}

function renderMatches(){
  const root=document.getElementById("matches");
  let list=currentLeague==="ALL"
    ? currentMatches
    : currentMatches.filter(m=>leagueLabel(m)===currentLeague);

  list=sortMatches(list);

  if(!list.length){
    root.innerHTML='<div class="empty">Şu anda gösterilecek canlı maç yok.</div>';
    return;
  }

  let html="";

  for(const m of list){
    const home=m.home_team||"";
    const away=m.away_team||"";
    const hg=homeGoalsOf(m);
    const ag=awayGoalsOf(m);
    const minute=Number(m.minute||0);
    const goal=Number(m.signal||0);
    const pressure=Number(m.momentum_score||0);
    const bot=Number(m.bot_pick_score||0);
    const homeGoal=Number(m.home_goal_signal||0);
    const awayGoal=Number(m.away_goal_signal||0);
    const btts=Number(m.btts_signal||0);
    const firstHalf=Number(m.first_half_signal||0);
    const s=m.stats||{};
    const cat=categoryFor(m);
    const league=leagueLabel(m);

    const expected=m.match_expected_team||m.expected_team||"";
    const firstHalfLimit=Number(m.first_half_limit||65);
    const firstHalfLine=firstHalfMarket(m);
    const firstHalfPick=Boolean(firstHalfLine && firstHalf>=firstHalfLimit);

    // Dinamik ÜST önerisi:
    // Mevcut toplam gole göre bir sonraki çizgi hesaplanır.
    // Örn. 2-3 => 5 gol => 5,5 ÜST = 1 gol daha.
    // Güçlü sinyal varsa daha ileri çizgi de söylenebilir:
    // Örn. 1-1 ve çok güçlü devam sinyali => 3,5 ÜST = 2 gol daha.
    const totalGoals=hg+ag;
    let expectedMoreGoals=1;

    // Çok güçlü devam baskısında 2 gol daha beklentisi.
    // BOT 78+ veya hem GOL hem BASKI güçlü ise.
    if(bot>=78 || (goal>=65 && pressure>=65)){
      expectedMoreGoals=2;
    }

    // Aşırı güçlü birleşik sinyalde 3 gol daha beklentisi.
    if(bot>=88 && goal>=80 && pressure>=75){
      expectedMoreGoals=3;
    }

    const overLine=(totalGoals + expectedMoreGoals - 0.5).toFixed(1).replace(".",",");
    const dynamicOver=`${overLine} ÜST • Bu maçta ${expectedMoreGoals} gol daha bekleniyor`;

    // KG Var yalnızca henüz gerçekleşmediyse önerilebilir.
    const bttsAlreadyHappened=(hg>0 && ag>0);
    const bttsPick=(!bttsAlreadyHappened && btts>=70);

    let botPickText=String(m.bot_pick_text||"").trim();
    const botPickIsBtts=/KG Var|Karşılıklı Gol/i.test(botPickText);

    // Eğer KG Var artık gerçekleşmişse eski KG Var BOT metnini ekranda tekrar önermeyiz.
    // Aynı mevcut sinyallerden dinamik ÜST önerisine döner.
    const validBotPickText=(m.bot_pick_best && botPickText && !(bttsAlreadyHappened && botPickIsBtts))
      ? botPickText
      : "";

    const pick=validBotPickText
      ? validBotPickText
      : (firstHalfPick ? `İY ${firstHalfLine} ÜST • İY Sinyali ${firstHalf}/100`
        : bttsPick ? "Karşılıklı Gol Var"
        : (goal>=65 || bot>=65 || pressure>=65 || m.bot_pick_best) ? dynamicOver
        : expected ? `${expected} gol bekleniyor`
        : "Gol için takipte");

    const odd=m.live_odd ? Number(m.live_odd) : null;
    const oddHtml=odd
      ? `<div class="odd ${odd>=1.50?"good":"low"}">${odd.toFixed(2)} ${odd>=1.50?"↑":"↓"}</div>`
      : `<div class="odd">—</div>`;

    const homeLogo=m.home_logo
      ? `<img class="badge-logo" src="${esc(m.home_logo)}" alt="">`
      : `<span class="fake-ball">⚽</span>`;

    const awayLogo=m.away_logo
      ? `<img class="badge-logo" src="${esc(m.away_logo)}" alt="">`
      : `<span class="fake-ball">⚽</span>`;

    const title=m.bot_pick_best ? pick : (firstHalfPick ? `İY ${firstHalfLine} ÜST` : cat.text);
    const remaining=m.status==="1H"
      ? (minute<45 ? `İlk yarı bitimine ~${45-minute} dk` : "İlk yarının son dakikaları")
      : (m.status==="2H" && minute<90 ? `90. dakikaya ~${90-minute} dk` : "Maç devam ediyor");
    const level=Math.max(0,Math.min(5,Math.ceil(pressure/20)));
    const dots=Array.from({length:5},(_,i)=>`<span class="pressure-dot ${i<level?"filled":""}"></span>`).join("");
    const recorded=Number(currentBotPickStats?.total||0);
    const performance=recorded>=20
      ? `🎯 Kayıtlı BOT PICK başarısı: %${Number(currentBotPickStats.success_rate).toFixed(1).replace(".",",")} (${Number(currentBotPickStats.won)}/${recorded})`
      : recorded>0
        ? `🎯 Kayıtlı sonuç: ${Number(currentBotPickStats.won)} doğru / ${recorded} seçim (az veri)`
        : "🎯 Kayıtlı BOT PICK sonucu henüz yok";
    const recent5=m.momentum_delta_5m||{};
    const oddDisplay=Number.isFinite(odd) && odd>0
      ? `<span class="featured-odd"><small>oran</small>${odd.toFixed(2)}</span>`
      : `<span class="featured-odd unavailable">oran yok</span>`;
    html+=`
      <div class="card featured ${m.bot_pick_best?"bot":""}">
        <div class="featured-head">
          <div class="featured-title">${m.bot_pick_best?"🧠":cat.icon} ${esc(title)}</div>
          <div class="featured-status">${m.bot_pick_best?"BOT PICK • ":""}Canlı • ${esc(m.status||"")}</div>
        </div>
        <div class="featured-divider"></div>
        <div class="featured-info">
          <div>🏆 ${esc(league)}</div>
          <div class="featured-teams">⚽ ${esc(home)} ${hg} – ${ag} ${esc(away)}</div>
          <div>🎯 Hedef: <b>${esc(pick)}</b></div>
          <div>⏱️ ${minute}. dakika | ${esc(remaining)}</div>
          <div>📌 Baskı: <span class="pressure-dots" aria-label="${level}/5 baskı">${dots}</span> <b>${pressure}/100</b></div>
          ${m.bot_pick_best ? `<div>${esc(performance)}</div>` : ""}
          ${m.bot_pick_best ? `<div>⏱️ Son 5 dk: Şut ${Number(recent5.shots||0)} · İsabet ${Number(recent5.target||0)} · Ceza içi ${Number(recent5.inside||0)}</div>` : ""}
          <div>⚽ Şut ${Number(s.shots||0)} · İsabet ${Number(s.target||0)} · Korner ${Number(s.corners||0)} · Ceza içi ${Number(s.inside||0)}</div>
        </div>
        <div class="featured-divider"></div>
        <div class="featured-foot">
          <div><div class="play-label">NE OYNANIR?</div><div class="featured-pick">${esc(pick)}</div></div>
          ${oddDisplay}
        </div>
      </div>`;
  }

  root.innerHTML=html;
}

function goalMinuteText(goal,fallback){
  if(!goal) return `${Number(fallback||0)}'`;
  const minute=Number(goal.minute||fallback||0),extra=Number(goal.extra||0);
  return extra>0?`${minute}+${extra}'`:`${minute}'`;
}

function showGoalToast(m,detectedTeam){
  const goal=m.last_goal||{};
  const goalTeam=goal.team||detectedTeam||"Gol atan takım bilinmiyor";
  const scorer=goal.player?`${goal.player}`:goalTeam;
  const assist=goal.assist?` • Asist: ${goal.assist}`:"";
  document.getElementById("goalToastBody").innerHTML=
    `${esc(m.home)} <b>${Number(m.home_goals||0)} – ${Number(m.away_goals||0)}</b> ${esc(m.away)}`+
    `<br>${esc(goalTeam)} • ${esc(scorer)}${esc(assist)} • ${esc(goalMinuteText(goal,m.minute))}`;
  const toast=document.getElementById("goalToast");
  toast.classList.add("show");
  clearTimeout(goalToastTimer);
  goalToastTimer=setTimeout(()=>toast.classList.remove("show"),9000);
}

function renderLiveTicker(ms){
  const list=Array.isArray(ms)?ms:[];
  const nextMemory=new Map();
  for(const m of list){
    const id=String(m.fixture_id||"");
    if(!id) continue;
    const home=Number(m.home_goals||0),away=Number(m.away_goals||0);
    nextMemory.set(id,{home,away});
    const previous=liveScoreMemory.get(id);
    if(tickerInitialized && previous && (home>previous.home||away>previous.away)){
      const detectedTeam=home>previous.home?m.home:m.away;
      showGoalToast(m,detectedTeam);
    }
  }
  liveScoreMemory=nextMemory;tickerInitialized=true;

  const track=document.getElementById("tickerTrack");
  if(!list.length){track.innerHTML="<span>Şu anda uygun liglerde canlı maç yok.</span>";return;}
  const items=list.map(m=>
    `<span class="ticker-item"><span class="ticker-minute">${Number(m.minute||0)}'</span>`+
    `<span>${esc(m.home)}</span><span class="ticker-score">${Number(m.home_goals||0)}-${Number(m.away_goals||0)}</span>`+
    `<span>${esc(m.away)}</span><span class="ticker-league">${esc(m.country||"")} • ${esc(m.league||"")}</span></span>`
  ).join("");
  const signature=list.map(m=>`${m.fixture_id}:${m.home_goals}-${m.away_goals}:${m.minute}`).join("|");
  if(track.dataset.signature!==signature){
    track.dataset.signature=signature;
    track.innerHTML=items+items;
    track.style.animationDuration=`${Math.max(90,list.length*12)}s`;
  }
}

async function loadAll(){
  try{
    const [sr,mr,tr]=await Promise.all([
      fetch("/api/status",{cache:"no-store"}),
      fetch("/api/matches",{cache:"no-store"}),
      fetch("/api/live-ticker",{cache:"no-store"}).catch(()=>null)
    ]);

    const s=await sr.json();
    const m=await mr.json();
    const t=tr&&tr.ok?await tr.json():null;

    document.getElementById("lastScan").textContent="Son güncelleme: "+(s.last_scan_finished||"-");
    document.getElementById("scanEvery").textContent=`↻ Genel ${s.check_seconds||30} sn • İY ${s.first_half_check_seconds||15} sn • Oran ${s.live_odds_cache_ttl||30} sn`;

    currentMatches=Array.isArray(m.matches)?m.matches:[];
    currentBotPickStats=m.bot_pick_stats||s.bot_pick_stats||null;
    renderLeagueTabs(currentMatches);
    renderMatches();
    if(t) renderLiveTicker(t.matches);
    tryNotifications(currentMatches);

    document.getElementById("errorBox").style.display="none";
  }catch(e){
    const x=document.getElementById("errorBox");
    x.style.display="block";
    x.textContent="Veri alınamadı: "+e;
    document.getElementById("systemState").textContent="● Bağlantı Sorunu";
  }
}

function requestNotifications(){
  if(!("Notification" in window)) return;
  Notification.requestPermission().then(()=>{
    document.getElementById("notificationBtn").textContent=
      Notification.permission==="granted" ? "🔔 ✓" : "🔕";
  });
}

/* Aynı maç + aynı skor döneminde yalnızca 1 bildirim */
function tryNotifications(ms){
  if(!("Notification" in window)||Notification.permission!=="granted") return;

  const saved=JSON.parse(localStorage.getItem("golRadarNotifiedV2")||"{}");
  const now=Date.now();

  for(const [k,ts] of Object.entries(saved)){
    if(now-Number(ts||0)>86400000) delete saved[k];
  }

  for(const m of ms){
    const fixture=m.fixture_id;
    if(!fixture) continue;

    const score=`${m.home_goals||0}-${m.away_goals||0}`;
    const periodKey=`MATCH:${fixture}:${score}`;
    const fhLine=firstHalfMarket(m);
    const fhLimit=Number(m.first_half_limit||(fhLine==="1,5"?72:60));
    const fhScore=Number(m.first_half_signal||0);
    const fhKey=`FH:${fixture}:${score}:${fhLine}`;

    // İlk yarı sinyali ayrı bir bildirim kanalıdır. Maç geneli bildirimi daha
    // önce gönderilmiş olsa bile güçlü İY 0,5 / 1,5 sinyali kaybolmaz.
    if(fhLine && fhScore>=fhLimit && !saved[fhKey]){
      new Notification(`⏱️ İY ${fhLine} ÜST • ${fhScore}/100`,{
        body:
          `${m.home_team} ${score} ${m.away_team}`+
          ` • ${m.minute||0}'`+
          ` • İlk yarı gol baskısı güçlü`+
          (m.expected_team?` • Gol beklenen: ${m.expected_team}`:"")
      });
      saved[fhKey]=now;
      continue;
    }

    if(saved[periodKey]) continue;

    if(m.bot_pick_best){
      let notifyPick=String(m.bot_pick_text||"").trim();
      if(bttsDoneOf(m) && /KG Var|Karşılıklı Gol/i.test(notifyPick)){
        const total=homeGoalsOf(m)+awayGoalsOf(m);
        const g=Number(m.signal||0), p=Number(m.momentum_score||0), b=Number(m.bot_pick_score||0);
        let more=1;
        if(b>=78 || (g>=65 && p>=65)) more=2;
        if(b>=88 && g>=80 && p>=75) more=3;
        notifyPick=`${(total+more-0.5).toFixed(1).replace(".",",")} ÜST • ${more} gol daha`;
      }
      new Notification(`🧠 BOT PICK • ${m.bot_pick_score||0}/100`,{
        body:
          `${m.home_team} ${score} ${m.away_team}`+
          ` • ${m.minute||0}'`+
          ` • ${notifyPick}`+
          (m.live_odd?` • Oran ${Number(m.live_odd).toFixed(2)}`:"")
      });
      saved[periodKey]=now;
      continue;
    }

    const hs=Number(m.home_goal_signal||0);
    const as=Number(m.away_goal_signal||0);
    const strongest=Math.max(hs,as);
    const diff=Math.abs(hs-as);

    if(strongest>=65 && diff>=8){
      const team=hs>=as?m.home_team:m.away_team;
      const sig=hs>=as?hs:as;

      new Notification(`⚽ GOL RADARI • %${sig}`,{
        body:
          `${m.home_team} ${score} ${m.away_team}`+
          ` • ${m.minute||0}'`+
          ` • Gol beklenen: ${team}`
      });

      saved[periodKey]=now;
    }
  }

  localStorage.setItem("golRadarNotifiedV2",JSON.stringify(saved));
}

document.getElementById("sortMode").addEventListener("change",renderMatches);
loadAll();
setInterval(loadAll,5000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)

PREMATCH_PAGE = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maç Önü Tahminleri • Gol Merkezi</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#071625;color:#ebf5ff;font:15px system-ui,Arial,sans-serif}
.wrap{max-width:1080px;margin:auto;padding:22px 16px 60px}
header{display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;margin-bottom:22px}
h1{font-size:25px;margin:0 0 5px}p{color:#a9bdce;margin:0;line-height:1.5}
.version{font-size:11px;color:#c9b5ff;border:1px solid #68548c;border-radius:12px;padding:3px 7px;vertical-align:middle}
a{color:#70e5b0;text-decoration:none}a:hover{text-decoration:underline}
.toolbar{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:15px;background:#13283a;border:1px solid #305064;border-radius:13px}
input,select,button{font:inherit;border-radius:9px;padding:10px 12px}input,select{background:#071d2d;color:#fff;border:1px solid #3c647a;color-scheme:dark}
select{max-width:min(100%,320px)}select:disabled{opacity:.55}
button{border:1px solid #2cab79;background:#0e6f50;color:white;cursor:pointer;font-weight:700}button:disabled{opacity:.5;cursor:wait}
.hint{font-size:12px;color:#9eb2c6;margin:14px 0}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.match{padding:16px;border:1px solid #37536d;border-radius:14px;background:#1b293e}
.meta{font-size:12px;color:#adc3d5;margin-bottom:8px}.teams{font-size:18px;font-weight:800;margin-bottom:12px}
.analysis{border-top:1px solid #40546e;margin-top:14px;padding-top:14px;line-height:1.5}
.pick{border:1px solid #278966;background:#103d32;padding:9px;border-radius:8px;margin-top:9px}
.pick b{color:#72e7b3}.reason{font-size:12px;color:#b9c9d5}
.quote{font-weight:800;color:#e7f5ff;margin-top:4px}.odds-note{font-size:12px;color:#abc0d0;margin-top:10px}
.pick-why{margin-top:9px;padding-top:8px;border-top:1px solid #357862;color:#e0ebe8;font-size:12px;line-height:1.5}
.pick-why b{color:#a2f0c4}
.joint-note{margin-top:9px;border:1px solid #4984a1;border-radius:8px;background:#173248;color:#dcefff;padding:9px;font-size:12px;line-height:1.5}
.lineup-box{margin:10px 0;border:1px solid #5c5278;border-radius:9px;background:#211f35;padding:10px;color:#dcd8ef;font-size:12px;line-height:1.5}
.lineup-box b{color:#b9a7ff}.lineup-teams{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:7px}
.lineup-team{background:#17182a;border-radius:7px;padding:8px}
.form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:10px 0}
.form div{background:#101f30;padding:9px;border-radius:7px;font-size:12px}
.pas{color:#ffc877;font-weight:800}.error{color:#ff9da1}
.coupon-panel{margin:14px 0;padding:14px;border:1px solid #7657a8;border-radius:13px;background:#1d2038}
.coupon-head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.coupon-head h2{font-size:18px;margin:0}.coupon-help{font-size:12px;color:#b8c5d7;margin-top:5px}
.bankroll{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px;margin-top:12px}
.bankroll div{background:#121a2c;border:1px solid #3d4d6b;border-radius:8px;padding:8px;font-size:11px;color:#acbdd0}
.bankroll b{display:block;color:#f2f6ff;font-size:15px;margin-top:3px}.profit{color:#72e7a9!important}.loss{color:#ff9198!important}
.coupons{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin-top:11px}
.coupon{background:#121a2c;border:1px solid #4c6381;border-radius:9px;padding:10px}
.coupon-title{font-weight:800;color:#cdb8ff;margin-bottom:7px}.coupon-leg{font-size:12px;border-top:1px solid #2f3c55;padding:7px 0;line-height:1.45}
.coupon-total{font-size:12px;color:#80e8b6;margin-top:7px;font-weight:800}.won{color:#72e7a9}.lost{color:#ff9198}.open{color:#ffd379}.void{color:#9aa7b8}
@media(max-width:680px){.grid,.form,.lineup-teams,.coupons{grid-template-columns:1fr}.bankroll{grid-template-columns:repeat(2,minmax(0,1fr))}.teams{font-size:16px}}
</style></head><body><div class="wrap">
<header><div><h1>📅 Maç Önü Tahminleri <span class="version">v4.10</span></h1><p>Maç seç; son maçların formunu ve gol eğilimlerini incele.</p></div>
<a href="/">← Canlı Gol Merkezi</a></header>
<div class="toolbar">
  <label for="day">Maç günü</label>
  <input id="day" type="date" min="{{today}}" max="{{last_day}}" value="{{initial_day}}">
  <button id="load">Fikstürü getir</button>
  <label for="league">Ülke</label>
  <select id="league" disabled><option value="ALL">Tüm ülkeler</option></select>
</div>
<div class="hint">Bir ülke seçildiğinde o ülkenin uygun tüm alt ligleri birlikte gösterilir • Saatler Türkiye saatidir • Analiz, açtığın maç için yapılır.</div>
<section class="coupon-panel">
  <div class="coupon-head"><div><h2>🎟️ Botun Sanal Bahis Defteri</h2><div class="coupon-help">Bot yalnızca 3 farklı maçtan oluşan, yüksek birlikte tutma ihtimalli kombineleri oynar. Her kupon 1.000 TL; kupon sayısı sabit değildir. Aynı maç günün aktif kuponlarında yalnız bir kez kullanılır.</div></div>
  <button id="makeCoupons" type="button">Botun oynayacağı kuponları oluştur</button></div>
  <div id="bankroll" class="bankroll"></div><div id="couponState" class="coupon-help">Henüz kupon oluşturulmadı.</div><div id="coupons" class="coupons"></div>
</section>
<p id="state">Fikstür yükleniyor…</p>
<div class="grid" id="fixtures"></div>
</div><script>
const esc=v=>String(v??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
const day=document.getElementById("day"),state=document.getElementById("state"),root=document.getElementById("fixtures"),leagueSelect=document.getElementById("league");
let loadedMatches=[],loadedDate="",analyzedMatches=new Map();
function shownTime(v){try{return new Intl.DateTimeFormat("tr-TR",{timeZone:"Europe/Istanbul",hour:"2-digit",minute:"2-digit"}).format(new Date(v))}catch{return "Saat bilinmiyor"}}
function quoteTime(v){if(!v)return "";const d=new Date(v);return Number.isNaN(d.getTime())?"":new Intl.DateTimeFormat("tr-TR",{timeZone:"Europe/Istanbul",day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}).format(d)}
function formText(v,side){
  if(!v || v.insufficient)return `Tamamlanmış son maç sayısı: ${Number(v?.count||0)} (en az 5 gerekli)`;
  const a=v.all,venue=v[side],label=side==="home"?"İç saha":"Deplasman";
  return `Son ${a.count} maç: ${a.wins} galibiyet · maç başı ${a.scored} atılan / ${a.conceded} yenilen gol · ${a.scored2} kez 2+ gol attı · ${a.btts} KG · ${a.over25} kez 2,5 üst · ${a.under25} kez 2,5 alt. ${label} (${venue.count} maç): ${venue.wins} galibiyet, ${venue.scored2} kez 2+ gol attı, maç başı ${venue.scored ?? "—"} atılan / ${venue.conceded ?? "—"} yenilen gol.`;
}
function h2hHtml(a){
  const h=a.h2h||{},m=h.all||{},n=Number(h.count||0);
  if(n<2)return `<div class="lineup-box"><b>🤝 İkili rekabet</b><br>Analize etki edecek yeterli yakın dönem karşılaşması bulunamadı.</div>`;
  const awayWins=Number(m.losses||0),avg=(Number(m.scored||0)+Number(m.conceded||0)).toFixed(1);
  return `<div class="lineup-box"><b>🤝 İki takımın kendi arasındaki son ${n} maç</b><br>Ev sahibi ${Number(m.wins||0)} galibiyet · ${Number(m.draws||0)} beraberlik · Deplasman ${awayWins} galibiyet · KG ${Number(m.btts||0)}/${n} · 2,5 ÜST ${Number(m.over25||0)}/${n} · Gol ortalaması ${avg}</div>`;
}
function lineupHtml(a){
  const l=a.lineups||{},status=l.confirmed?"✅ Onaylı ilk 11 analize dahil":"⏳ Kadrolar bekleniyor";
  if(!l.confirmed)return `<div class="lineup-box"><b>${status}</b><br>${esc(a.lineup_note||"")}</div>`;
  const team=x=>`<div class="lineup-team"><b>${esc(x.team)} • ${esc(x.formation)}</b><br>${(x.starters||[]).map(p=>`${esc(p.name)} (${esc(p.pos)})`).join(" · ")}</div>`;
  return `<div class="lineup-box"><b>${status}</b><div class="lineup-teams">${team(l.home)}${team(l.away)}</div></div>`;
}
async function getJson(url){
  const r=await fetch(url,{cache:"no-store"});let body;
  try{body=await r.json()}catch{throw Error("Sunucu yanıtı okunamadı.")}
  if(!r.ok)throw Error(body.error||"Veri alınamadı.");
  return body;
}
const couponStoreKey="golPrematchCouponsV2";
const couponStake=1000;
function couponStore(){try{return JSON.parse(localStorage.getItem(couponStoreKey)||"{}")||{}}catch{return {}}}
function saveCouponStore(store){localStorage.setItem(couponStoreKey,JSON.stringify(store))}
const modelProbability=confidence=>Math.max(.50,Math.min(.90,Number(confidence||0)/100-.04));
function candidatePool(){
  const pool=[],stats={suggestions:0,lowConfidence:0,lowOdd:0};
  for(const [fixture,item] of analyzedMatches){
    for(const pick of item.analysis.suggestions||[]){
      stats.suggestions++;
      const confidence=Number(pick.confidence||0),odd=Number(pick.quote?.odd||0);
      if(confidence<80){stats.lowConfidence++;continue;}
      if(odd<1.30){stats.lowOdd++;continue;}
      pool.push({fixture:Number(fixture),home:item.match.home,away:item.match.away,kickoff:item.match.kickoff,
        market:pick.market,label:pick.label,confidence,odd,bookmaker:pick.quote.bookmaker,status:"OPEN"});
    }
  }
  pool.sort((a,b)=>b.confidence-a.confidence||a.odd-b.odd);pool.stats=stats;return pool;
}
function buildCoupons(){
  const store=couponStore(),existing=store[loadedDate]||[],pool=candidatePool();
  // v4.3'ün aşırı maç tekrarına sahip henüz oynanmamış kuponlarını silmeden
  // iptal et; başarı ve para hesabını yeni stratejiyle temiz başlat.
  for(const coupon of existing){
    if((coupon.status||"OPEN")==="OPEN"&&(coupon.legs||[]).length<3){
      coupon.status="VOID";coupon.voidReason="Yeni kupon kuralı: en az 3 maç";
      for(const leg of coupon.legs||[])if((leg.status||"OPEN")==="OPEN")leg.status="VOID";
      continue;
    }
    if((coupon.status||"OPEN")==="OPEN"&&!['v4.4','v4.5','v4.6','v4.7'].includes(coupon.strategyVersion)){
      coupon.status="VOID";coupon.voidReason="Yeni risk dağılımı: aynı maç tek kupon";
      for(const leg of coupon.legs||[])if((leg.status||"OPEN")==="OPEN")leg.status="VOID";
    }
  }
  const activeExisting=existing.filter(c=>c.status!=="VOID");
  const existingSignatures=new Set(activeExisting.map(c=>c.signature||c.legs.map(x=>`${x.fixture}:${x.market}`).sort().join("|")));
  const usedFixtures=new Set(activeExisting.flatMap(c=>(c.legs||[]).map(x=>Number(x.fixture))));
  const combos=[],rejected={sameMatch:0,usedMatch:0,duplicate:0,totalOdd:0,probability:0,roi:0};
  function consider(legs){
    if(new Set(legs.map(x=>x.fixture)).size!==legs.length){rejected.sameMatch++;return;}
    if(legs.some(x=>usedFixtures.has(Number(x.fixture)))){rejected.usedMatch++;return;}
    const signature=legs.map(x=>`${x.fixture}:${x.market}`).sort().join("|");
    if(existingSignatures.has(signature)){rejected.duplicate++;return;}
    const min=Math.min(...legs.map(x=>x.confidence)),avg=legs.reduce((s,x)=>s+x.confidence,0)/legs.length;
    const total=legs.reduce((s,x)=>s*x.odd,1);
    const probability=legs.reduce((p,x)=>p*modelProbability(x.confidence),1);
    const expectedReturn=couponStake*total*probability;
    const expectedProfit=expectedReturn-couponStake;
    const expectedRoi=expectedProfit/couponStake;
    const minProbability=.42;
    if(total<1.60||total>5.00){rejected.totalOdd++;return;}
    if(probability<minProbability){rejected.probability++;return;}
    if(expectedRoi<.10){rejected.roi++;return;}
    combos.push({legs,min,avg,total,probability,expectedReturn,expectedProfit,expectedRoi,
      signature,score:expectedRoi*10000+probability*1000+min});
  }
  for(let i=0;i<pool.length;i++)for(let j=i+1;j<pool.length;j++)for(let k=j+1;k<pool.length;k++)consider([pool[i],pool[j],pool[k]]);
  combos.sort((a,b)=>b.score-a.score);
  const chosen=[],newUsedFixtures=new Set();
  // Sabit kupon adedi yok: analiz edilen güçlü seçim havuzu büyüdükçe botun
  // bu turda alacağı kupon sayısı 1-8 arasında kademeli artar.
  const batchLimit=Math.min(8,Math.max(1,Math.ceil(Math.sqrt(pool.length))));
  for(const combo of combos){
    const fixtures=combo.legs.map(x=>Number(x.fixture));
    if(fixtures.some(id=>newUsedFixtures.has(id)))continue;
    chosen.push({...combo,stake:couponStake,status:"OPEN",strategyVersion:"v4.7",created:new Date().toISOString()});
    fixtures.forEach(id=>newUsedFixtures.add(id));
    if(chosen.length>=batchLimit)break;
  }
  if(!chosen.length){
    saveCouponStore(store);
    renderCoupons(existing);
    const s=pool.stats||{};
    document.getElementById("couponState").textContent=`Yeni 3 maçlı kupon bulunamadı: ${s.suggestions||0} tercih incelendi; ${s.lowConfidence||0} tercih 80 güvenin altında, ${s.lowOdd||0} tercih 1,30 oranın altında kaldı. Kupon havuzuna ${pool.length} tercih girdi. Kombinelerde ${rejected.probability} olasılık, ${rejected.roi} beklenen değer, ${rejected.totalOdd} toplam oran ve ${rejected.usedMatch} daha önce kullanılmış maç nedeniyle elendi.`;
    return;
  }
  store[loadedDate]=[...existing,...chosen];saveCouponStore(store);
  renderCoupons(store[loadedDate]);
  document.getElementById("couponState").textContent=`Bot bu turda ${chosen.length} yeni kupona toplam ${(chosen.length*couponStake).toLocaleString("tr-TR")} TL sanal bahis aldı.`;
}
function legResult(market,h,a){
  const total=h+a;
  if(market==="HOME")return h>a;if(market==="AWAY")return a>h;
  if(market==="DC_1X")return h>=a;if(market==="DC_X2")return a>=h;
  if(market==="HOME_OVER_1_5")return h>=2;if(market==="AWAY_OVER_1_5")return a>=2;
  if(market==="BTTS_YES")return h>0&&a>0;if(market==="BTTS_NO")return h===0||a===0;
  if(market==="OVER_1_5")return total>=2;if(market==="OVER_2_5")return total>=3;
  if(market==="UNDER_2_5")return total<=2;if(market==="OVER_3_5")return total>=4;
  if(market==="UNDER_3_5")return total<=3;return false;
}
async function settleCoupons(){
  const store=couponStore();let changed=false;
  for(const coupons of Object.values(store))for(const coupon of coupons||[]){
    if(coupon.status==="VOID")continue;
    for(const leg of coupon.legs||[]){
    if(leg.status!=="OPEN")continue;
    try{
      const r=await getJson(`/api/prematch/result?fixture=${encodeURIComponent(leg.fixture)}`);
      if(r.finished&&r.home_goals!==null&&r.away_goals!==null){
        leg.status=legResult(leg.market,Number(r.home_goals),Number(r.away_goals))?"WON":"LOST";
        leg.score=`${r.home_goals}-${r.away_goals}`;changed=true;
      }
    }catch{}
    }
  }
  for(const coupons of Object.values(store))for(const coupon of coupons||[]){
    if(coupon.status==="VOID")continue;
    coupon.status=coupon.legs.some(x=>x.status==="LOST")?"LOST":coupon.legs.every(x=>x.status==="WON")?"WON":"OPEN";
    const stake=Number(coupon.stake||couponStake),total=Number(coupon.total||1);
    coupon.stake=stake;
    coupon.payout=coupon.status==="WON"?stake*total:coupon.status==="LOST"?0:0;
    coupon.profit=coupon.status==="WON"?coupon.payout-stake:coupon.status==="LOST"?-stake:0;
  }
  saveCouponStore(store);
  renderCoupons(store[loadedDate]||[]);
}
function bankrollStats(store){
  const stored=Object.values(store).flatMap(x=>Array.isArray(x)?x:[]),voided=stored.filter(c=>c.status==="VOID");
  const all=stored.filter(c=>c.status!=="VOID");
  const won=all.filter(c=>c.status==="WON"),lost=all.filter(c=>c.status==="LOST"),open=all.filter(c=>(c.status||"OPEN")==="OPEN");
  const settled=[...won,...lost],stakeOf=c=>Number(c.stake||couponStake);
  const totalStaked=all.reduce((s,c)=>s+stakeOf(c),0);
  const settledStaked=settled.reduce((s,c)=>s+stakeOf(c),0);
  const returned=won.reduce((s,c)=>s+stakeOf(c)*Number(c.total||1),0);
  return {total:all.length,voided:voided.length,won:won.length,lost:lost.length,open:open.length,totalStaked,returned,
    net:returned-settledStaked,success:settled.length?won.length/settled.length*100:0};
}
function renderBankroll(store){
  const s=bankrollStats(store),netClass=s.net>=0?"profit":"loss",money=n=>`${Number(n||0).toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`;
  document.getElementById("bankroll").innerHTML=
    `<div>Toplam kupon<b>${s.total}</b></div>`+
    `<div>Tutan / Yatan<b><span class="won">${s.won}</span> / <span class="lost">${s.lost}</span></b></div>`+
    `<div>Botun toplam başarısı<b>%${s.success.toFixed(1).replace(".",",")}</b></div>`+
    `<div>Toplam sanal bahis<b>${money(s.totalStaked)}</b></div>`+
    `<div>Gerçekleşen net kâr/zarar<b class="${netClass}">${s.net>=0?"+":""}${money(s.net)}</b></div>`;
}
function renderCoupons(coupons){
  const root=document.getElementById("coupons"),store=couponStore();root.innerHTML="";
  coupons=(coupons||[]).filter(c=>c.status!=="VOID");
  renderBankroll(store);
  let won=0,lost=0,legWon=0,legLost=0;
  for(const list of Object.values(store))for(const c of list||[]){
    if(c.status==="WON")won++;if(c.status==="LOST")lost++;
    for(const l of c.legs||[]){if(l.status==="WON")legWon++;if(l.status==="LOST")legLost++;}
  }
  const settled=won+lost,settledLegs=legWon+legLost;
  document.getElementById("couponState").textContent=coupons.length
    ? `Bu gün için ${coupons.length} kupon • Genel kupon başarısı: ${settled?Math.round(won/settled*100):0}% (${won}/${settled}) • Seçim başarısı: ${settledLegs?Math.round(legWon/settledLegs*100):0}% (${legWon}/${settledLegs})`
    : "Bu tarih için kayıtlı kupon yok.";
  coupons.forEach((c,i)=>{
    const el=document.createElement("div");el.className="coupon";
    const status=c.status||"OPEN",statusText=status==="WON"?"TUTTU":status==="LOST"?"YATMADI":status==="VOID"?"İPTAL":"BEKLİYOR";
    const stake=Number(c.stake||couponStake),total=Number(c.total||1),potential=stake*total;
    const probability=Number(c.probability||0),expectedProfit=Number(c.expectedProfit||0);
    el.innerHTML=`<div class="coupon-title">Kupon ${i+1} • <span class="${status.toLowerCase()}">${statusText}</span></div>`+
      c.legs.map(l=>`<div class="coupon-leg"><b>${esc(l.home)} — ${esc(l.away)}</b><br>${esc(l.label)} • ${l.odd.toFixed(2)} ${esc(l.bookmaker)}<br>Model güveni ${l.confidence}/100${l.score?` • Skor ${esc(l.score)}`:""} • <span class="${String(l.status||"OPEN").toLowerCase()}">${l.status==="WON"?"TUTTU":l.status==="LOST"?"YATTI":l.status==="VOID"?"İPTAL":"AÇIK"}</span></div>`).join("")+
      `<div class="coupon-total">Toplam oran ${total.toFixed(2)} • ${status==="VOID"?`Bahis iptal • Para ve başarı hesabına dahil değil`: `Bahis ${stake.toLocaleString("tr-TR")} TL • ${status==="OPEN"?`Potansiyel dönüş ${potential.toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`:status==="WON"?`Dönüş ${potential.toLocaleString("tr-TR",{maximumFractionDigits:0})} TL • Net +${(potential-stake).toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`:`Net -${stake.toLocaleString("tr-TR")} TL`}`}<br>${probability?`Kuponun birlikte tutma ihtimali %${(probability*100).toFixed(1).replace(".",",")} • Teorik beklenen değer ${expectedProfit>=0?"+":""}${expectedProfit.toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`:"Eski kupon • olasılık kaydı yok"}</div>`;
    root.appendChild(el);
  });
}
function renderFixtures(){
  const selected=loadedDate;
  const filtered=leagueSelect.value==="ALL" ? loadedMatches
    : loadedMatches.filter(m=>String(m.country||"Diğer")===leagueSelect.value);
  state.textContent=filtered.length
    ? `${filtered.length} maç gösteriliyor. Analiz için bir maç seç.`
    : "Seçilen ligde başlamamış maç bulunamadı.";
  root.innerHTML="";
  for(const m of filtered){
      const card=document.createElement("article");card.className="match";
      card.innerHTML=`<div class="meta">🏆 ${esc(m.country)} • ${esc(m.league)} &nbsp; ⏰ ${esc(shownTime(m.kickoff))}</div>
        <div class="teams">${esc(m.home)} — ${esc(m.away)}</div>
        <button type="button">Bu maçı analiz et</button><div class="analysis" hidden></div>`;
      const button=card.querySelector("button"),box=card.querySelector(".analysis");
      button.onclick=async()=>{
        button.disabled=true;box.hidden=false;box.textContent="Son maçlar inceleniyor…";
        try{
          const a=await getJson("/api/prematch/analyze?date="+encodeURIComponent(selected)+"&fixture="+encodeURIComponent(m.id));
          analyzedMatches.set(Number(m.id),{match:m,analysis:a});
          const picks=a.suggestions.length
            ? a.suggestions.map(p=>`<div class="pick"><b>${esc(p.label)}</b><div class="quote">Oran ${Number(p.quote.odd).toFixed(2)} · ${esc(p.quote.bookmaker)}</div><div class="reason">${esc(p.quote.market_name)}: ${esc(p.quote.selection)}${quoteTime(p.quote.updated)?` · Güncelleme: ${esc(quoteTime(p.quote.updated))}`:""}</div><div class="pick-why"><b>Neden bu tercih?</b><br>${esc(p.explanation || p.reason)}</div></div>`).join("")
            : `<div class="pas">PAS • ${a.candidate_count ? "Modelde eğilim var, ancak fiyat koşulu sağlanmadı." : "Yeterli ortak veri işareti yok."}</div>`;
          box.innerHTML=`<div class="form"><div><b>${esc(a.home)}</b><br>${esc(formText(a.home_form,"home"))}</div>
            <div><b>${esc(a.away)}</b><br>${esc(formText(a.away_form,"away"))}</div></div>
            ${h2hHtml(a)}${lineupHtml(a)}${picks}${(a.joint_notes||[]).map(n=>`<div class="joint-note"><b>Birlikte gerçekleşme ihtimali</b><br>${esc(n)}</div>`).join("")}<div class="odds-note">${esc(a.odds_note)}</div><div class="hint">${esc(a.note)}</div>`;
        }catch(e){box.innerHTML='<span class="error">'+esc(e.message)+'</span>'}
        finally{button.disabled=false}
      };
      root.appendChild(card);
  }
}
async function load(){
  const selected=day.value;state.textContent="Fikstür yükleniyor…";root.innerHTML="";
  leagueSelect.disabled=true;leagueSelect.innerHTML='<option value="ALL">Tüm ülkeler</option>';
  loadedMatches=[];loadedDate="";analyzedMatches=new Map();
  try{
    const data=await getJson("/api/prematch/fixtures?date="+encodeURIComponent(selected));
    loadedDate=selected;loadedMatches=data.matches;
    const countries=new Map();
    for(const m of loadedMatches){
      const country=String(m.country||"Diğer");
      if(!countries.has(country))countries.set(country,{count:0,leagues:new Set()});
      countries.get(country).count++;
      countries.get(country).leagues.add(String(m.league||"Lig"));
    }
    const options=[...countries.entries()].sort((a,b)=>a[0].localeCompare(b[0],"tr"));
    leagueSelect.innerHTML=`<option value="ALL">Tüm ülkeler (${loadedMatches.length} maç)</option>`+
      options.map(([country,item])=>`<option value="${esc(country)}">${esc(country)} · ${item.leagues.size} lig (${item.count} maç)</option>`).join("");
    leagueSelect.disabled=options.length===0;
    renderFixtures();renderCoupons(couponStore()[loadedDate]||[]);settleCoupons();
  }catch(e){state.innerHTML='<span class="error">'+esc(e.message)+'</span>'}
}
leagueSelect.onchange=renderFixtures;
document.getElementById("makeCoupons").onclick=buildCoupons;
document.getElementById("load").onclick=load;load();
</script></body></html>"""


@app.route("/tahmin")
def prematch_page():
    today = datetime.now(ISTANBUL_TZ).date()
    next_saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    return render_template_string(
        PREMATCH_PAGE, today=today.isoformat(),
        initial_day=next_saturday.isoformat(),
        last_day=(today + timedelta(days=7)).isoformat(),
    )


# Gunicorn import ettiğinde scanner başlasın.
ensure_scanner_started()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
