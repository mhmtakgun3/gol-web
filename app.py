import os
import time
import json
import sqlite3
import threading
import requests
import fcntl
from datetime import datetime, timezone, timedelta

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

VERY_STRONG_LIMIT = 80

WATCHDOG_LIMIT = 120

PANEL_REFRESH_SECONDS = 10

DB_FILE = "/tmp/gol_scanner.db"

LOCK_FILE = "/tmp/gol_scanner.lock"

TURKEY_TZ = timezone(timedelta(hours=3))


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
# ZAMAN
# ============================================================

def now_tr():
    return datetime.now(TURKEY_TZ)


def time_string():
    return now_tr().strftime("%H:%M:%S")


def timestamp():
    return now_tr().timestamp()


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_database():
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

                last_scan_timestamp REAL,

                next_scan_timestamp REAL,

                error TEXT
            )
        """)

        conn.execute("""
            INSERT OR IGNORE INTO system_state (
                id,
                running,
                scanner,
                scan_in_progress,
                api_key,
                live_match_count,
                eligible_match_count,
                analyzed_match_count,
                match_count,
                league_count,
                updated,
                last_scan_started,
                last_scan_finished,
                last_scan_timestamp,
                next_scan_timestamp,
                error
            )
            VALUES (
                1,
                1,
                0,
                0,
                ?,
                0,
                0,
                0,
                0,
                ?,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL
            )
        """, (
            1 if API_KEY else 0,
            len(ALLOWED_LEAGUES)
        ))

        # Eski database varsa eksik kolonları tamamla.
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(system_state)"
            ).fetchall()
        }

        if "last_scan_timestamp" not in columns:
            conn.execute("""
                ALTER TABLE system_state
                ADD COLUMN last_scan_timestamp REAL
            """)

        if "next_scan_timestamp" not in columns:
            conn.execute("""
                ALTER TABLE system_state
                ADD COLUMN next_scan_timestamp REAL
            """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS matches_data (
                id INTEGER PRIMARY KEY,
                data TEXT NOT NULL
            )
        """)

        conn.commit()

    finally:
        conn.close()


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
        "last_scan_timestamp",
        "next_scan_timestamp",
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

    conn = db_connect()

    try:
        values.append(1)

        conn.execute(
            f"""
            UPDATE system_state
            SET {", ".join(fields)}
            WHERE id = ?
            """,
            values
        )

        conn.commit()

    finally:
        conn.close()


def get_state():
    conn = db_connect()

    try:
        row = conn.execute("""
            SELECT *
            FROM system_state
            WHERE id = 1
        """).fetchone()

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
                "last_scan_timestamp": None,
                "next_scan_timestamp": None,
                "error": None
            }

        result = dict(row)

        result["running"] = bool(result["running"])
        result["scanner"] = bool(result["scanner"])
        result["scan_in_progress"] = bool(
            result["scan_in_progress"]
        )
        result["api_key"] = bool(result["api_key"])

        return result

    finally:
        conn.close()


def save_matches(matches):
    conn = db_connect()

    try:
        conn.execute("""
            DELETE FROM matches_data
        """)

        conn.execute("""
            INSERT INTO matches_data (
                id,
                data
            )
            VALUES (?, ?)
        """, (
            1,
            json.dumps(
                matches,
                ensure_ascii=False
            )
        ))

        conn.commit()

    finally:
        conn.close()


def load_matches():
    conn = db_connect()

    try:
        row = conn.execute("""
            SELECT data
            FROM matches_data
            WHERE id = 1
        """).fetchone()

        if not row:
            return []

        try:
            return json.loads(row["data"])
        except Exception:
            return []

    finally:
        conn.close()


# ============================================================
# DATABASE BAŞLAT
# ============================================================

init_database()

update_state(
    running=1,
    api_key=1 if API_KEY else 0,
    league_count=len(ALLOWED_LEAGUES)
)


# ============================================================
# API
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

    except Exception as exc:
        print(
            "❌ API bağlantı hatası:",
            repr(exc)
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
# İSTATİSTİK
# ============================================================

def get_stats(fixture_id):
    data = api_get(
        "fixtures/statistics",
        {
            "fixture": fixture_id
        }
    )

    if not data:
        return []

    return data.get(
        "response",
        []
    )


def stat_value(stats, name):
    for item in stats:
        if item.get("type") != name:
            continue

        value = item.get("value")

        if value is None:
            return 0

        if isinstance(value, str):
            value = value.replace("%", "")

        try:
            return float(value)
        except Exception:
            return 0

    return 0


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
        "shots": stat_value(
            home,
            "Total Shots"
        ),

        "target": stat_value(
            home,
            "Shots on Goal"
        ),

        "corners": stat_value(
            home,
            "Corner Kicks"
        ),

        "inside": stat_value(
            home,
            "Shots insidebox"
        )
    }

    away_data = {
        "shots": stat_value(
            away,
            "Total Shots"
        ),

        "target": stat_value(
            away,
            "Shots on Goal"
        ),

        "corners": stat_value(
            away,
            "Corner Kicks"
        ),

        "inside": stat_value(
            away,
            "Shots insidebox"
        )
    }

    return home_data, away_data


def get_total_stats(stats):
    team_stats = get_team_stats(stats)

    if not team_stats:
        return None

    home, away = team_stats

    return {
        "shots": (
            home["shots"] +
            away["shots"]
        ),

        "target": (
            home["target"] +
            away["target"]
        ),

        "corners": (
            home["corners"] +
            away["corners"]
        ),

        "inside": (
            home["inside"] +
            away["inside"]
        )
    }


# ============================================================
# NORMAL SİNYAL
# ============================================================

def calculate_signal(match, current_stats):
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
        return 0, [], "Belirsiz"

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
        return 0, [], "Belirsiz"

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
        expected_team = "⚽ Her iki takım"

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
        f"🔎 Analiz: {home} - {away} | "
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

    current_stats = get_total_stats(
        stats
    )

    team_stats = get_team_stats(
        stats
    )

    if (
        not current_stats
        or
        not team_stats
    ):
        print(
            "⚠️ Eksik istatistik."
        )

        return None

    signal, reasons = calculate_signal(
        match,
        current_stats
    )

    first_signal = 0

    first_reasons = []

    first_expected_team = "Belirsiz"

    if 15 <= minute <= 45:
        (
            first_signal,
            first_reasons,
            first_expected_team
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
        expected_team = "🏠 " + home

    elif away_pressure > home_pressure * 1.20:
        expected_team = "✈️ " + away

    else:
        expected_team = "⚽ Her iki takım"

    priority = max(
        signal,
        first_signal
    )

    very_strong = (
        priority >= VERY_STRONG_LIMIT
    )

    return {
        "fixture_id": fixture_id,

        "league": league_name,

        "home": home,

        "away": away,

        "minute": minute,

        "score_home": goals_home,

        "score_away": goals_away,

        "shots": int(current_stats["shots"]),

        "target": int(current_stats["target"]),

        "corners": int(current_stats["corners"]),

        "inside": int(current_stats["inside"]),

        "signal": int(signal),

        "signal_reasons": reasons,

        "expected_team": expected_team,

        "first_half_signal": int(first_signal),

        "first_half_reasons": first_reasons,

        "first_half_expected_team": (
            first_expected_team
        ),

        "strong_signal": (
            signal >= SIGNAL_LIMIT
        ),

        "strong_first_half": (
            first_signal >= SIGNAL_LIMIT
        ),

        "very_strong": very_strong,

        "priority": int(priority),

        "updated_at": time_string()
    }


# ============================================================
# SIRALAMA
# ============================================================

def sort_matches(matches):
    return sorted(
        matches,
        key=lambda item: (
            item.get("priority", 0),
            item.get("signal", 0),
            item.get("first_half_signal", 0),
            item.get("shots", 0),
            item.get("target", 0)
        ),
        reverse=True
    )


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
# SCANNER
# ============================================================

scanner_thread = None

scanner_lock_file = None


def scanner_loop():
    print("")
    print("=" * 60)
    print("🚀 GOL SCANNER BAŞLADI")
    print("=" * 60)

    if API_KEY:
        print("🔑 API_KEY: OK")
    else:
        print("❌ API_KEY: YOK")

    while True:
        cycle_start = time.time()

        started = time_string()

        update_state(
            scanner=1,
            scan_in_progress=1,
            last_scan_started=started,
            error=None
        )

        try:
            print("")
            print("=" * 60)
            print("📡 YENİ TARAMA")
            print("=" * 60)

            matches = get_live_matches()

            live_count = len(matches)

            update_state(
                live_match_count=live_count,
                eligible_match_count=live_count
            )

            analyzed = []

            for match in matches:
                try:
                    result = analyze_match(
                        match
                    )

                    if result:
                        analyzed.append(
                            result
                        )

                except Exception as exc:
                    print(
                        "❌ Maç analiz hatası:",
                        repr(exc)
                    )

            analyzed = sort_matches(
                analyzed
            )

            save_matches(
                analyzed
            )

            finished = time_string()

            finished_timestamp = time.time()

            next_scan = (
                finished_timestamp +
                CHECK_SECONDS
            )

            update_state(
                scanner=1,
                scan_in_progress=0,
                analyzed_match_count=len(
                    analyzed
                ),
                match_count=len(
                    analyzed
                ),
                updated=finished,
                last_scan_finished=finished,
                last_scan_timestamp=finished_timestamp,
                next_scan_timestamp=next_scan,
                error=None
            )

            print("")
            print(
                f"📊 Analiz edilen: "
                f"{len(analyzed)}"
            )

            print(
                f"⏱ Süre: "
                f"{time.time() - cycle_start:.1f}s"
            )

            print(
                f"😴 {CHECK_SECONDS}s sonra "
                f"yeni tarama."
            )

        except Exception as exc:
            print("")
            print("=" * 60)
            print("❌ SCANNER HATASI")
            print("=" * 60)

            print(
                repr(exc)
            )

            print("=" * 60)

            update_state(
                scanner=1,
                scan_in_progress=0,
                error=str(exc),
                last_scan_finished=time_string()
            )

        time.sleep(
            CHECK_SECONDS
        )


# ============================================================
# WATCHDOG
# ============================================================

def watchdog_loop():
    print(
        "🐕 Watchdog başladı."
    )

    while True:
        try:
            state = get_state()

            last_scan = (
                state.get(
                    "last_scan_timestamp"
                )
            )

            if not last_scan:
                healthy = False
                message = (
                    "Henüz başarılı tarama yok."
                )

            else:
                seconds_since = (
                    time.time() -
                    float(last_scan)
                )

                if seconds_since <= WATCHDOG_LIMIT:
                    healthy = True
                    message = (
                        "Scanner sağlıklı."
                    )

                else:
                    healthy = False
                    message = (
                        "Scanner son taramayı "
                        f"{int(seconds_since)} saniye önce yaptı."
                    )

            if not healthy:
                print(
                    "⚠️ WATCHDOG:",
                    message
                )

            time.sleep(15)

        except Exception as exc:
            print(
                "❌ Watchdog hatası:",
                repr(exc)
            )

            time.sleep(15)


# ============================================================
# SCANNER BAŞLAT
# ============================================================

def start_scanner():
    global scanner_thread
    global scanner_lock_file

    if (
        scanner_thread is not None
        and
        scanner_thread.is_alive()
    ):
        return

    scanner_lock_file = (
        acquire_scanner_lock()
    )

    if scanner_lock_file is None:
        print(
            "ℹ️ Bu worker scanner lock alamadı."
        )
        return

    scanner_thread = threading.Thread(
        target=scanner_loop,
        name="FootballScanner",
        daemon=True
    )

    scanner_thread.start()

    watchdog_thread = threading.Thread(
        target=watchdog_loop,
        name="ScannerWatchdog",
        daemon=True
    )

    watchdog_thread.start()

    print(
        "✅ Scanner + Watchdog başlatıldı."
    )


# ============================================================
# HTML
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
    background: #07111f;
    color: #f8fafc;
    font-family: Arial, sans-serif;
}

.header {
    padding: 18px;
    text-align: center;
    background: #0b1628;
    border-bottom: 1px solid #1e293b;
}

.header h1 {
    margin: 0;
    font-size: 25px;
}

.header p {
    margin: 7px 0 0;
    color: #94a3b8;
}

.container {
    width: 100%;
    max-width: 1100px;
    margin: auto;
    padding: 14px;
}

.status {
    background: #0f1d31;
    border: 1px solid #24344d;
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 15px;
}

.status-grid {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(140px, 1fr));
    gap: 8px;
}

.status-item {
    background: #091525;
    padding: 10px;
    border-radius: 9px;
}

.status-label {
    color: #94a3b8;
    font-size: 12px;
}

.status-value {
    font-size: 18px;
    font-weight: bold;
    margin-top: 3px;
}

.healthy {
    color: #4ade80;
}

.unhealthy {
    color: #f87171;
}

.section-title {
    margin: 18px 0 10px;
    font-size: 19px;
}

.match {
    background: #101e32;
    border: 1px solid #253752;
    border-radius: 13px;
    margin-bottom: 10px;
    padding: 13px;
}

.match.strong {
    border: 2px solid #22c55e;
    background: #0c2119;
}

.match.very-strong {
    border: 2px solid #ef4444;
    background: #241014;
    box-shadow: 0 0 14px rgba(239,68,68,0.15);
}

.match.first-strong {
    border: 2px solid #f59e0b;
}

.top-badge {
    display: inline-block;
    padding: 4px 8px;
    border-radius: 8px;
    background: #dc2626;
    font-size: 11px;
    font-weight: bold;
    margin-bottom: 7px;
}

.strong-badge {
    background: #16a34a;
}

.first-badge {
    background: #d97706;
}

.league {
    color: #60a5fa;
    font-size: 13px;
    font-weight: bold;
    margin-bottom: 5px;
}

.teams {
    font-size: 18px;
    font-weight: bold;
}

.score-line {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 7px;
}

.score {
    font-size: 27px;
    font-weight: bold;
}

.minute {
    color: #cbd5e1;
    font-weight: bold;
}

.stats {
    display: grid;
    grid-template-columns:
        repeat(4, 1fr);
    gap: 6px;
    margin-top: 10px;
}

.stat {
    background: #081321;
    border-radius: 8px;
    padding: 7px;
    text-align: center;
    color: #94a3b8;
    font-size: 11px;
}

.stat b {
    display: block;
    color: white;
    font-size: 17px;
    margin-top: 3px;
}

.signal-row {
    display: flex;
    gap: 7px;
    margin-top: 9px;
    flex-wrap: wrap;
}

.signal {
    padding: 8px 10px;
    border-radius: 8px;
    background: #172554;
}

.signal.green {
    background: #14532d;
}

.signal.red {
    background: #7f1d1d;
}

.signal.orange {
    background: #713f12;
}

.expected {
    margin-top: 8px;
    padding: 8px;
    border-radius: 8px;
    background: #172554;
    font-size: 13px;
}

.reasons {
    margin-top: 8px;
    color: #cbd5e1;
    font-size: 12px;
    line-height: 1.5;
}

.empty {
    text-align: center;
    background: #101e32;
    border-radius: 13px;
    padding: 40px 15px;
    color: #94a3b8;
}

.refresh {
    color: #64748b;
    font-size: 11px;
    margin-top: 10px;
}

</style>

</head>

<body>

<div class="header">

<h1>⚽ GOL SİNYAL MERKEZİ</h1>

<p>
Yüksek gol ihtimali taşıyan maçlar otomatik olarak üste alınır
</p>

</div>

<div class="container">

<div class="status">

<div class="status-grid">

<div class="status-item">
<div class="status-label">SİSTEM</div>
<div id="system" class="status-value">...</div>
</div>

<div class="status-item">
<div class="status-label">SCANNER</div>
<div id="scanner" class="status-value">...</div>
</div>

<div class="status-item">
<div class="status-label">CANLI</div>
<div id="live" class="status-value">0</div>
</div>

<div class="status-item">
<div class="status-label">ANALİZ</div>
<div id="analyzed" class="status-value">0</div>
</div>

<div class="status-item">
<div class="status-label">SON TARAMA</div>
<div id="lastscan" class="status-value">-</div>
</div>

<div class="status-item">
<div class="status-label">SONRAKİ TARAMA</div>
<div id="nextscan" class="status-value">-</div>
</div>

</div>

<div class="refresh">
Panel: 10 saniye · API taraması: 30 saniye
</div>

</div>

<div class="section-title">
🔥 Yüksek Gol Sinyalli Maçlar
</div>

<div id="matches"></div>

</div>


<script>

let notifiedMatches = {};

try {
    notifiedMatches =
        JSON.parse(
            localStorage.getItem(
                "gol_notified_matches"
            ) || "{}"
        );
} catch (e) {
    notifiedMatches = {};
}


function escapeHtml(value) {

    const div =
        document.createElement("div");

    div.textContent =
        String(value ?? "");

    return div.innerHTML;
}


function requestNotificationPermission() {

    if (
        "Notification" in window &&
        Notification.permission === "default"
    ) {
        Notification.requestPermission();
    }
}


function notifyStrongMatch(match) {

    if (
        !("Notification" in window)
    ) {
        return;
    }

    if (
        Notification.permission !== "granted"
    ) {
        return;
    }

    const key =
        String(match.fixture_id);

    if (notifiedMatches[key]) {
        return;
    }

    if (
        Number(match.priority || 0) <
        65
    ) {
        return;
    }

    const title =
        match.very_strong
        ? "🚨 ÇOK GÜÇLÜ GOL SİNYALİ"
        : "🔥 YÜKSEK GOL SİNYALİ";

    const body =
        match.home +
        " - " +
        match.away +
        " | " +
        match.minute +
        "' | " +
        match.score_home +
        "-" +
        match.score_away +
        " | Sinyal %" +
        match.priority;

    try {

        new Notification(
            title,
            {
                body: body,
                tag: key
            }
        );

    } catch (e) {
        console.log(
            "Bildirim oluşturulamadı:",
            e
        );
    }

    notifiedMatches[key] =
        Date.now();

    try {
        localStorage.setItem(
            "gol_notified_matches",
            JSON.stringify(
                notifiedMatches
            )
        );
    } catch (e) {
        console.log(e);
    }
}


function createMatch(match) {

    const priority =
        Number(match.priority || 0);

    let className = "match";

    let badge = "";

    if (
        match.very_strong
    ) {

        className += " very-strong";

        badge =
            '<div class="top-badge">' +
            '🚨 ÇOK GÜÇLÜ SİNYAL' +
            '</div>';

    } else if (
        priority >= 65
    ) {

        className += " strong";

        badge =
            '<div class="top-badge strong-badge">' +
            '🔥 GÜÇLÜ GOL SİNYALİ' +
            '</div>';

    } else if (
        match.first_half_signal >= 65
    ) {

        className += " first-strong";

        badge =
            '<div class="top-badge first-badge">' +
            '⚡ İLK YARI GOL SİNYALİ' +
            '</div>';
    }


    let reasons = "";

    if (
        match.signal_reasons &&
        match.signal_reasons.length
    ) {

        reasons +=
            "<b>Normal sinyal nedenleri:</b>";

        match.signal_reasons.forEach(
            function(reason) {

                reasons +=
                    "<div>• " +
                    escapeHtml(reason) +
                    "</div>";
            }
        );
    }


    if (
        match.first_half_reasons &&
        match.first_half_reasons.length
    ) {

        reasons +=
            "<br><b>İlk yarı nedenleri:</b>";

        match.first_half_reasons.forEach(
            function(reason) {

                reasons +=
                    "<div>• " +
                    escapeHtml(reason) +
                    "</div>";
            }
        );
    }


    return `

    <div class="${className}">

        ${badge}

        <div class="league">
            🏆 ${escapeHtml(match.league)}
        </div>

        <div class="teams">
            ${escapeHtml(match.home)}
            -
            ${escapeHtml(match.away)}
        </div>

        <div class="score-line">

            <div class="score">
                ${match.score_home}
                -
                ${match.score_away}
            </div>

            <div class="minute">
                ⏱ ${match.minute}'
            </div>

        </div>

        <div class="stats">

            <div class="stat">
                ŞUT
                <b>${match.shots}</b>
            </div>

            <div class="stat">
                İSABET
                <b>${match.target}</b>
            </div>

            <div class="stat">
                KORNER
                <b>${match.corners}</b>
            </div>

            <div class="stat">
                CEZA SAHASI
                <b>${match.inside}</b>
            </div>

        </div>

        <div class="signal-row">

            <div class="signal green">
                ⚽ Gol Sinyali
                <b>%${match.signal}</b>
            </div>

            <div class="signal orange">
                ⚡ İlk Yarı
                <b>%${match.first_half_signal}</b>
            </div>

            <div class="signal red">
                🔥 Öncelik
                <b>%${priority}</b>
            </div>

        </div>

        <div class="expected">
            ⚽ <b>Gol beklenen taraf:</b>
            ${escapeHtml(match.expected_team)}
        </div>

        ${
            reasons
            ? `
            <div class="reasons">
                ${reasons}
            </div>
            `
            : ""
        }

    </div>

    `;
}


async function loadStatus() {

    try {

        const response =
            await fetch(
                "/api/status",
                {
                    cache: "no-store"
                }
            );

        const data =
            await response.json();


        const system =
            document.getElementById(
                "system"
            );

        const scanner =
            document.getElementById(
                "scanner"
            );


        system.textContent =
            data.error
            ? "🔴 HATA"
            : "🟢 Çalışıyor";

        system.className =
            data.error
            ? "status-value unhealthy"
            : "status-value healthy";


        if (
            data.scanner_healthy
        ) {

            scanner.textContent =
                data.scan_in_progress
                ? "🟢 Tarıyor"
                : "🟢 Hazır";

            scanner.className =
                "status-value healthy";

        } else {

            scanner.textContent =
                "🔴 Kontrol et";

            scanner.className =
                "status-value unhealthy";
        }


        document.getElementById(
            "live"
        ).textContent =
            data.live_match_count || 0;


        document.getElementById(
            "analyzed"
        ).textContent =
            data.analyzed_match_count || 0;


        document.getElementById(
            "lastscan"
        ).textContent =
            data.last_scan_finished || "-";


        if (
            data.next_scan_in !== null &&
            data.next_scan_in !== undefined
        ) {

            document.getElementById(
                "nextscan"
            ).textContent =
                data.next_scan_in + " sn";

        } else {

            document.getElementById(
                "nextscan"
            ).textContent =
                "-";
        }

    } catch (error) {

        document.getElementById(
            "system"
        ).textContent =
            "🔴 Bağlantı yok";

        document.getElementById(
            "scanner"
        ).textContent =
            "🔴 Bağlantı yok";
    }
}


async function loadMatches() {

    try {

        const response =
            await fetch(
                "/api/matches",
                {
                    cache: "no-store"
                }
            );

        const data =
            await response.json();


        const container =
            document.getElementById(
                "matches"
            );


        if (
            !data.matches ||
            data.matches.length === 0
        ) {

            container.innerHTML = `

                <div class="empty">

                    ⚽

                    <br><br>

                    Şu anda analiz edilen
                    canlı maç yok.

                    <br><br>

                    Scanner otomatik olarak
                    taramaya devam ediyor.

                </div>

            `;

            return;
        }


        /*
         * En yüksek sinyalli maçları
         * tekrar garanti etmek için
         * browser tarafında da sıralıyoruz.
         */

        const matches =
            [...data.matches].sort(
                function(a, b) {

                    return (
                        Number(b.priority || 0) -
                        Number(a.priority || 0)
                    );
                }
            );


        container.innerHTML =
            matches
            .map(createMatch)
            .join("");


        /*
         * İlk kez yüksek sinyal alan
         * maç için bildirim.
         */

        matches.forEach(
            function(match) {

                if (
                    Number(
                        match.priority || 0
                    ) >= 65
                ) {
                    notifyStrongMatch(
                        match
                    );
                }
            }
        );

    } catch (error) {

        console.log(
            "Maçlar alınamadı:",
            error
        );
    }
}


async function refreshAll() {

    await loadStatus();

    await loadMatches();

}


requestNotificationPermission();

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

    matches = sort_matches(
        matches
    )

    return jsonify({
        "matches": matches,

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

    now = time.time()

    last_scan =
    state.get(
        "last_scan_timestamp"
    )

    if last_scan is None:
        seconds_since_scan = None
        scanner_healthy = False
        watchdog_message = (
            "Henüz başarılı tarama yok."
        )

    else:
        seconds_since_scan = max(
            0,
            int(
                now -
                float(last_scan)
            )
        )

        scanner_healthy = (
            seconds_since_scan <=
            WATCHDOG_LIMIT
        )

        if scanner_healthy:
            watchdog_message = (
                "Scanner sağlıklı."
            )
        else:
            watchdog_message = (
                "Son başarılı tarama "
                f"{seconds_since_scan} saniye önce."
            )


    next_scan_timestamp = (
        state.get(
            "next_scan_timestamp"
        )
    )

    if next_scan_timestamp is None:
        next_scan_in = None

    else:
        next_scan_in = max(
            0,
            int(
                float(next_scan_timestamp) -
                now
            )
        )


    return jsonify({
        "running":
            state["running"],

        "api_key":
            state["api_key"],

        "scanner":
            state["scanner"],

        "scan_in_progress":
            state["scan_in_progress"],

        "scanner_healthy":
            scanner_healthy,

        "watchdog_message":
            watchdog_message,

        "seconds_since_scan":
            seconds_since_scan,

        "next_scan_in":
            next_scan_in,

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
# SCANNER BAŞLAT
# ============================================================

start_scanner()


# ============================================================
# LOCAL ÇALIŞTIRMA
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
    print("🌐 FLASK BAŞLIYOR")
    print("=" * 60)

    print(
        f"PORT: {port}"
    )

    print("=" * 60)
    print("")

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
