import os
import time
import threading
import sqlite3
import requests
import fcntl

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

# Render'da tüm worker/process'lerin ortak görebileceği dosya
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
# ORTAK DURUM VERİTABANI
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

    conn = db_connect()

    try:

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
                "error": None

            }

        result = dict(row)

        result["running"] = bool(
            result["running"]
        )

        result["scanner"] = bool(
            result["scanner"]
        )

        result["scan_in_progress"] = bool(
            result["scan_in_progress"]
        )

        result["api_key"] = bool(
            result["api_key"]
        )

        return result

    finally:

        conn.close()


def save_matches(matches):

    conn = db_connect()

    try:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS matches_data (

                id INTEGER PRIMARY KEY,

                data TEXT NOT NULL

            )
        """)

        import json

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

    import json

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

            return json.loads(
                row["data"]
            )

        except Exception:

            return []

    finally:

        conn.close()


# ============================================================
# VERİTABANINI BAŞLAT
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

    team_stats = get_team_stats(
        stats
    )

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

    total_shots = (
        current_stats["shots"]
    )

    total_target = (
        current_stats["target"]
    )

    total_corners = (
        current_stats["corners"]
    )

    total_inside = (
        current_stats["inside"]
    )

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

    return min(
        score,
        100
    ), reasons


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

    home_stats, away_stats = (
        team_stats
    )

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
            signal >= SIGNAL_LIMIT,

        "strong_first_half":
            first_signal >= FIRST_HALF_LIMIT

    }


# ============================================================
# SCANNER PROCESS LOCK
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

        print(
            "🔑 API_KEY: OK"
        )

    else:

        print(
            "❌ API_KEY: YOK"
        )

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

            live_count = len(
                matches
            )

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

            # ÖNEMLİ:
            # Sonuçları ortak SQLite'a yazıyoruz.
            save_matches(
                analyzed
            )

            finish_time = time.strftime(
                "%H:%M:%S"
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

                updated=finish_time,

                last_scan_finished=finish_time,

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

                last_scan_finished=time.strftime(
                    "%H:%M:%S"
                )

            )

        time.sleep(
            CHECK_SECONDS
        )


# ============================================================
# SCANNER'I BAŞLAT
# ============================================================

scanner_thread = None


def start_scanner():

    global scanner_thread

    # Aynı process içinde zaten çalışıyorsa tekrar başlatma
    if (
        scanner_thread is not None
        and
        scanner_thread.is_alive()
    ):

        return

    # Birden fazla Gunicorn worker'ı varsa
    # yalnızca bir tanesinin scanner olmasına izin ver.
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

    print("")


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
    font-family: Arial, sans-serif;
    background: #0f172a;
    color: white;
}

.header {
    background: #111827;
    padding: 22px;
    text-align: center;
    border-bottom: 1px solid #334155;
}

.header h1 {
    margin: 0;
    font-size: 28px;
}

.header p {
    color: #94a3b8;
    margin-bottom: 0;
}

.container {
    max-width: 1200px;
    margin: auto;
    padding: 20px;
}

.status {
    background: #1e293b;
    padding: 18px;
    border-radius: 12px;
    margin-bottom: 20px;
    line-height: 1.6;
}

.status-title {
    font-size: 18px;
    margin-bottom: 10px;
}

.match {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 15px;
    padding: 20px;
    margin-bottom: 15px;
}

.match.signal {
    border: 2px solid #22c55e;
    box-shadow: 0 0 15px rgba(34,197,94,0.20);
}

.match.first-signal {
    border: 2px solid #f59e0b;
}

.teams {
    font-size: 22px;
    font-weight: bold;
    margin-bottom: 8px;
}

.score {
    font-size: 32px;
    font-weight: bold;
    margin: 5px 0;
}

.minute {
    color: #94a3b8;
    margin-bottom: 10px;
}

.league {
    color: #60a5fa;
    margin-bottom: 10px;
    font-weight: bold;
}

.stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 10px;
    margin-top: 15px;
}

.stat {
    background: #0f172a;
    padding: 12px;
    border-radius: 10px;
    text-align: center;
    color: #cbd5e1;
}

.stat b {
    display: block;
    font-size: 22px;
    margin-top: 5px;
    color: white;
}

.signal-box {
    margin-top: 15px;
    padding: 15px;
    background: #14532d;
    border-radius: 10px;
}

.first-box {
    margin-top: 10px;
    padding: 15px;
    background: #713f12;
    border-radius: 10px;
}

.expected-box {
    margin-top: 15px;
    padding: 14px;
    background: #172554;
    border-radius: 10px;
}

.normal-score {
    margin-top: 15px;
    padding: 12px;
    background: #0f172a;
    border-radius: 10px;
}

.empty {
    text-align: center;
    padding: 60px 20px;
    color: #94a3b8;
    background: #1e293b;
    border-radius: 15px;
}

.reason {
    margin-top: 5px;
    color: #d1d5db;
}

.badge {
    display: inline-block;
    padding: 5px 10px;
    border-radius: 20px;
    background: #334155;
    font-size: 13px;
    margin-left: 5px;
}

.refresh {
    color: #94a3b8;
    font-size: 13px;
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

<div class="status-title">
<b>📡 Sistem Durumu</b>
</div>

<b>Durum:</b>
<span id="status">Bağlanıyor...</span>

<br>

<b>Tarama motoru:</b>
<span id="scanner">-</span>

<br>

<b>Canlı maç:</b>
<span id="livecount">0</span>

<br>

<b>Uygun liglerde canlı maç:</b>
<span id="eligiblecount">0</span>

<br>

<b>Analiz edilen maç:</b>
<span id="analyzedcount">0</span>

<br>

<b>Son güncelleme:</b>
<span id="updated">-</span>

<br>

<b>Son tarama başlangıcı:</b>
<span id="scanstart">-</span>

<br>

<b>Son tarama bitişi:</b>
<span id="scanfinish">-</span>

<br>

<b>Normal gol limiti:</b>
%65

<span class="badge">
65 ve üzeri güçlü sinyal
</span>

<br>

<b>İlk yarı limiti:</b>
%65

<span class="badge">
15-45 dakika
</span>

<br><br>

<span class="refresh">
Panel her 10 saniyede yenilenir.
API her 30 saniyede taranır.
</span>

</div>

<div id="matches"></div>

</div>

<script>

function escapeHtml(text) {

    const div = document.createElement("div");

    div.textContent = text;

    return div.innerHTML;
}


function createMatch(match) {

    let html = "";

    const strong = match.strong_signal;

    const firstStrong =
        match.strong_first_half;

    let className = "match";


    if (strong) {

        className += " signal";

    } else if (firstStrong) {

        className += " first-signal";

    }


    html += `

    <div class="${className}">

        <div class="league">
            🏆 ${escapeHtml(match.league)}
        </div>

        <div class="teams">
            ${escapeHtml(match.home)}
            -
            ${escapeHtml(match.away)}
        </div>

        <div class="score">
            ${match.score_home}
            -
            ${match.score_away}
        </div>

        <div class="minute">
            ⏱ ${match.minute}'
        </div>

        <div class="stats">

            <div class="stat">
                Şut
                <b>${match.shots}</b>
            </div>

            <div class="stat">
                İsabetli Şut
                <b>${match.target}</b>
            </div>

            <div class="stat">
                Korner
                <b>${match.corners}</b>
            </div>

            <div class="stat">
                Ceza Sahası
                <b>${match.inside}</b>
            </div>

        </div>
    `;


    if (match.strong_signal) {

        html += `

        <div class="signal-box">

            🔥

            <b>
                GOL SİNYALİ:
                %${match.signal}
            </b>

            <br><br>

            ${match.signal_reasons
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

    } else {

        html += `

        <div class="normal-score">

            📊 Gol sinyali:

            <b>%${match.signal}</b>

        </div>

        `;

    }


    html += `

    <div class="expected-box">

        ⚽ <b>Gol beklenen taraf:</b>

        <br><br>

        ${escapeHtml(match.expected_team)}

    </div>

    `;


    if (match.first_half_signal >= 65) {

        html += `

        <div class="first-box">

            ⚡

            <b>
                İLK YARI GOL SİNYALİ:
                %${match.first_half_signal}
            </b>

            <br><br>

            ⚽ <b>Gol beklenen taraf:</b>

            ${escapeHtml(match.expected_team)}

            <br><br>

            ${match.first_half_reasons
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


    html += "</div>";

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
                ? "🟢 Tarama yapıyor"
                : "🟢 Hazır"
              )

            : "🔴 Durdu";


        document.getElementById(
            "livecount"
        ).textContent =
            data.live_match_count || 0;


        document.getElementById(
            "eligiblecount"
        ).textContent =
            data.eligible_match_count || 0;


        document.getElementById(
            "analyzedcount"
        ).textContent =
            data.analyzed_match_count || 0;


        document.getElementById(
            "updated"
        ).textContent =
            data.updated || "-";


        document.getElementById(
            "scanstart"
        ).textContent =
            data.last_scan_started || "-";


        document.getElementById(
            "scanfinish"
        ).textContent =
            data.last_scan_finished || "-";

    }

    catch (error) {

        document.getElementById(
            "status"
        ).textContent =
            "🔴 Sunucu bağlantı hatası";

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
# SCANNER'I BAŞLAT
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
