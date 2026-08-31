import os
import time
import json
import sqlite3
import threading
import fcntl
from datetime import datetime

import requests
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

FIRST_HALF_LIMIT = 65

WATCHDOG_LIMIT = CHECK_SECONDS * 3 + 30

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
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
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

                last_success_timestamp REAL DEFAULT 0,

                error TEXT
            )
        """)

        # Eski database varsa yeni kolonları güvenli şekilde ekle
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(system_state)"
            ).fetchall()
        }

        if "last_success_timestamp" not in columns:
            conn.execute("""
                ALTER TABLE system_state
                ADD COLUMN last_success_timestamp REAL DEFAULT 0
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
                last_success_timestamp,
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
                0,
                NULL
            )
        """, (
            1 if API_KEY else 0,
            len(ALLOWED_LEAGUES)
        ))

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
        "last_success_timestamp",
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
                "last_success_timestamp": 0,
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


# ============================================================
# MAÇ VERİTABANI
# ============================================================

def save_matches(matches):
    conn = db_connect()

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS matches_data (
                id INTEGER PRIMARY KEY,
                data TEXT NOT NULL
            )
        """)

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
# BAŞLANGIÇ
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

            print(response.text[:500])

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
# NORMAL GOL SİNYALİ
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

    total_goals = (
        goals_home +
        goals_away
    )

    if (
        total_goals == 0
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
# İLK YARI
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

    # Daha yüksek olan sinyal sıralamada kullanılacak
    priority = max(
        signal,
        first_signal
    )

    very_strong = (
        signal >= VERY_STRONG_LIMIT
        or
        first_signal >= VERY_STRONG_LIMIT
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

        "expected_team":
            normal_expected_team,

        "first_half_signal":
            int(first_signal),

        "first_half_reasons":
            first_reasons,

        "strong_signal":
            signal >= SIGNAL_LIMIT,

        "strong_first_half":
            first_signal >= FIRST_HALF_LIMIT,

        "very_strong":
            very_strong,

        "priority":
            int(priority)
    }


# ============================================================
# SIRALAMA
# ============================================================

def sort_matches(matches):
    return sorted(
        matches,
        key=lambda x: (
            x.get("very_strong", False),
            x.get("strong_signal", False)
            or x.get("strong_first_half", False),
            x.get("priority", 0),
            x.get("signal", 0),
            x.get("minute", 0)
        ),
        reverse=True
    )


# ============================================================
# LOCK
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

def scanner_loop():
    print("")
    print("=" * 60)
    print("🚀 ARKA PLAN MAÇ TARAMA MOTORU BAŞLADI")
    print("=" * 60)

    if API_KEY:
        print("🔑 API_KEY: OK")
    else:
        print("❌ API_KEY: YOK")

    while True:
        cycle_start = time.time()

        start_time = time.strftime(
            "%H:%M:%S"
        )

        update_state(
            scanner=1,
            scan_in_progress=1,
            last_scan_started=start_time,
            error=None
        )

        try:
            print("")
            print("=" * 60)
            print("📡 CANLI MAÇLAR TARAMASI BAŞLADI")
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

                except Exception as e:
                    print(
                        "❌ Maç analiz hatası:",
                        repr(e)
                    )

            analyzed = sort_matches(
                analyzed
            )

            save_matches(
                analyzed
            )

            finish_time = time.strftime(
                "%H:%M:%S"
            )

            now_timestamp = time.time()

            update_state(
                scanner=1,
                scan_in_progress=0,

                analyzed_match_count=
                    len(analyzed),

                match_count=
                    len(analyzed),

                updated=
                    finish_time,

                last_scan_finished=
                    finish_time,

                last_success_timestamp=
                    now_timestamp,

                error=None
            )

            print("")
            print(
                f"📊 Analiz edilen maç: "
                f"{len(analyzed)}"
            )

            print(
                f"⏱ Tarama süresi: "
                f"{time.time() - cycle_start:.1f} saniye"
            )

            print(
                f"😴 {CHECK_SECONDS} saniye sonra "
                f"yeni tarama başlayacak."
            )

        except Exception as e:
            print("")
            print("=" * 60)
            print("❌ TARAMA MOTORU HATASI")
            print("=" * 60)

            print(
                "HATA:",
                repr(e)
            )

            print("=" * 60)

            update_state(
                scanner=1,
                scan_in_progress=0,
                error=str(e),
                last_scan_finished=
                    time.strftime("%H:%M:%S")
            )

        time.sleep(
            CHECK_SECONDS
        )


# ============================================================
# SCANNER BAŞLATMA
# ============================================================

scanner_thread = None


def start_scanner():
    global scanner_thread

    if (
        scanner_thread is not None
        and
        scanner_thread.is_alive()
    ):
        return

    lock = acquire_scanner_lock()

    if lock is None:
        print(
            "ℹ️ Bu Gunicorn worker scanner lock alamadı."
        )
        return

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

    print(
        "✅ Scanner thread başlatıldı."
    )


# ============================================================
# WATCHDOG
# ============================================================

def watchdog_status(state):
    last_success = (
        state.get(
            "last_success_timestamp"
        )
        or 0
    )

    scan_in_progress = bool(
        state.get(
            "scan_in_progress"
        )
    )

    if last_success <= 0:
        return {
            "scanner_healthy": False,
            "seconds_since_scan": None,
            "next_scan_in": None,
            "watchdog_message":
                "Henüz başarılı tarama yok."
        }

    seconds_since = max(
        0,
        int(
            time.time() -
            last_success
        )
    )

    if scan_in_progress:
        healthy = (
            seconds_since <=
            WATCHDOG_LIMIT
        )

        message = (
            "Tarama devam ediyor."
        )

    else:
        healthy = (
            seconds_since <=
            WATCHDOG_LIMIT
        )

        if healthy:
            message = (
                "Scanner sağlıklı çalışıyor."
            )
        else:
            message = (
                "Scanner güncel değil."
            )

    next_scan_in = max(
        0,
        CHECK_SECONDS -
        (
            seconds_since %
            CHECK_SECONDS
        )
    )

    return {
        "scanner_healthy":
            healthy,

        "seconds_since_scan":
            seconds_since,

        "next_scan_in":
            next_scan_in,

        "watchdog_message":
            message
    }


# ============================================================
# WEB PANEL
# ============================================================

HTML = r"""
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
    font-family: Arial, sans-serif;
    background: #07111f;
    color: white;
}

.header {
    background: #0b1628;
    padding: 18px 15px;
    text-align: center;
    border-bottom: 1px solid #263852;
    position: sticky;
    top: 0;
    z-index: 20;
}

.header h1 {
    margin: 0;
    font-size: 25px;
}

.header p {
    color: #8fa3bd;
    margin: 7px 0 0;
    font-size: 13px;
}

.container {
    max-width: 1100px;
    margin: auto;
    padding: 14px;
}

.status {
    background: #101d30;
    padding: 14px;
    border-radius: 12px;
    margin-bottom: 14px;
    border: 1px solid #22334d;
}

.status-grid {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(130px, 1fr));
    gap: 8px;
}

.status-item {
    background: #0a1525;
    padding: 9px;
    border-radius: 8px;
}

.status-label {
    color: #8296af;
    font-size: 11px;
    display: block;
}

.status-value {
    font-size: 16px;
    font-weight: bold;
    margin-top: 3px;
}

.matches-title {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 12px 0;
}

.matches-title h2 {
    margin: 0;
    font-size: 18px;
}

.refresh {
    color: #71859f;
    font-size: 11px;
}

.match {
    background: #101d30;
    border: 1px solid #243750;
    border-radius: 12px;
    padding: 13px;
    margin-bottom: 9px;
}

.match.signal {
    border: 2px solid #22c55e;
    box-shadow: 0 0 16px
        rgba(34,197,94,0.16);
}

.match.very-strong {
    border: 2px solid #ef4444;
    box-shadow: 0 0 20px
        rgba(239,68,68,0.18);
}

.match.first-signal {
    border: 2px solid #f59e0b;
}

.top-badge {
    display: inline-block;
    padding: 4px 8px;
    border-radius: 15px;
    background: #14532d;
    color: #86efac;
    font-size: 11px;
    font-weight: bold;
    margin-bottom: 6px;
}

.very-badge {
    background: #7f1d1d;
    color: #fecaca;
}

.teams-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
}

.teams {
    font-size: 17px;
    font-weight: bold;
}

.score-area {
    text-align: right;
    min-width: 65px;
}

.score {
    font-size: 24px;
    font-weight: bold;
}

.minute {
    color: #94a3b8;
    font-size: 12px;
}

.league {
    color: #60a5fa;
    font-size: 12px;
    font-weight: bold;
    margin-bottom: 5px;
}

.stats {
    display: grid;
    grid-template-columns:
        repeat(4, 1fr);
    gap: 5px;
    margin-top: 10px;
}

.stat {
    background: #091423;
    padding: 7px 3px;
    border-radius: 7px;
    text-align: center;
    color: #8fa3bd;
    font-size: 10px;
}

.stat b {
    display: block;
    font-size: 17px;
    margin-top: 2px;
    color: white;
}

.signal-row {
    display: flex;
    gap: 7px;
    margin-top: 9px;
}

.signal-box {
    flex: 1;
    padding: 9px;
    background: #123c25;
    border-radius: 8px;
}

.first-box {
    flex: 1;
    padding: 9px;
    background: #54350a;
    border-radius: 8px;
}

.signal-number {
    font-size: 22px;
    font-weight: bold;
}

.reason {
    color: #cbd5e1;
    font-size: 11px;
    margin-top: 3px;
}

.expected-box {
    margin-top: 8px;
    padding: 8px;
    background: #111f45;
    border-radius: 8px;
    font-size: 12px;
}

.normal-score {
    margin-top: 8px;
    padding: 7px;
    background: #091423;
    border-radius: 7px;
    font-size: 12px;
    color: #94a3b8;
}

.empty {
    text-align: center;
    padding: 45px 15px;
    color: #94a3b8;
    background: #101d30;
    border-radius: 12px;
}

.ok {
    color: #4ade80;
}

.bad {
    color: #f87171;
}

.warning {
    color: #fbbf24;
}

@media (max-width: 600px) {

    .container {
        padding: 8px;
    }

    .teams {
        font-size: 14px;
    }

    .score {
        font-size: 20px;
    }

    .stats {
        gap: 3px;
    }

    .signal-row {
        display: block;
    }

    .signal-box,
    .first-box {
        margin-bottom: 5px;
    }
}

</style>

</head>

<body>

<div class="header">

<h1>⚽ GOL SİNYAL MERKEZİ</h1>

<p>
Canlı maçlar otomatik analiz ediliyor
</p>

</div>

<div class="container">

<div class="status">

<div class="status-grid">

<div class="status-item">
<span class="status-label">SİSTEM</span>
<div id="status"
class="status-value">...</div>
</div>

<div class="status-item">
<span class="status-label">SCANNER</span>
<div id="scanner"
class="status-value">...</div>
</div>

<div class="status-item">
<span class="status-label">CANLI</span>
<div id="livecount"
class="status-value">0</div>
</div>

<div class="status-item">
<span class="status-label">ANALİZ</span>
<div id="analyzedcount"
class="status-value">0</div>
</div>

<div class="status-item">
<span class="status-label">SON TARAMA</span>
<div id="updated"
class="status-value">-</div>
</div>

<div class="status-item">
<span class="status-label">SONRAKİ</span>
<div id="nextscan"
class="status-value">-</div>
</div>

</div>

<div style="
margin-top:10px;
font-size:11px;
color:#71859f;
">

<span id="watchdog">
Watchdog kontrol ediliyor...
</span>

</div>

</div>

<div class="matches-title">

<h2>🔥 Öncelikli Maçlar</h2>

<span class="refresh">
10 sn otomatik yenileme
</span>

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
} catch(e) {
    notifiedMatches = {};
}


function escapeHtml(text) {

    const div =
        document.createElement("div");

    div.textContent =
        text == null ? "" : text;

    return div.innerHTML;
}


function notifyStrongMatch(match) {

    if (
        !match.strong_signal
        &&
        !match.strong_first_half
    ) {
        return;
    }

    const key =
        String(match.fixture_id)
        + "-"
        + String(
            Math.max(
                match.signal || 0,
                match.first_half_signal || 0
            )
        );

    if (notifiedMatches[key]) {
        return;
    }

    notifiedMatches[key] = true;

    try {
        localStorage.setItem(
            "gol_notified_matches",
            JSON.stringify(
                notifiedMatches
            )
        );
    } catch(e) {}

    if (
        "Notification" in window
        &&
        Notification.permission === "granted"
    ) {

        const score =
            match.score_home
            + " - "
            + match.score_away;

        const best =
            Math.max(
                match.signal || 0,
                match.first_half_signal || 0
            );

        new Notification(
            "🔥 Yüksek Gol Sinyali",
            {
                body:
                    match.home
                    + " - "
                    + match.away
                    + "\n"
                    + score
                    + " | "
                    + match.minute
                    + "' | Sinyal: %"
                    + best
            }
        );
    }
}


function createMatch(match) {

    let html = "";

    const strong =
        match.strong_signal;

    const firstStrong =
        match.strong_first_half;

    const veryStrong =
        match.very_strong;

    let className =
        "match";

    if (veryStrong) {
        className +=
            " very-strong";
    }
    else if (strong) {
        className +=
            " signal";
    }
    else if (firstStrong) {
        className +=
            " first-signal";
    }


    if (veryStrong) {

        html += `
            <div class="top-badge very-badge">
                🚨 ÇOK GÜÇLÜ SİNYAL
            </div>
        `;

    }
    else if (strong || firstStrong) {

        html += `
            <div class="top-badge">
                🔥 YÜKSEK ÖNCELİKLİ MAÇ
            </div>
        `;

    }


    html += `

    <div class="league">
        🏆 ${escapeHtml(match.league)}
    </div>

    <div class="teams-row">

        <div class="teams">
            ${escapeHtml(match.home)}
            -
            ${escapeHtml(match.away)}
        </div>

        <div class="score-area">

            <div class="score">
                ${match.score_home}
                -
                ${match.score_away}
            </div>

            <div class="minute">
                ⏱ ${match.minute}'
            </div>

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

    `;


    if (match.signal >= 65) {

        html += `

        <div class="signal-row">

            <div class="signal-box">

                🔥 <b>GOL SİNYALİ</b>

                <div class="signal-number">
                    %${match.signal}
                </div>

                ${
                    (match.signal_reasons || [])
                    .map(
                        r =>
                        `<div class="reason">
                            • ${escapeHtml(r)}
                        </div>`
                    )
                    .join("")
                }

            </div>

        `;

    }
    else {

        html += `

        <div class="normal-score">

            📊 Gol sinyali:

            <b>%${match.signal}</b>

        </div>

        `;

    }


    if (match.first_half_signal >= 65) {

        html += `

            <div class="first-box">

                ⚡ <b>İLK YARI</b>

                <div class="signal-number">
                    %${match.first_half_signal}
                </div>

                ${
                    (match.first_half_reasons || [])
                    .map(
                        r =>
                        `<div class="reason">
                            • ${escapeHtml(r)}
                        </div>`
                    )
                    .join("")
                }

            </div>

        </div>

        `;

    }
    else if (match.signal >= 65) {

        html += `</div>`;

    }


    html += `

    <div class="expected-box">

        ⚽ <b>Gol beklenen taraf:</b>

        ${escapeHtml(
            match.expected_team
        )}

    </div>

    `;

    html += `</div>`;

    return html;
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


        const statusElement =
            document.getElementById(
                "status"
            );

        const scannerElement =
            document.getElementById(
                "scanner"
            );


        statusElement.textContent =
            data.error
            ? "🔴 Hata"
            : "🟢 Çalışıyor";

        statusElement.className =
            "status-value "
            + (
                data.error
                ? "bad"
                : "ok"
            );


        if (!data.scanner) {

            scannerElement.textContent =
                "🔴 Durdu";

            scannerElement.className =
                "status-value bad";

        }
        else if (
            data.scan_in_progress
        ) {

            scannerElement.textContent =
                "🟡 Tarıyor";

            scannerElement.className =
                "status-value warning";

        }
        else if (
            data.scanner_healthy
        ) {

            scannerElement.textContent =
                "🟢 Hazır";

            scannerElement.className =
                "status-value ok";

        }
        else {

            scannerElement.textContent =
                "🔴 Güncel değil";

            scannerElement.className =
                "status-value bad";
        }


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


        const next =
            data.next_scan_in;

        document.getElementById(
            "nextscan"
        ).textContent =
            next == null
            ? "-"
            : next + " sn";


        const watchdog =
            document.getElementById(
                "watchdog"
            );


        if (
            data.scanner_healthy
        ) {

            watchdog.textContent =
                "🟢 "
                + (
                    data.watchdog_message
                    || "Scanner sağlıklı."
                )
                + " | Son başarılı tarama: "
                + (
                    data.seconds_since_scan
                    ?? "-"
                )
                + " sn önce.";

            watchdog.className =
                "ok";

        }
        else {

            watchdog.textContent =
                "🔴 "
                + (
                    data.watchdog_message
                    || "Scanner güncel değil."
                );

            watchdog.className =
                "bad";
        }

    }

    catch (error) {

        document.getElementById(
            "status"
        ).textContent =
            "🔴 Sunucu bağlantı hatası";

        document.getElementById(
            "status"
        ).className =
            "status-value bad";
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
            !data.matches
            ||
            data.matches.length === 0
        ) {

            container.innerHTML = `

                <div class="empty">

                    ⚽

                    <br><br>

                    <b>
                        Şu anda analiz edilen
                        uygun canlı maç yok.
                    </b>

                    <br><br>

                    Sistem otomatik olarak
                    taramaya devam ediyor.

                </div>

            `;

            return;
        }


        data.matches.forEach(
            notifyStrongMatch
        );


        container.innerHTML =
            data.matches
            .map(createMatch)
            .join("");

    }

    catch (error) {

        console.log(
            "Maç verisi alınamadı:",
            error
        );

    }
}


async function enableNotifications() {

    if (
        !("Notification" in window)
    ) {
        return;
    }

    if (
        Notification.permission ===
        "default"
    ) {

        try {
            await Notification.requestPermission();
        }
        catch(e) {}
    }
}


async function refreshAll() {

    await loadStatus();

    await loadMatches();

}


enableNotifications();

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

    watchdog = watchdog_status(
        state
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
            watchdog["scanner_healthy"],

        "seconds_since_scan":
            watchdog["seconds_since_scan"],

        "next_scan_in":
            watchdog["next_scan_in"],

        "watchdog_message":
            watchdog["watchdog_message"],

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
