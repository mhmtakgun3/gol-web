import os
import time
import threading
import sqlite3
import requests
import fcntl
import json

from flask import Flask, jsonify, render_template_string


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# AYARLAR
# ============================================================

API_KEY = os.getenv("API_KEY", "").strip()

API_URL = "https://v3.football.api-sports.io"

CHECK_SECONDS = 30

SIGNAL_LIMIT = 65

FIRST_HALF_LIMIT = 65

# Çok güçlü sinyal seviyesi
VERY_STRONG_SIGNAL = 80

# Render ortak dosya
DB_FILE = "/tmp/gol_scanner.db"

LOCK_FILE = "/tmp/gol_scanner.lock"


# ============================================================
# İZİN VERİLEN LİGLER
# ============================================================

ALLOWED_LEAGUES = {

    # İngiltere
    39, 40, 41, 42,

    # İspanya
    140, 141, 435,

    # İtalya
    135, 136, 137,

    # Almanya
    78, 79, 80,

    # Fransa
    61, 62, 63,

    # Türkiye
    203, 204, 205, 206,

    # Hollanda
    88, 89,

    # Belçika
    144, 145,

    # Portekiz
    94, 95,

    # İskoçya
    179, 180, 181, 182,

    # Avusturya
    218, 219,

    # İsviçre
    207, 208,

    # Yunanistan
    197,

    # Polonya
    106, 107,

    # Çekya
    345, 346,

    # Danimarka
    119, 120,

    # Norveç
    103, 104,

    # İsveç
    113, 114,

    # ABD
    253,

    # Brezilya
    71, 72,

    # Arjantin
    128,

    # Meksika
    262, 263,

    # Japonya
    98, 99,

    # Güney Kore
    292, 293,

    # Avustralya
    188,

    # Romanya
    283, 284,

    # Sırbistan
    286,

    # Hırvatistan
    210,

    # Bulgaristan
    172,

    # İrlanda
    357,

    # Finlandiya
    244,

    # İzlanda
    164,

    # Güney Afrika
    288
}


# ============================================================
# VERİTABANI
# ============================================================

# SQLite aynı anda birden fazla Gunicorn worker/thread tarafından
# okunup yazılabildiği için bağlantı ayarlarını burada merkezi olarak
# güçlendiriyoruz. WAL sayesinde okuyucular yazma sırasında daha az
# birbirini bekler; busy_timeout da kısa süreli kilitlerde hata vermek
# yerine beklemeyi sağlar.
DB_RETRY_COUNT = 8
DB_RETRY_DELAY = 0.25
DB_BUSY_TIMEOUT_MS = 60000

# Aynı process içindeki scanner + Flask thread'lerinin SQLite yazmalarını
# sıraya sokar. Process'ler arası korumayı ise SQLite/WAL + fcntl sağlar.
DB_LOCK = threading.RLock()


def db_connect():

    conn = sqlite3.connect(
        DB_FILE,
        timeout=60,
        check_same_thread=False,
        isolation_level=None
    )

    conn.row_factory = sqlite3.Row

    # SQLite kilitlenmelerini azalt.
    conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    return conn


def _db_is_locked_error(exc):

    text = str(exc).lower()

    return (
        isinstance(exc, sqlite3.OperationalError)
        and (
            "database is locked" in text
            or "database table is locked" in text
            or "busy" in text
        )
    )


def _db_retry(action, description="SQLite işlemi"):

    last_error = None

    for attempt in range(1, DB_RETRY_COUNT + 1):

        try:
            with DB_LOCK:
                return action()

        except sqlite3.OperationalError as exc:

            last_error = exc

            if not _db_is_locked_error(exc):
                raise

            wait = DB_RETRY_DELAY * attempt

            print(
                f"⚠️ {description} SQLite kilitli; "
                f"{wait:.2f}s sonra tekrar denenecek "
                f"({attempt}/{DB_RETRY_COUNT})."
            )

            time.sleep(wait)

    raise last_error


def init_database():

    def _init():

        conn = db_connect()

        try:

            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    running INTEGER DEFAULT 1,
                    scanner INTEGER DEFAULT 0,
                    scan_in_progress INTEGER DEFAULT 0,
                    api_key INTEGER DEFAULT 0,
                    live_match_count INTEGER DEFAULT 0,
                    eligible_match_count INTEGER DEFAULT 0,
                    analyzed_match_count INTEGER DEFAULT 0,
                    match_count INTEGER DEFAULT 0,
                    league_count INTEGER DEFAULT 0,
                    updated TEXT,
                    last_scan_started TEXT,
                    last_scan_finished TEXT,
                    error TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS matches_data (
                    id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL
                )
            """)

            conn.execute("""
                INSERT OR IGNORE INTO system_state (
                    id, running, scanner, scan_in_progress, api_key,
                    live_match_count, eligible_match_count,
                    analyzed_match_count, match_count, league_count,
                    updated, last_scan_started, last_scan_finished, error
                )
                VALUES (1, 1, 0, 0, ?, 0, 0, 0, 0, ?, NULL, NULL, NULL, NULL)
            """, (1 if API_KEY else 0, len(ALLOWED_LEAGUES)))

            conn.commit()

        finally:
            conn.close()

    return _db_retry(_init, "Veritabanı başlatma")


def update_state(**kwargs):

    if not kwargs:
        return

    allowed = {
        "running",
        "scanner",
        "scan_in_progress",
        "api_key",
        "live_match_count",
        "eligible_match_count",
        "analyzed_match_count",
        "match_count",
        "league_count",
        "updated",
        "last_scan_started",
        "last_scan_finished",
        "error"
    }

    fields = []
    values = []

    for key, value in kwargs.items():
        if key not in allowed:
            continue
        fields.append(f"{key} = ?")
        values.append(value)

    if not fields:
        return

    def _update():

        conn = db_connect()

        try:
            values_with_id = values + [1]
            conn.execute(
                f"UPDATE system_state SET {', '.join(fields)} WHERE id = ?",
                values_with_id
            )
            conn.commit()

        finally:
            conn.close()

    return _db_retry(_update, "Durum güncelleme")


def get_state():

    def _read():

        conn = db_connect()

        try:
            row = conn.execute(
                "SELECT * FROM system_state WHERE id = 1"
            ).fetchone()

            if not row:
                return {
                    "running": True,
                    "scanner": False,
                    "scan_in_progress": False,
                    "api_key": bool(API_KEY),
                    "live_match_count": 0,
                    "eligible_match_count": 0,
                    "analyzed_match_count": 0,
                    "match_count": 0,
                    "league_count": len(ALLOWED_LEAGUES),
                    "updated": None,
                    "last_scan_started": None,
                    "last_scan_finished": None,
                    "error": None
                }

            result = dict(row)
            result["running"] = bool(result["running"])
            result["scanner"] = bool(result["scanner"])
            result["scan_in_progress"] = bool(result["scan_in_progress"])
            result["api_key"] = bool(result["api_key"])

            return result

        finally:
            conn.close()

    try:
        return _db_retry(_read, "Durum okuma")
    except sqlite3.OperationalError as exc:
        # Panelin 500 vermesini engelle. Scanner biraz sonra tekrar
        # yazacağı için geçici DB kilidinde güvenli bir durum döndür.
        print("❌ SQLite durum okuma hatası:", repr(exc))
        return {
            "running": True,
            "scanner": False,
            "scan_in_progress": False,
            "api_key": bool(API_KEY),
            "live_match_count": 0,
            "eligible_match_count": 0,
            "analyzed_match_count": 0,
            "match_count": 0,
            "league_count": len(ALLOWED_LEAGUES),
            "updated": None,
            "last_scan_started": None,
            "last_scan_finished": None,
            "error": "SQLite geçici olarak meşgul"
        }


def save_matches(matches):

    payload = json.dumps(matches, ensure_ascii=False)

    def _save():

        conn = db_connect()

        try:
            # DELETE + INSERT yerine tek bir UPSERT kullanıyoruz.
            # Böylece yazma kilidi çok daha kısa tutuluyor.
            conn.execute("""
                INSERT INTO matches_data (id, data)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET data = excluded.data
            """, (payload,))
            conn.commit()

        finally:
            conn.close()

    return _db_retry(_save, "Maçları kaydetme")


def load_matches():

    def _load():

        conn = db_connect()

        try:
            row = conn.execute(
                "SELECT data FROM matches_data WHERE id = 1"
            ).fetchone()

            if not row:
                return []

            try:
                return json.loads(row["data"])
            except Exception:
                return []

        finally:
            conn.close()

    try:
        return _db_retry(_load, "Maçları okuma")
    except sqlite3.OperationalError as exc:
        print("❌ SQLite maç okuma hatası:", repr(exc))
        return []


# ============================================================
# BAŞLAT
# ============================================================

init_database()

update_state(

    running=1,

    api_key=1 if API_KEY else 0,

    league_count=len(ALLOWED_LEAGUES)

)


# ============================================================
# API İSTEĞİ
# ============================================================

def api_get(endpoint, params=None):

    if not API_KEY:

        print("❌ API_KEY bulunamadı.")

        return None

    headers = {
        "x-apisports-key": API_KEY
    }

    try:

        response = requests.get(

            f"{API_URL}/{endpoint}",

            headers=headers,

            params=params,

            timeout=20

        )

        print(
            f"API -> {endpoint} | "
            f"HTTP {response.status_code}"
        )

        if response.status_code != 200:

            print(
                "❌ API HTTP hatası:",
                response.status_code
            )

            print(
                response.text[:500]
            )

            return None

        data = response.json()

        if data.get("errors"):

            print(
                "❌ API hatası:",
                data.get("errors")
            )

            return None

        return data

    except Exception as e:

        print(
            "❌ API bağlantı hatası:",
            repr(e)
        )

        return None


# ============================================================
# CANLI MAÇLAR
# ============================================================

def get_live_matches():

    data = api_get(
        "fixtures",
        {
            "live": "all"
        }
    )

    if not data:
        return []

    matches = data.get(
        "response",
        []
    )

    print(
        f"📡 API canlı maç sayısı: "
        f"{len(matches)}"
    )

    result = []

    for match in matches:

        league_id = (
            match
            .get("league", {})
            .get("id")
        )

        if league_id in ALLOWED_LEAGUES:

            result.append(match)

    print(
        f"✅ Uygun liglerde canlı maç: "
        f"{len(result)}"
    )

    return result


# ============================================================
# İSTATİSTİKLER
# ============================================================
def get_stats(fixture_id):

    print(f"🧪 STATS v4 | fixture={fixture_id}")

    # 1) Önce fixture detayındaki gömülü istatistikleri dene.
    for param_name in ("id", "ids"):

        print(
            f"🔁 Fixture detay denemesi: "
            f"fixtures?{param_name}={fixture_id}"
        )

        detail = api_get(
            "fixtures",
            {
                param_name: fixture_id
            }
        )

        if detail:

            response = detail.get(
                "response",
                []
            ) or []

            print(
                f"📦 fixture detay: "
                f"fixture={fixture_id} | "
                f"results={detail.get('results', len(response))} | "
                f"items={len(response)}"
            )

            if response:

                embedded = response[0].get(
                    "statistics",
                    []
                ) or []

                print(
                    f"📊 embedded statistics: "
                    f"fixture={fixture_id} | "
                    f"blocks={len(embedded)}"
                )

                if len(embedded) >= 2:

                    print(
                        f"✅ EMBEDDED STATS OK: "
                        f"fixture={fixture_id}"
                    )

                    return embedded

    # 2) Gömülü istatistik yoksa dedicated endpoint.
    data = api_get(
        "fixtures/statistics",
        {
            "fixture": fixture_id
        }
    )

    if data:

        stats = data.get(
            "response",
            []
        ) or []

        print(
            f"📊 dedicated statistics: "
            f"fixture={fixture_id} | "
            f"results={data.get('results', len(stats))} | "
            f"blocks={len(stats)}"
        )

        if len(stats) >= 2:

            print(
                f"✅ DEDICATED STATS OK: "
                f"fixture={fixture_id}"
            )

            return stats

    print(
        f"❌ STATS YOK: "
        f"fixture={fixture_id}"
    )

    return []


# ============================================================
# 65 LİG STATISTICS COVERAGE TESTİ
# ============================================================

def check_all_league_statistics_coverage():

    print("")
    print("=" * 70)
    print("🔬 65 LİG STATISTICS COVERAGE TESTİ BAŞLADI")
    print("=" * 70)

    results = []

    league_ids = list(ALLOWED_LEAGUES)

    for index, league_id in enumerate(
        league_ids,
        start=1
    ):

        print("")
        print(
            f"🔄 {index}/{len(league_ids)} "
            f"| League ID: {league_id}"
        )

        # Sezonu sabitlemiyoruz.
        # API'den ligin sezonlarını alıp
        # current=True olan sezonu buluyoruz.
        data = api_get(
            "leagues",
            {
                "id": league_id
            }
        )

        if not data:

            print(
                f"❌ League {league_id}: "
                f"API cevabı alınamadı"
            )

            results.append({
                "league_id": league_id,
                "season": None,
                "league_name": "BİLİNMİYOR",
                "country": "BİLİNMİYOR",
                "statistics_fixtures": None,
                "status": "API HATASI"
            })

            continue

        response = data.get(
            "response",
            []
        ) or []

        if not response:

            print(
                f"⚠️ League {league_id}: "
                f"response boş"
            )

            results.append({
                "league_id": league_id,
                "season": None,
                "league_name": "BULUNAMADI",
                "country": "BULUNAMADI",
                "statistics_fixtures": None,
                "status": "LİG BULUNAMADI"
            })

            continue

        league_data = response[0]

        league_info = league_data.get(
            "league",
            {}
        ) or {}

        country_info = league_data.get(
            "country",
            {}
        ) or {}

        league_name = league_info.get(
            "name",
            "Bilinmiyor"
        )

        country_name = country_info.get(
            "name",
            "Bilinmiyor"
        )

        seasons = league_data.get(
            "seasons",
            []
        ) or []

        # Önce current=True sezonunu bul.
        current_season = next(
            (
                season
                for season in seasons
                if season.get("current") is True
            ),
            None
        )

        # current=True bulunamazsa en yeni yılı kullan.
        if current_season is None and seasons:

            current_season = max(
                seasons,
                key=lambda x: x.get("year", 0)
            )

        if current_season is None:

            print(
                f"⚠️ {country_name} - "
                f"{league_name} | "
                f"Sezon bulunamadı"
            )

            results.append({
                "league_id": league_id,
                "season": None,
                "league_name": league_name,
                "country": country_name,
                "statistics_fixtures": None,
                "status": "SEZON BULUNAMADI"
            })

            continue

        season_year = current_season.get(
            "year"
        )

        coverage = current_season.get(
            "coverage",
            {}
        ) or {}

        fixtures_coverage = coverage.get(
            "fixtures",
            {}
        ) or {}

        statistics_fixtures = (
            fixtures_coverage.get(
                "statistics_fixtures",
                None
            )
        )

        if statistics_fixtures is True:

            status = "✅ VAR"

        elif statistics_fixtures is False:

            status = "❌ YOK"

        else:

            status = "⚠️ BİLİNMİYOR"

        print(
            f"{status} | "
            f"{country_name} - "
            f"{league_name} | "
            f"ID={league_id} | "
            f"SEZON={season_year} | "
            f"statistics_fixtures="
            f"{statistics_fixtures}"
        )

        results.append({
            "league_id": league_id,
            "season": season_year,
            "league_name": league_name,
            "country": country_name,
            "statistics_fixtures": statistics_fixtures,
            "status": status
        })

    # ========================================================
    # SONUÇ
    # ========================================================

    print("")
    print("=" * 70)
    print("📊 65 LİG COVERAGE SONUCU")
    print("=" * 70)

    available = [
        item
        for item in results
        if item["statistics_fixtures"] is True
    ]

    unavailable = [
        item
        for item in results
        if item["statistics_fixtures"] is False
    ]

    unknown = [
        item
        for item in results
        if item["statistics_fixtures"] is None
    ]

    print(
        f"✅ İstatistik VAR : "
        f"{len(available)}"
    )

    print(
        f"❌ İstatistik YOK : "
        f"{len(unavailable)}"
    )

    print(
        f"⚠️ Bilinmiyor     : "
        f"{len(unknown)}"
    )

    print(
        f"📊 Toplam         : "
        f"{len(results)}"
    )

    print("")
    print("--- İSTATİSTİK VAR ---")

    for item in available:

        print(
            f"✅ {item['league_id']} | "
            f"{item['country']} | "
            f"{item['league_name']} | "
            f"Sezon {item['season']}"
        )

    print("")
    print("--- İSTATİSTİK YOK ---")

    for item in unavailable:

        print(
            f"❌ {item['league_id']} | "
            f"{item['country']} | "
            f"{item['league_name']} | "
            f"Sezon {item['season']}"
        )

    print("")
    print("--- BİLİNMEYEN ---")

    for item in unknown:

        print(
            f"⚠️ {item['league_id']} | "
            f"{item['country']} | "
            f"{item['league_name']} | "
            f"{item['status']}"
        )

    print("=" * 70)

    return results
# ============================================================
# İSTATİSTİK DEĞERİ
# ============================================================

def stat_value(stats, name):

    for item in stats:

        if item.get("type") != name:
            continue

        value = item.get("value")

        if value is None:
            return 0

        if isinstance(value, str):

            value = value.replace(
                "%",
                ""
            )

        try:

            return float(value)

        except Exception:

            return 0

    return 0


# ============================================================
# TAKIM İSTATİSTİKLERİ
# ============================================================

def get_team_stats(stats):

    if len(stats) < 2:
        return None

    home = stats[0].get(
        "statistics",
        []
    )

    away = stats[1].get(
        "statistics",
        []
    )

    home_data = {

        "shots":
            stat_value(
                home,
                "Total Shots"
            ),

        "target":
            stat_value(
                home,
                "Shots on Goal"
            ),

        "corners":
            stat_value(
                home,
                "Corner Kicks"
            ),

        "inside":
            stat_value(
                home,
                "Shots insidebox"
            )

    }

    away_data = {

        "shots":
            stat_value(
                away,
                "Total Shots"
            ),

        "target":
            stat_value(
                away,
                "Shots on Goal"
            ),

        "corners":
            stat_value(
                away,
                "Corner Kicks"
            ),

        "inside":
            stat_value(
                away,
                "Shots insidebox"
            )

    }

    return home_data, away_data


# ============================================================
# TOPLAM İSTATİSTİK
# ============================================================

def get_total_stats(stats):

    team_stats = get_team_stats(stats)

    if not team_stats:
        return None

    home, away = team_stats

    return {

        "shots":
            home["shots"] +
            away["shots"],

        "target":
            home["target"] +
            away["target"],

        "corners":
            home["corners"] +
            away["corners"],

        "inside":
            home["inside"] +
            away["inside"]

    }


# ============================================================
# NORMAL SİNYAL
# ============================================================

def calculate_signal(
    match,
    current_stats
):

    total_shots = current_stats["shots"]
    total_target = current_stats["target"]
    total_corners = current_stats["corners"]
    total_inside = current_stats["inside"]

    minute = (
        match
        .get("fixture", {})
        .get("status", {})
        .get("elapsed")
        or 0
    )

    goals_home = (
        match
        .get("goals", {})
        .get("home")
        or 0
    )

    goals_away = (
        match
        .get("goals", {})
        .get("away")
        or 0
    )

    score = 0

    reasons = []

    if total_shots >= 15:

        score += 20

        reasons.append(
            "Çok fazla şut"
        )

    elif total_shots >= 11:

        score += 14

        reasons.append(
            "Şut sayısı yüksek"
        )

    elif total_shots >= 8:

        score += 8

    if total_target >= 8:

        score += 25

        reasons.append(
            "İsabetli şut çok yüksek"
        )

    elif total_target >= 6:

        score += 18

        reasons.append(
            "İsabetli şut yüksek"
        )

    elif total_target >= 4:

        score += 10

    if total_corners >= 9:

        score += 15

        reasons.append(
            "Korner baskısı yüksek"
        )

    elif total_corners >= 6:

        score += 9

    elif total_corners >= 4:

        score += 4

    if total_inside >= 10:

        score += 15

        reasons.append(
            "Ceza sahası şutları yüksek"
        )

    elif total_inside >= 7:

        score += 10

    if 55 <= minute <= 75:

        score += 5

    elif minute >= 76:

        score += 8

    if (

        goals_home +
        goals_away == 0

        and

        minute >= 55

    ):

        score += 8

        reasons.append(
            "55+ dakika ve skor 0-0"
        )

    elif (

        abs(
            goals_home -
            goals_away
        ) == 1

        and

        minute >= 60

    ):

        score += 5

        reasons.append(
            "Maç tek farklı"
        )

    return min(score, 100), reasons


# ============================================================
# İLK YARI SİNYAL
# ============================================================

def calculate_first_half_signal(
    match,
    team_stats
):

    if not team_stats:

        return (
            0,
            [],
            "Belirsiz"
        )

    home, away = team_stats

    minute = (
        match
        .get("fixture", {})
        .get("status", {})
        .get("elapsed")
        or 0
    )

    goals_home = (
        match
        .get("goals", {})
        .get("home")
        or 0
    )

    goals_away = (
        match
        .get("goals", {})
        .get("away")
        or 0
    )

    if minute < 15 or minute > 45:

        return (
            0,
            [],
            "Belirsiz"
        )

    score = 0

    reasons = []

    total_shots = (
        home["shots"] +
        away["shots"]
    )

    if total_shots >= 12:

        score += 22

        reasons.append(
            "İlk yarıda şut yoğunluğu çok yüksek"
        )

    elif total_shots >= 9:

        score += 16

        reasons.append(
            "İlk yarıda şut sayısı yüksek"
        )

    elif total_shots >= 6:

        score += 9

    total_target = (
        home["target"] +
        away["target"]
    )

    if total_target >= 6:

        score += 25

        reasons.append(
            "İsabetli şut baskısı çok yüksek"
        )

    elif total_target >= 4:

        score += 17

        reasons.append(
            "İsabetli şut baskısı yüksek"
        )

    elif total_target >= 3:

        score += 9

    total_corners = (
        home["corners"] +
        away["corners"]
    )

    if total_corners >= 6:

        score += 15

        reasons.append(
            "Korner baskısı yüksek"
        )

    elif total_corners >= 4:

        score += 9

    total_inside = (
        home["inside"] +
        away["inside"]
    )

    if total_inside >= 7:

        score += 15

        reasons.append(
            "Ceza sahası şutları yüksek"
        )

    elif total_inside >= 5:

        score += 9

    if (
        goals_home +
        goals_away == 0
    ):

        score += 8

        reasons.append(
            "Skor hâlâ 0-0"
        )

    home_pressure = (
        home["shots"] * 1.0 +
        home["target"] * 2.5 +
        home["corners"] * 1.2 +
        home["inside"] * 1.5
    )

    away_pressure = (
        away["shots"] * 1.0 +
        away["target"] * 2.5 +
        away["corners"] * 1.2 +
        away["inside"] * 1.5
    )

    if home_pressure > away_pressure * 1.20:

        expected_team = (
            "🏠 " +
            match["teams"]["home"]["name"]
        )

    elif away_pressure > home_pressure * 1.20:

        expected_team = (
            "✈️ " +
            match["teams"]["away"]["name"]
        )

    else:

        expected_team = (
            "⚽ Her iki takım"
        )

    return (
        min(score, 100),
        reasons,
        expected_team
    )


# ============================================================
# MAÇ ANALİZİ
# ============================================================

def analyze_match(match):

    fixture_id = (
        match["fixture"]["id"]
    )

    minute = (
        match
        .get("fixture", {})
        .get("status", {})
        .get("elapsed")
        or 0
    )

    if minute < 15:
        return None

    home = (
        match["teams"]["home"]["name"]
    )

    away = (
        match["teams"]["away"]["name"]
    )

    league_name = (
        match
        .get("league", {})
        .get(
            "name",
            "Bilinmeyen Lig"
        )
    )

    goals_home = (
        match
        .get("goals", {})
        .get("home")
        or 0
    )

    goals_away = (
        match
        .get("goals", {})
        .get("away")
        or 0
    )

    print(
        f"🔎 Analiz: "
        f"{home} - {away} | "
        f"{minute}' | "
        f"{goals_home}-{goals_away}"
    )

    stats = get_stats(
        fixture_id
    )

    if not stats:

        print(
            "⚠️ İstatistik bulunamadı."
        )

        return None

    current_stats = (
        get_total_stats(stats)
    )

    team_stats = (
        get_team_stats(stats)
    )

    if (
        not current_stats
        or not team_stats
    ):

        print(
            "⚠️ Eksik istatistik."
        )

        return None

    signal, reasons = (
        calculate_signal(
            match,
            current_stats
        )
    )

    first_signal = 0
    first_reasons = []
    expected_team = "Belirsiz"

    if 15 <= minute <= 45:

        (
            first_signal,
            first_reasons,
            expected_team
        ) = calculate_first_half_signal(
            match,
            team_stats
        )

    home_stats, away_stats = team_stats

    home_pressure = (
        home_stats["shots"] * 1.0 +
        home_stats["target"] * 2.5 +
        home_stats["corners"] * 1.2 +
        home_stats["inside"] * 1.5
    )

    away_pressure = (
        away_stats["shots"] * 1.0 +
        away_stats["target"] * 2.5 +
        away_stats["corners"] * 1.2 +
        away_stats["inside"] * 1.5
    )

    if home_pressure > away_pressure * 1.20:

        normal_expected_team = (
            "🏠 " + home
        )

    elif away_pressure > home_pressure * 1.20:

        normal_expected_team = (
            "✈️ " + away
        )

    else:

        normal_expected_team = (
            "⚽ Her iki takım"
        )

    # Genel öncelik puanı
    priority = max(
        signal,
        first_signal
    )

    # Güçlü sinyal
    strong_signal = (
        signal >= SIGNAL_LIMIT
    )

    strong_first_half = (
        first_signal >= FIRST_HALF_LIMIT
    )

    # Çok güçlü
    very_strong = (
        priority >= VERY_STRONG_SIGNAL
    )

    return {

        "fixture_id":
            fixture_id,

        "league":
            league_name,

        "home":
            home,

        "away":
            away,

        "minute":
            minute,

        "score_home":
            goals_home,

        "score_away":
            goals_away,

        "shots":
            int(current_stats["shots"]),

        "target":
            int(current_stats["target"]),

        "corners":
            int(current_stats["corners"]),

        "inside":
            int(current_stats["inside"]),

        "signal":
            int(signal),

        "signal_reasons":
            reasons,

        "expected_team":
            normal_expected_team,

        "first_half_signal":
            int(first_signal),

        "first_half_reasons":
            first_reasons,

        "strong_signal":
            strong_signal,

        "strong_first_half":
            strong_first_half,

        "very_strong":
            very_strong,

        "priority":
            int(priority)

    }


# ============================================================
# SCANNER LOCK
# ============================================================

def acquire_scanner_lock():

    try:

        lock_file = open(
            LOCK_FILE,
            "w"
        )

        fcntl.flock(
            lock_file.fileno(),
            fcntl.LOCK_EX |
            fcntl.LOCK_NB
        )

        return lock_file

    except Exception:

        return None


# ============================================================
# TARAMA MOTORU
# ============================================================

def scanner_loop():

    print("")
    print("=" * 60)
    print("🚀 ARKA PLAN MAÇ TARAMA MOTORU BAŞLADI")
    print("=" * 60)

    if API_KEY:
        print("🔑 API_KEY: OK")
    else:
        print("❌ API_KEY: YOK")

    try:
        while True:

            cycle_start = time.time()
            start_time = time.strftime("%H:%M:%S")

            try:
                update_state(
                    scanner=1,
                    scan_in_progress=1,
                    last_scan_started=start_time,
                    error=None
                )

                print("")
                print("=" * 60)
                print("📡 CANLI MAÇLAR TARAMASI BAŞLADI")
                print("=" * 60)

                matches = get_live_matches()
                live_count = len(matches)

                update_state(
                    live_match_count=live_count,
                    eligible_match_count=live_count,
                    analyzed_match_count=0,
                    match_count=0
                )

                # Eski taramanın maçlarını temizle. Yeni sonuçlar
                # analiz edildikçe aşağıda anlık olarak kaydedilecek.
                analyzed = []
                save_matches(analyzed)

                for index, match in enumerate(matches, start=1):

                    try:
                        fixture_id = match.get("fixture", {}).get("id")
                        home = match.get("teams", {}).get("home", {}).get("name", "?")
                        away = match.get("teams", {}).get("away", {}).get("name", "?")

                        print(
                            f"🔄 Maç {index}/{live_count}: "
                            f"{home} - {away} | fixture={fixture_id}"
                        )

                        result = analyze_match(match)

                        if result:
                            analyzed.append(result)

                            # Her başarılı analizden sonra DB'ye yaz.
                            # Böylece scanner sonraki maçta durursa bile
                            # daha önce analiz edilen maçlar panelde kalır.
                            analyzed.sort(
                                key=lambda x: (
                                    x.get("very_strong", False),
                                    x.get("strong_signal", False),
                                    x.get("strong_first_half", False),
                                    x.get("priority", 0),
                                    x.get("signal", 0),
                                    x.get("first_half_signal", 0)
                                ),
                                reverse=True
                            )

                            save_matches(analyzed)

                            update_state(
                                analyzed_match_count=len(analyzed),
                                match_count=len(analyzed),
                                updated=time.strftime("%H:%M:%S")
                            )

                            print(
                                f"💾 Kaydedildi: {len(analyzed)} analiz"
                            )
                        else:
                            print(
                                f"⚪ Analiz sonucu yok: {home} - {away}"
                            )

                    except Exception as e:
                        # Tek maçtaki hata bütün taramayı durdurmasın.
                        print(
                            f"❌ Maç analiz hatası "
                            f"({home} - {away}):",
                            repr(e)
                        )
                        continue

                # Tarama sonunda son kez sırala ve kaydet.
                analyzed.sort(
                    key=lambda x: (
                        x.get("very_strong", False),
                        x.get("strong_signal", False),
                        x.get("strong_first_half", False),
                        x.get("priority", 0),
                        x.get("signal", 0),
                        x.get("first_half_signal", 0)
                    ),
                    reverse=True
                )

                save_matches(analyzed)

                finish_time = time.strftime("%H:%M:%S")

                update_state(
                    scanner=1,
                    scan_in_progress=0,
                    analyzed_match_count=len(analyzed),
                    match_count=len(analyzed),
                    updated=finish_time,
                    last_scan_finished=finish_time,
                    error=None
                )

                print("")
                print(
                    f"📊 Analiz edilen maç: {len(analyzed)}"
                )
                print(
                    f"⏱ Tarama süresi: "
                    f"{time.time() - cycle_start:.1f} saniye"
                )
                print(
                    f"😴 {CHECK_SECONDS} saniye sonra "
                    f"yeni tarama başlayacak."
                )

                time.sleep(CHECK_SECONDS)

            except Exception as e:
                print("")
                print("=" * 60)
                print("❌ TARAMA DÖNGÜSÜ HATASI")
                print("=" * 60)
                print("HATA:", repr(e))
                print("=" * 60)

                try:
                    update_state(
                        scanner=1,
                        scan_in_progress=0,
                        error=str(e),
                        last_scan_finished=time.strftime("%H:%M:%S")
                    )
                except Exception as state_error:
                    print(
                        "❌ Hata durumu DB'ye yazılamadı:",
                        repr(state_error)
                    )

                # Döngü tamamen ölmesin; kısa bekleyip yeniden dene.
                time.sleep(5)

    except Exception as e:
        # Beklenmeyen bir thread ölümü watchdog tarafından görülebilsin.
        print("")
        print("=" * 60)
        print("💥 SCANNER THREAD BEKLENMEDİK ŞEKİLDE SONLANDI")
        print("=" * 60)
        print("HATA:", repr(e))
        print("=" * 60)

        try:
            update_state(
                scanner=0,
                scan_in_progress=0,
                error=f"Scanner thread durdu: {e}",
                updated=time.strftime("%H:%M:%S")
            )
        except Exception:
            pass


# ============================================================
# SCANNER BAŞLAT / WATCHDOG
# ============================================================

scanner_thread = None
scanner_lock_handle = None
watchdog_thread = None
scanner_start_lock = threading.Lock()


def start_scanner():

    global scanner_thread
    global scanner_lock_handle

    with scanner_start_lock:

        if (
            scanner_thread is not None
            and scanner_thread.is_alive()
        ):
            return True

        lock = acquire_scanner_lock()

        if lock is None:
            print(
                "ℹ️ Bu Gunicorn worker scanner lock alamadı."
            )
            return False

        scanner_lock_handle = lock

        print("")
        print("=" * 60)
        print("🚀 SCANNER OTOMATİK OLARAK BAŞLATILIYOR")
        print("=" * 60)
        print("")

        scanner_thread = threading.Thread(
            target=scanner_loop,
            name="FootballScanner",
            daemon=True
        )

        scanner_thread.start()

        print("✅ Scanner thread başlatıldı.")
        print("")
        return True


def scanner_watchdog_loop():
    """Scanner thread'i ölürse aynı worker içinde yeniden başlatır."""

    print("🛡️ Scanner watchdog başlatıldı.")

    while True:
        try:
            if scanner_thread is None or not scanner_thread.is_alive():
                print(
                    "⚠️ Scanner thread çalışmıyor. "
                    "Yeniden başlatma deneniyor..."
                )

                # Eski thread gerçekten öldüyse durumunu düzelt.
                try:
                    update_state(
                        scanner=0,
                        scan_in_progress=0,
                        error="Scanner thread çalışmıyor; yeniden başlatılıyor.",
                        updated=time.strftime("%H:%M:%S")
                    )
                except Exception as state_error:
                    print(
                        "⚠️ Watchdog durum güncelleme hatası:",
                        repr(state_error)
                    )

                start_scanner()

        except Exception as e:
            print(
                "❌ Watchdog hatası:",
                repr(e)
            )

        time.sleep(10)


# ============================================================
# WATCHDOG BAŞLAT
# ============================================================

def start_watchdog():
    global watchdog_thread

    if (
        watchdog_thread is not None
        and watchdog_thread.is_alive()
    ):
        return

    watchdog_thread = threading.Thread(
        target=scanner_watchdog_loop,
        name="ScannerWatchdog",
        daemon=True
    )

    watchdog_thread.start()


# ============================================================
# WEB PANELİ
# ============================================================

HTML = """
<!DOCTYPE html>

<html lang="tr">

<head>

<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width, initial-scale=1.0">

<title>Gol Sinyal Merkezi</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background:
        #07111f;

    color:
        #f8fafc;

}


/* =========================================================
   HEADER
   ========================================================= */

.header {

    background:
        linear-gradient(
            135deg,
            #0f172a,
            #172554
        );

    padding:
        18px 15px;

    text-align:
        center;

    border-bottom:
        1px solid #334155;

    position:
        sticky;

    top:
        0;

    z-index:
        50;

}

.header h1 {

    margin:
        0;

    font-size:
        24px;

}

.header p {

    margin:
        5px 0 0;

    color:
        #94a3b8;

    font-size:
        13px;

}


/* =========================================================
   CONTAINER
   ========================================================= */

.container {

    max-width:
        1050px;

    margin:
        auto;

    padding:
        12px;

}


/* =========================================================
   STATUS
   ========================================================= */

.status {

    background:
        #111c2d;

    border:
        1px solid #26364d;

    padding:
        13px;

    border-radius:
        12px;

    margin-bottom:
        12px;

}

.status-grid {

    display:
        grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                135px,
                1fr
            )
        );

    gap:
        7px;

}

.status-item {

    background:
        #0b1423;

    border-radius:
        8px;

    padding:
        8px 10px;

    font-size:
        12px;

}

.status-item b {

    display:
        block;

    color:
        #94a3b8;

    font-size:
        10px;

    margin-bottom:
        3px;

}


/* =========================================================
   SECTION
   ========================================================= */

.section-title {

    display:
        flex;

    align-items:
        center;

    justify-content:
        space-between;

    margin:
        15px 2px 8px;

}

.section-title h2 {

    margin:
        0;

    font-size:
        17px;

}

.section-title span {

    color:
        #94a3b8;

    font-size:
        11px;

}


/* =========================================================
   MATCH CARD
   ========================================================= */

.match {

    background:
        #111c2d;

    border:
        1px solid #26364d;

    border-radius:
        11px;

    padding:
        11px;

    margin-bottom:
        8px;

    transition:
        transform .15s,
        border .15s;

}

.match:hover {

    transform:
        translateY(-1px);

    border-color:
        #475569;

}


/* =========================================================
   VERY STRONG
   ========================================================= */

.match.very-strong {

    border:
        2px solid #22c55e;

    background:
        linear-gradient(
            135deg,
            #10291d,
            #111c2d
        );

    box-shadow:
        0 0 18px
        rgba(
            34,
            197,
            94,
            .20
        );

}


/* =========================================================
   STRONG
   ========================================================= */

.match.strong {

    border:
        2px solid #16a34a;

}


/* =========================================================
   FIRST HALF
   ========================================================= */

.match.first-half {

    border:
        2px solid #f59e0b;

}


/* =========================================================
   MATCH TOP
   ========================================================= */

.match-top {

    display:
        flex;

    align-items:
        center;

    justify-content:
        space-between;

    gap:
        10px;

}

.league {

    color:
        #60a5fa;

    font-size:
        11px;

    font-weight:
        bold;

    white-space:
        nowrap;

    overflow:
        hidden;

    text-overflow:
        ellipsis;

}

.minute {

    color:
        #cbd5e1;

    font-size:
        12px;

    white-space:
        nowrap;

}


/* =========================================================
   TEAMS
   ========================================================= */

.teams-row {

    display:
        grid;

    grid-template-columns:
        1fr auto 1fr;

    align-items:
        center;

    gap:
        8px;

    margin-top:
        7px;

}

.team {

    font-size:
        15px;

    font-weight:
        bold;

}

.team.away {

    text-align:
        right;

}

.score {

    font-size:
        21px;

    font-weight:
        900;

    white-space:
        nowrap;

}


/* =========================================================
   COMPACT STATS
   ========================================================= */

.stats {

    display:
        grid;

    grid-template-columns:
        repeat(
            4,
            1fr
        );

    gap:
        5px;

    margin-top:
        9px;

}

.stat {

    background:
        #0b1423;

    padding:
        6px 4px;

    border-radius:
        7px;

    text-align:
        center;

    color:
        #94a3b8;

    font-size:
        9px;

}

.stat b {

    display:
        block;

    font-size:
        15px;

    color:
        #f8fafc;

    margin-top:
        2px;

}


/* =========================================================
   SIGNAL
   ========================================================= */

.signal-row {

    display:
        flex;

    align-items:
        center;

    justify-content:
        space-between;

    margin-top:
        8px;

    gap:
        7px;

}

.signal-score {

    font-size:
        17px;

    font-weight:
        900;

}

.signal-normal {

    color:
        #94a3b8;

}

.signal-good {

    color:
        #4ade80;

}

.signal-very {

    color:
        #22c55e;

}


/* =========================================================
   BADGES
   ========================================================= */

.badge {

    display:
        inline-block;

    padding:
        4px 7px;

    border-radius:
        20px;

    font-size:
        9px;

    font-weight:
        bold;

}

.badge-green {

    background:
        #14532d;

    color:
        #86efac;

}

.badge-orange {

    background:
        #713f12;

    color:
        #fde68a;

}

.badge-blue {

    background:
        #172554;

    color:
        #93c5fd;

}


/* =========================================================
   DETAILS
   ========================================================= */

.details {

    margin-top:
        7px;

    color:
        #cbd5e1;

    font-size:
        10px;

    line-height:
        1.45;

}

.expected {

    margin-top:
        7px;

    padding:
        7px 9px;

    background:
        #0b1423;

    border-radius:
        7px;

    font-size:
        11px;

}

.reasons {

    margin-top:
        6px;

    color:
        #94a3b8;

}

.reason {

    margin-top:
        2px;

}


/* =========================================================
   EMPTY
   ========================================================= */

.empty {

    text-align:
        center;

    padding:
        45px 15px;

    color:
        #94a3b8;

    background:
        #111c2d;

    border:
        1px solid #26364d;

    border-radius:
        12px;

}


/* =========================================================
   NOTIFICATION
   ========================================================= */

.notification-container {

    position:
        fixed;

    top:
        72px;

    right:
        12px;

    width:
        min(
            350px,
            calc(
                100vw - 24px
            )
        );

    z-index:
        9999;

}

.notification {

    background:
        #10291d;

    border:
        1px solid #22c55e;

    box-shadow:
        0 8px 30px
        rgba(
            0,
            0,
            0,
            .45
        );

    border-radius:
        12px;

    padding:
        13px;

    margin-bottom:
        8px;

    animation:
        slideIn .25s ease;

}

.notification-title {

    color:
        #4ade80;

    font-size:
        13px;

    font-weight:
        900;

}

.notification-match {

    margin-top:
        5px;

    font-size:
        14px;

    font-weight:
        bold;

}

.notification-score {

    margin-top:
        4px;

    color:
        #bbf7d0;

    font-size:
        12px;

}

@keyframes slideIn {

    from {

        opacity:
            0;

        transform:
            translateX(
                30px
            );

    }

    to {

        opacity:
            1;

        transform:
            translateX(
                0
            );

    }

}


/* =========================================================
   MOBILE
   ========================================================= */

@media (
    max-width: 600px
) {

    .container {

        padding:
            8px;

    }

    .header h1 {

        font-size:
            20px;

    }

    .team {

        font-size:
            13px;

    }

    .score {

        font-size:
            19px;

    }

    .stats {

        gap:
            3px;

    }

}


/* =========================================================
   DESKTOP
   ========================================================= */

@media (
    min-width: 900px
) {

    .match {

        padding:
            12px 15px;

    }

}

</style>

</head>


<body>


<div class="header">

    <h1>
        ⚽ GOL SİNYAL MERKEZİ
    </h1>

    <p>
        Yüksek gol ihtimali olan canlı maçlar öne çıkarılır
    </p>

</div>


<div class="container">


    <!-- STATUS -->

    <div class="status">

        <div class="status-grid">

            <div class="status-item">
                <b>SİSTEM</b>
                <span id="status">
                    Bağlanıyor...
                </span>
            </div>

            <div class="status-item">
                <b>SCANNER</b>
                <span id="scanner">
                    -
                </span>
            </div>

            <div class="status-item">
                <b>CANLI</b>
                <span id="livecount">
                    0
                </span>
            </div>

            <div class="status-item">
                <b>ANALİZ</b>
                <span id="analyzedcount">
                    0
                </span>
            </div>

            <div class="status-item">
                <b>SON GÜNCELLEME</b>
                <span id="updated">
                    -
                </span>
            </div>

            <div class="status-item">
                <b>SON TARAMA</b>
                <span id="scanfinish">
                    -
                </span>
            </div>

        </div>

    </div>


    <!-- VERY STRONG -->

    <div
        class="section-title"
        id="veryStrongTitle"
        style="display:none;"
    >

        <h2>
            🔥 ÇOK YÜKSEK GOL SİNYALİ
        </h2>

        <span>
            %80+
        </span>

    </div>

    <div id="veryStrongMatches"></div>


    <!-- STRONG -->

    <div
        class="section-title"
        id="strongTitle"
        style="display:none;"
    >

        <h2>
            🟢 YÜKSEK GOL SİNYALİ
        </h2>

        <span>
            %65+
        </span>

    </div>

    <div id="strongMatches"></div>


    <!-- OTHER -->

    <div
        class="section-title"
        id="otherTitle"
        style="display:none;"
    >

        <h2>
            📊 DİĞER ANALİZLER
        </h2>

        <span>
            Daha düşük sinyaller
        </span>

    </div>

    <div id="otherMatches"></div>


    <div
        id="empty"
        class="empty"
        style="display:none;"
    >

        ⚽

        <br><br>

        <b>
            Şu anda analiz edilen
            canlı maç yok.
        </b>

        <br><br>

        Sistem otomatik olarak
        taramaya devam ediyor.

    </div>


</div>


<!-- BİLDİRİM ALANI -->

<div
    id="notificationContainer"
    class="notification-container"
></div>


<script>


// ==========================================================
// BİLDİRİM TAKİBİ
// ==========================================================

const notifiedSignals =
    new Set();

let firstLoad =
    true;


// ==========================================================
// HTML GÜVENLİĞİ
// ==========================================================

function escapeHtml(text) {

    const div =
        document.createElement(
            "div"
        );

    div.textContent =
        text ?? "";

    return div.innerHTML;
}


// ==========================================================
// BİLDİRİM GÖSTER
// ==========================================================

function showNotification(match) {

    const container =
        document.getElementById(
            "notificationContainer"
        );

    const box =
        document.createElement(
            "div"
        );

    box.className =
        "notification";

    box.innerHTML = `

        <div class="notification-title">
            🔥 YÜKSEK GOL SİNYALİ
        </div>

        <div class="notification-match">
            ${escapeHtml(match.home)}
            -
            ${escapeHtml(match.away)}
        </div>

        <div class="notification-score">

            Gol sinyali:
            <b>
                %${match.signal}
            </b>

            &nbsp; | &nbsp;

            ${match.minute}'

        </div>

    `;

    container.appendChild(
        box
    );

    setTimeout(
        () => {

            box.remove();

        },
        7000
    );


    // Tarayıcı masaüstü bildirimi
    // sadece izin verilmişse

    if (
        "Notification"
        in window
        &&
        Notification.permission ===
        "granted"
    ) {

        try {

            new Notification(
                "🔥 Yüksek Gol Sinyali",
                {

                    body:
                        match.home +
                        " - " +
                        match.away +
                        " | Gol sinyali: %" +
                        match.signal

                }
            );

        } catch (e) {

            console.log(
                "Bildirim oluşturulamadı:",
                e
            );

        }

    }

}


// ==========================================================
// BİLDİRİM İZNİ
// ==========================================================

function requestNotificationPermission() {

    if (
        !(
            "Notification"
            in window
        )
    ) {

        return;

    }

    if (
        Notification.permission ===
        "default"
    ) {

        Notification.requestPermission()
        .then(
            permission => {

                console.log(
                    "Bildirim izni:",
                    permission
                );

            }
        )
        .catch(
            error => {

                console.log(
                    "Bildirim izni alınamadı:",
                    error
                );

            }
        );

    }

}


// Sayfaya ilk tıklamada
// bildirim izni iste

document.addEventListener(
    "click",
    requestNotificationPermission,
    {
        once: true
    }
);


// ==========================================================
// MAÇ KARTI
// ==========================================================

function createMatch(match) {

    let className =
        "match";

    if (
        match.very_strong
    ) {

        className +=
            " very-strong";

    } else if (
        match.strong_signal
    ) {

        className +=
            " strong";

    } else if (
        match.strong_first_half
    ) {

        className +=
            " first-half";

    }


    let signalClass =
        "signal-normal";

    if (
        match.signal >= 80
    ) {

        signalClass =
            "signal-very";

    } else if (
        match.signal >= 65
    ) {

        signalClass =
            "signal-good";

    }


    let badge = "";


    if (
        match.very_strong
    ) {

        badge = `

            <span class="badge badge-green">
                🔥 ÇOK GÜÇLÜ
            </span>

        `;

    } else if (
        match.strong_signal
    ) {

        badge = `

            <span class="badge badge-green">
                🟢 GÜÇLÜ
            </span>

        `;

    } else if (
        match.strong_first_half
    ) {

        badge = `

            <span class="badge badge-orange">
                ⚡ İLK YARI
            </span>

        `;

    }


    let reasons = "";

    if (
        match.signal_reasons
        &&
        match.signal_reasons.length
    ) {

        reasons =
            match.signal_reasons
            .slice(0, 3)
            .map(
                r =>
                    `<div class="reason">
                        • ${escapeHtml(r)}
                    </div>`
            )
            .join("");

    }


    let firstHalf = "";

    if (
        match.first_half_signal >= 65
    ) {

        firstHalf = `

            <span class="badge badge-orange">
                ⚡ İlk Yarı %${match.first_half_signal}
            </span>

        `;

    }


    return `

        <div
            class="${className}"
            data-fixture="${match.fixture_id}"
        >

            <div class="match-top">

                <div class="league">

                    🏆
                    ${escapeHtml(match.league)}

                </div>

                <div class="minute">

                    ⏱ ${match.minute}'

                </div>

            </div>


            <div class="teams-row">

                <div class="team">

                    ${escapeHtml(match.home)}

                </div>

                <div class="score">

                    ${match.score_home}
                    -
                    ${match.score_away}

                </div>

                <div class="team away">

                    ${escapeHtml(match.away)}

                </div>

            </div>


            <div class="stats">

                <div class="stat">

                    ŞUT

                    <b>
                        ${match.shots}
                    </b>

                </div>

                <div class="stat">

                    İSABET

                    <b>
                        ${match.target}
                    </b>

                </div>

                <div class="stat">

                    KORNER

                    <b>
                        ${match.corners}
                    </b>

                </div>

                <div class="stat">

                    CEZA SAHASI

                    <b>
                        ${match.inside}
                    </b>

                </div>

            </div>


            <div class="signal-row">

                <div>

                    <span class="signal-score ${signalClass}">

                        🔥 %${match.signal}

                    </span>

                    ${badge}

                    ${firstHalf}

                </div>

            </div>


            <div class="expected">

                ⚽

                <b>
                    Gol beklenen taraf:
                </b>

                ${escapeHtml(
                    match.expected_team
                )}

            </div>


            ${
                reasons
                ?
                `
                    <div class="details">

                        <b>
                            Neden?
                        </b>

                        <div class="reasons">
                            ${reasons}
                        </div>

                    </div>
                `
                :
                ""
            }

        </div>

    `;

}


// ==========================================================
// MAÇLARI YERLEŞTİR
// ==========================================================

function renderMatches(matches) {

    const veryStrong =
        matches.filter(
            m =>
                m.very_strong
        );

    const strong =
        matches.filter(
            m =>
                !m.very_strong
                &&
                (
                    m.strong_signal
                    ||
                    m.strong_first_half
                )
        );

    const other =
        matches.filter(
            m =>
                !m.very_strong
                &&
                !m.strong_signal
                &&
                !m.strong_first_half
        );


    document.getElementById(
        "veryStrongTitle"
    ).style.display =
        veryStrong.length
        ? "flex"
        : "none";


    document.getElementById(
        "strongTitle"
    ).style.display =
        strong.length
        ? "flex"
        : "none";


    document.getElementById(
        "otherTitle"
    ).style.display =
        other.length
        ? "flex"
        : "none";


    document.getElementById(
        "veryStrongMatches"
    ).innerHTML =
        veryStrong
        .map(createMatch)
        .join("");


    document.getElementById(
        "strongMatches"
    ).innerHTML =
        strong
        .map(createMatch)
        .join("");


    document.getElementById(
        "otherMatches"
    ).innerHTML =
        other
        .map(createMatch)
        .join("");


    document.getElementById(
        "empty"
    ).style.display =
        matches.length
        ? "none"
        : "block";


    // ======================================================
    // YENİ YÜKSEK SİNYAL BİLDİRİMLERİ
    // ======================================================

    if (!firstLoad) {

        matches.forEach(
            match => {

                if (
                    match.signal < 65
                ) {

                    return;

                }

                const key =
                    String(
                        match.fixture_id
                    ) +
                    "-" +
                    String(
                        match.signal
                    );

                if (
                    notifiedSignals.has(
                        key
                    )
                ) {

                    return;

                }

                notifiedSignals.add(
                    key
                );

                showNotification(
                    match
                );

            }
        );

    }


    // İlk yüklemede
    // mevcut maçları bildirme

    if (firstLoad) {

        matches.forEach(
            match => {

                if (
                    match.signal >= 65
                ) {

                    const key =
                        String(
                            match.fixture_id
                        ) +
                        "-" +
                        String(
                            match.signal
                        );

                    notifiedSignals.add(
                        key
                    );

                }

            }
        );

        firstLoad = false;

    }


    // Çok uzun süre açık kalan
    // sayfada Set'in büyümesini engelle

    if (
        notifiedSignals.size > 500
    ) {

        notifiedSignals.clear();

    }

}


// ==========================================================
// STATUS
// ==========================================================

async function loadStatus() {

    try {

        const response =
            await fetch(
                "/api/status",
                {
                    cache:
                        "no-store"
                }
            );

        const data =
            await response.json();


        document.getElementById(
            "status"
        ).textContent =

            data.error

            ? "🔴 Hata"

            : "🟢 Çalışıyor";


        document.getElementById(
            "scanner"
        ).textContent =

            data.scanner

            ? (
                data.scan_in_progress
                ? "🟢 Tarıyor"
                : "🟢 Hazır"
            )

            : "🔴 Durdu";


        document.getElementById(
            "livecount"
        ).textContent =
            data.live_match_count || 0;


        document.getElementById(
            "analyzedcount"
        ).textContent =
            data.analyzed_match_count || 0;


        document.getElementById(
            "updated"
        ).textContent =
            data.updated || "-";


        document.getElementById(
            "scanfinish"
        ).textContent =
            data.last_scan_finished || "-";

    }

    catch (error) {

        document.getElementById(
            "status"
        ).textContent =
            "🔴 Bağlantı hatası";

    }

}


// ==========================================================
// MATCHES
// ==========================================================

async function loadMatches() {

    try {

        const response =
            await fetch(
                "/api/matches",
                {
                    cache:
                        "no-store"
                }
            );

        const data =
            await response.json();


        renderMatches(
            data.matches || []
        );

    }

    catch (error) {

        console.log(
            "Maç verisi alınamadı:",
            error
        );

    }

}


// ==========================================================
// REFRESH
// ==========================================================

async function refreshAll() {

    await loadStatus();

    await loadMatches();

}


refreshAll();


setInterval(
    refreshAll,
    10000
);


</script>

</body>

</html>
"""


# ============================================================
# ANA SAYFA
# ============================================================

@app.route("/")
def index():

    return render_template_string(
        HTML
    )


# ============================================================
# API MATCHES
# ============================================================

@app.route("/api/matches")
def api_matches():

    state = get_state()

    matches = load_matches()

    return jsonify({

        "matches":
            matches,

        "updated":
            state["updated"],

        "error":
            state["error"],

        "scanner_running":
            state["scanner"],

        "scan_in_progress":
            state["scan_in_progress"],

        "last_scan_started":
            state["last_scan_started"],

        "last_scan_finished":
            state["last_scan_finished"]

    })


# ============================================================
# API STATUS
# ============================================================

@app.route("/api/status")
def api_status():

    state = get_state()

    return jsonify({

        "running":
            state["running"],

        "api_key":
            state["api_key"],

        "scanner":
            state["scanner"],

        "scan_in_progress":
            state["scan_in_progress"],

        "league_count":
            state["league_count"],

        "live_match_count":
            state["live_match_count"],

        "eligible_match_count":
            state["eligible_match_count"],

        "analyzed_match_count":
            state["analyzed_match_count"],

        "match_count":
            state["match_count"],

        "updated":
            state["updated"],

        "last_scan_started":
            state["last_scan_started"],

        "last_scan_finished":
            state["last_scan_finished"],

        "error":
            state["error"]

    })


# ============================================================
# SCANNER + WATCHDOG BAŞLAT
# ============================================================

start_scanner()
start_watchdog()


# ============================================================
# LOCAL
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    print("")
    print("=" * 60)
    print("🌐 FLASK WEB SUNUCUSU BAŞLIYOR")
    print("=" * 60)

    print(
        f"🌐 PORT: {port}"
    )

    print("=" * 60)
    print("")

    app.run(

        host="0.0.0.0",

        port=port,

        debug=False,

        use_reloader=False

    )
