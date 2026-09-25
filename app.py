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
