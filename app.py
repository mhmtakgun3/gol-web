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

# Her tarama arasındaki süre
CHECK_SECONDS = 30

# Normal güçlü sinyal
SIGNAL_LIMIT = 65

# İlk yarı güçlü sinyal
FIRST_HALF_LIMIT = 65

# Watchdog
# Bu süreden daha uzun başarılı tarama olmazsa scanner sağlıksız kabul edilir.
WATCHDOG_TIMEOUT = 90

# Veritabanı
DB_FILE = "/tmp/gol_scanner.db"

# Scanner lock
LOCK_FILE = "/tmp/gol_scanner.lock"


# ============================================================
# SCANNER LOCK HANDLE
# ============================================================

# ÖNEMLİ:
# Lock dosyasını global tutuyoruz.
# Yoksa Python garbage collector dosyayı kapatıp flock'u bırakabilir.
SCANNER_LOCK_HANDLE = None


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
# DB
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

                error TEXT

            )
        """)

        # Eski DB varsa yeni kolon eksik olabilir.
        columns = [
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(system_state)"
            ).fetchall()
        ]

        if "last_scan_timestamp" not in columns:

            conn.execute("""
                ALTER TABLE system_state
                ADD COLUMN last_scan_timestamp REAL
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
            "last_scan_timestamp",
            "error"
        }

        fields = []
        values = []

        for key, value in kwargs.items():

            if key not in allowed:
                continue

            fields.append(
                f"{key} = ?"
            )

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
                "last_scan_timestamp": None,
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


# ============================================================
# MAÇ VERİLERİ
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

            return json.loads(
                row["data"]
            )

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

        print(
            "❌ API_KEY bulunamadı."
        )

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

    return min(
        score,
        100
    ), reasons


# ============================================================
# İLK YARI SİNYALİ
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

    # ========================================================
    # ÖNCELİK SİSTEMİ
    # ========================================================

    # Normal sinyal ana puan.
    # İlk yarı sinyali de varsa güçlü şekilde dikkate alınır.

    priority = max(
        signal,
        first_signal
    )

    # Çok güçlü sinyal
    very_strong = (
        signal >= 80
        or
        first_signal >= 80
    )

    # Öncelik bonusları
    if very_strong:

        priority += 20

    elif signal >= SIGNAL_LIMIT:

        priority += 10

    elif first_signal >= FIRST_HALF_LIMIT:

        priority += 8

    priority = min(
        priority,
        120
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
            first_signal >= FIRST_HALF_LIMIT,

        "very_strong":
            very_strong,

        "priority":
            int(priority)

    }


# ============================================================
# SCANNER LOCK
# ============================================================

def acquire_scanner_lock():

    global SCANNER_LOCK_HANDLE

    if SCANNER_LOCK_HANDLE is not None:

        return SCANNER_LOCK_HANDLE

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

        # Global tutuluyor.
        SCANNER_LOCK_HANDLE = lock_file

        return lock_file

    except Exception as e:

        print(
            "ℹ️ Scanner lock alınamadı:",
            repr(e)
        )

        try:

            lock_file.close()

        except Exception:

            pass

        return None


# ============================================================
# WATCHDOG
# ============================================================

def watchdog_loop():

    print(
        "🐕 Watchdog başlatıldı."
    )

    while True:

        try:

            state = get_state()

            last_timestamp = (
                state.get(
                    "last_scan_timestamp"
                )
            )

            if last_timestamp:

                seconds_since = max(
                    0,
                    int(
                        time.time() -
                        float(last_timestamp)
                    )
                )

                if seconds_since > WATCHDOG_TIMEOUT:

                    # Tarama uzun süredir yok.
                    update_state(
                        scanner=0,
                        error=(
                            "Watchdog: "
                            "uzun süredir başarılı tarama yok."
                        )
                    )

                    print(
                        f"🐕 ⚠️ Scanner sağlıksız: "
                        f"{seconds_since} saniyedir "
                        f"başarılı tarama yok."
                    )

                else:

                    # Scanner hâlâ sağlıklı.
                    update_state(
                        scanner=1
                    )

            else:

                # İlk başarılı tarama henüz gerçekleşmedi.
                # Scanner thread çalışıyor olabilir.
                if state.get(
                    "scan_in_progress"
                ):

                    update_state(
                        scanner=1
                    )

                else:

                    update_state(
                        scanner=1
                    )

        except Exception as e:

            print(
                "🐕 Watchdog hatası:",
                repr(e)
            )

        time.sleep(10)


# ============================================================
# TARAMA MOTORU
# ============================================================

def scanner_loop():

    print("")
    print("=" * 60)
    print(
        "🚀 ARKA PLAN MAÇ TARAMA MOTORU BAŞLADI"
    )
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

        start_timestamp = time.time()

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
            print(
                "📡 CANLI MAÇLAR TARAMASI BAŞLADI"
            )
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

            # =================================================
            # ÖNEMLİ:
            # SADECE BAŞARILI TARAMADA MATCHES GÜNCELLENİR.
            # =================================================

            save_matches(
                analyzed
            )

            finish_timestamp = time.time()

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

                # WATCHDOG İÇİN GERÇEK ZAMAN
                last_scan_timestamp=finish_timestamp,

                error=None

            )

            print("")
            print(
                f"📊 Analiz edilen maç: "
                f"{len(analyzed)}"
            )

            print(
                f"⏱ Tarama süresi: "
                f"{finish_timestamp - start_timestamp:.1f}"
                f" saniye"
            )

            print(
                f"😴 {CHECK_SECONDS} saniye sonra "
                f"yeni tarama başlayacak."
            )

        except Exception as e:

            print("")
            print("=" * 60)
            print(
                "❌ TARAMA MOTORU HATASI"
            )
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
watchdog_thread = None


def start_scanner():

    global scanner_thread
    global watchdog_thread

    if (
        scanner_thread is not None
        and
        scanner_thread.is_alive()
    ):

        return

    lock = acquire_scanner_lock()

    if lock is None:

        print(
            "ℹ️ Bu Gunicorn worker "
            "scanner lock alamadı."
        )

        return

    print("")
    print("=" * 60)
    print(
        "🚀 SCANNER OTOMATİK OLARAK BAŞLATILIYOR"
    )
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

    # Watchdog
    if (
        watchdog_thread is None
        or
        not watchdog_thread.is_alive()
    ):

        watchdog_thread = threading.Thread(

            target=watchdog_loop,

            name="ScannerWatchdog",

            daemon=True

        )

        watchdog_thread.start()

        print(
            "🐕 Watchdog thread başlatıldı."
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

    font-family:
        Arial,
        sans-serif;

    background:
        #020617;

    color:
        white;
}


/* =========================================================
   HEADER
   ========================================================= */

.header {

    background:
        linear-gradient(
            135deg,
            #0f172a,
            #111827
        );

    padding:
        20px;

    text-align:
        center;

    border-bottom:
        1px solid #334155;

    position:
        sticky;

    top: 0;

    z-index: 20;

}

.header h1 {

    margin:
        0;

    font-size:
        25px;

}

.header p {

    color:
        #94a3b8;

    margin:
        7px 0 0;

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
        15px;

}


/* =========================================================
   STATUS
   ========================================================= */

.status {

    background:
        #111827;

    padding:
        16px;

    border-radius:
        14px;

    margin-bottom:
        15px;

    border:
        1px solid #1e293b;

}

.status-grid {

    display:
        grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                140px,
                1fr
            )
        );

    gap:
        8px;

}

.status-item {

    background:
        #0f172a;

    padding:
        10px;

    border-radius:
        9px;

}

.status-label {

    display:
        block;

    color:
        #64748b;

    font-size:
        11px;

    text-transform:
        uppercase;

}

.status-value {

    display:
        block;

    font-size:
        15px;

    margin-top:
        4px;

    font-weight:
        bold;

}


/* =========================================================
   WATCHDOG
   ========================================================= */

.watchdog {

    margin-top:
        12px;

    padding:
        10px 12px;

    border-radius:
        9px;

    background:
        #0f172a;

    color:
        #cbd5e1;

}

.watchdog.ok {

    border:
        1px solid #166534;

}

.watchdog.bad {

    border:
        1px solid #991b1b;

    background:
        #450a0a;

}


/* =========================================================
   BUTTON
   ========================================================= */

.notify-button {

    margin-top:
        10px;

    padding:
        9px 14px;

    border:
        none;

    border-radius:
        8px;

    background:
        #2563eb;

    color:
        white;

    cursor:
        pointer;

    font-weight:
        bold;

}

.notify-button:hover {

    background:
        #1d4ed8;

}


/* =========================================================
   SECTION
   ========================================================= */

.section-title {

    display:
        flex;

    justify-content:
        space-between;

    align-items:
        center;

    margin:
        18px 2px 10px;

}

.section-title h2 {

    font-size:
        17px;

    margin:
        0;

}

.section-title span {

    color:
        #64748b;

    font-size:
        12px;

}


/* =========================================================
   MATCH
   ========================================================= */

.match {

    background:
        #111827;

    border:
        1px solid #1e293b;

    border-radius:
        12px;

    padding:
        13px;

    margin-bottom:
        9px;

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


/* Güçlü */

.match.signal {

    border:
        1px solid #22c55e;

    background:
        linear-gradient(
            135deg,
            #0f2418,
            #111827
        );

}


/* Çok güçlü */

.match.very-strong {

    border:
        2px solid #22c55e;

    box-shadow:
        0 0 18px
        rgba(
            34,
            197,
            94,
            .20
        );

}


/* İlk yarı */

.match.first-signal {

    border:
        1px solid #f59e0b;

}


/* =========================================================
   MATCH TOP
   ========================================================= */

.match-top {

    display:
        flex;

    justify-content:
        space-between;

    gap:
        10px;

    align-items:
        center;

}

.league {

    color:
        #60a5fa;

    font-size:
        11px;

    font-weight:
        bold;

}

.minute {

    color:
        #94a3b8;

    font-size:
        12px;

}


/* =========================================================
   TEAMS
   ========================================================= */

.teams-row {

    display:
        grid;

    grid-template-columns:
        1fr
        auto
        1fr;

    gap:
        10px;

    align-items:
        center;

    margin-top:
        9px;

}

.team {

    font-size:
        16px;

    font-weight:
        bold;

}

.team.away {

    text-align:
        right;

}

.score {

    font-size:
        24px;

    font-weight:
        bold;

    white-space:
        nowrap;

}


/* =========================================================
   STATS
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
        10px;

}

.stat {

    background:
        #020617;

    padding:
        7px;

    border-radius:
        7px;

    text-align:
        center;

    color:
        #64748b;

    font-size:
        10px;

}

.stat b {

    display:
        block;

    color:
        white;

    font-size:
        16px;

    margin-top:
        2px;

}


/* =========================================================
   SIGNAL
   ========================================================= */

.signal-row {

    display:
        flex;

    flex-wrap:
        wrap;

    gap:
        7px;

    margin-top:
        10px;

}

.signal-badge {

    padding:
        6px 9px;

    border-radius:
        7px;

    background:
        #052e16;

    color:
        #86efac;

    font-weight:
        bold;

    font-size:
        12px;

}

.signal-badge.orange {

    background:
        #451a03;

    color:
        #fdba74;

}

.signal-badge.blue {

    background:
        #172554;

    color:
        #93c5fd;

}


/* =========================================================
   EXPECTED
   ========================================================= */

.expected {

    margin-top:
        8px;

    padding:
        8px;

    background:
        #0f172a;

    border-radius:
        7px;

    font-size:
        12px;

    color:
        #cbd5e1;

}


/* =========================================================
   REASONS
   ========================================================= */

.reasons {

    margin-top:
        8px;

    color:
        #94a3b8;

    font-size:
        11px;

    line-height:
        1.5;

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
        #64748b;

    background:
        #111827;

    border-radius:
        12px;

}


/* =========================================================
   MOBILE
   ========================================================= */

@media (max-width: 600px) {

    .container {

        padding:
            8px;

    }

    .header {

        padding:
            14px;

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
            20px;

    }

    .stats {

        gap:
            3px;

    }

    .stat {

        padding:
            6px 2px;

    }

    .stat b {

        font-size:
            14px;

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
        Canlı maçlar otomatik analiz ediliyor
    </p>

</div>


<div class="container">


    <!-- =====================================================
         STATUS
         ===================================================== -->

    <div class="status">

        <div class="status-grid">

            <div class="status-item">

                <span class="status-label">
                    Sistem
                </span>

                <span
                    class="status-value"
                    id="status">
                    Bağlanıyor...
                </span>

            </div>


            <div class="status-item">

                <span class="status-label">
                    Scanner
                </span>

                <span
                    class="status-value"
                    id="scanner">
                    -
                </span>

            </div>


            <div class="status-item">

                <span class="status-label">
                    Canlı
                </span>

                <span
                    class="status-value"
                    id="livecount">
                    0
                </span>

            </div>


            <div class="status-item">

                <span class="status-label">
                    Analiz
                </span>

                <span
                    class="status-value"
                    id="analyzedcount">
                    0
                </span>

            </div>


            <div class="status-item">

                <span class="status-label">
                    Son Tarama
                </span>

                <span
                    class="status-value"
                    id="scanfinish">
                    -
                </span>

            </div>


            <div class="status-item">

                <span class="status-label">
                    Sonraki Tarama
                </span>

                <span
                    class="status-value"
                    id="nextscan">
                    -
                </span>

            </div>

        </div>


        <div
            id="watchdog"
            class="watchdog">

            🐕 Watchdog:
            <span id="watchdogtext">
                Kontrol ediliyor...
            </span>

        </div>


        <button
            class="notify-button"
            onclick="enableNotifications()">

            🔔 Bildirimleri Aç

        </button>


        <div
            style="
                margin-top:8px;
                color:#64748b;
                font-size:11px;
            ">

            Normal güçlü sinyal:
            %65+

            &nbsp; | &nbsp;

            Çok güçlü:
            %80+

            &nbsp; | &nbsp;

            Panel:
            5 saniyede yenilenir

        </div>

    </div>


    <!-- =====================================================
         MAÇLAR
         ===================================================== -->

    <div class="section-title">

        <h2>
            🔥 Öncelikli Maçlar
        </h2>

        <span id="matchcount">
            0 maç
        </span>

    </div>


    <div id="matches">

        <div class="empty">

            ⚽

            <br><br>

            Maçlar yükleniyor...

        </div>

    </div>


</div>


<script>


// ============================================================
// BİLDİRİM
// ============================================================

async function enableNotifications() {

    if (!("Notification" in window)) {

        alert(
            "Tarayıcınız bildirimleri desteklemiyor."
        );

        return;

    }

    try {

        const permission =
            await Notification.requestPermission();

        if (
            permission === "granted"
        ) {

            alert(
                "🔔 Bildirimler aktif edildi."
            );

        }

    } catch (error) {

        console.log(
            "Bildirim izni alınamadı:",
            error
        );

    }

}


// ============================================================
// HTML ESCAPE
// ============================================================

function escapeHtml(text) {

    const div =
        document.createElement("div");

    div.textContent =
        text == null
        ? ""
        : String(text);

    return div.innerHTML;

}


// ============================================================
// YENİ SİNYAL BİLDİRİMİ
// ============================================================

function notifyStrongMatches(matches) {

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

    let notified =
        JSON.parse(
            localStorage.getItem(
                "gol_notified_matches"
            ) || "{}"
        );


    matches.forEach(match => {

        const strong =
            match.signal >= 65
            ||
            match.first_half_signal >= 65;

        if (!strong) {

            return;

        }


        const key =
            String(match.fixture_id);


        // Daha önce bu tarayıcıda
        // bildirim gönderilmişse tekrar gönderme.
        if (notified[key]) {

            return;

        }


        const score =
            Math.max(
                match.signal || 0,
                match.first_half_signal || 0
            );


        const title =
            score >= 80
            ? "🔥 ÇOK GÜÇLÜ GOL SİNYALİ"
            : "⚽ GÜÇLÜ GOL SİNYALİ";


        const body =
            match.home +
            " - " +
            match.away +
            " | " +
            match.minute +
            "' | " +
            "%" +
            score;


        try {

            new Notification(
                title,
                {
                    body:
                        body,

                    tag:
                        "gol-" +
                        key
                }
            );

            notified[key] =
                Date.now();

        } catch (error) {

            console.log(
                "Notification hatası:",
                error
            );

        }

    });


    // Çok büyümesini engelle
    const keys =
        Object.keys(
            notified
        );

    if (keys.length > 300) {

        const sorted =
            keys.sort(
                (a, b) =>
                    notified[a] -
                    notified[b]
            );

        sorted
            .slice(
                0,
                keys.length - 200
            )
            .forEach(
                key =>
                    delete notified[key]
            );

    }


    localStorage.setItem(
        "gol_notified_matches",
        JSON.stringify(notified)
    );

}


// ============================================================
// MAÇ HTML
// ============================================================

function createMatch(match) {

    let className =
        "match";


    if (match.very_strong) {

        className +=
            " very-strong";

    } else if (
        match.strong_signal
    ) {

        className +=
            " signal";

    } else if (
        match.strong_first_half
    ) {

        className +=
            " first-signal";

    }


    const maxSignal =
        Math.max(
            match.signal || 0,
            match.first_half_signal || 0
        );


    let signalBadge = "";


    if (
        match.very_strong
    ) {

        signalBadge += `

            <div class="signal-badge">

                🔥 ÇOK GÜÇLÜ
                %${maxSignal}

            </div>

        `;

    } else if (
        match.strong_signal
    ) {

        signalBadge += `

            <div class="signal-badge">

                ⚽ GOL SİNYALİ
                %${match.signal}

            </div>

        `;

    } else {

        signalBadge += `

            <div class="signal-badge blue">

                📊 Sinyal
                %${match.signal}

            </div>

        `;

    }


    if (
        match.first_half_signal >= 65
    ) {

        signalBadge += `

            <div class="signal-badge orange">

                ⚡ İLK YARI
                %${match.first_half_signal}

            </div>

        `;

    }


    let reasons = [];


    if (
        match.signal_reasons
        &&
        match.signal_reasons.length
    ) {

        reasons =
            reasons.concat(
                match.signal_reasons
            );

    }


    if (
        match.first_half_reasons
        &&
        match.first_half_reasons.length
    ) {

        reasons =
            reasons.concat(
                match.first_half_reasons
            );

    }


    reasons =
        [...new Set(reasons)];


    return `

        <div class="${className}">


            <div class="match-top">

                <div class="league">

                    🏆
                    ${escapeHtml(match.league)}

                </div>

                <div class="minute">

                    ⏱
                    ${match.minute}'

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

                ${signalBadge}

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
                reasons.length
                ?

                `

                <div class="reasons">

                    ${reasons
                        .map(
                            reason =>
                                "• " +
                                escapeHtml(reason)
                        )
                        .join("<br>")
                    }

                </div>

                `

                :

                ""
            }


        </div>

    `;

}


// ============================================================
// STATUS
// ============================================================

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

            data.scanner_healthy

            ? (
                data.scan_in_progress

                ? "🟢 Tarıyor"

                : "🟢 Hazır"
            )

            : "🔴 Sağlıksız";


        document.getElementById(
            "livecount"
        ).textContent =
            data.live_match_count || 0;


        document.getElementById(
            "analyzedcount"
        ).textContent =
            data.analyzed_match_count || 0;


        document.getElementById(
            "scanfinish"
        ).textContent =
            data.last_scan_finished || "-";


        const next =
            data.next_scan_in;


        document.getElementById(
            "nextscan"
        ).textContent =

            next == null

            ? "-"

            : (
                next <= 0
                ? "Şimdi"
                : next + " sn"
            );


        const watchdog =
            document.getElementById(
                "watchdog"
            );


        const watchdogText =
            document.getElementById(
                "watchdogtext"
            );


        if (
            data.scanner_healthy
        ) {

            watchdog.className =
                "watchdog ok";


            let text =
                "🟢 Scanner sağlıklı";


            if (
                data.seconds_since_scan != null
            ) {

                text +=
                    " | Son başarılı tarama " +
                    data.seconds_since_scan +
                    " sn önce";

            }


            watchdogText.textContent =
                text;

        } else {

            watchdog.className =
                "watchdog bad";


            watchdogText.textContent =
                data.watchdog_message ||
                "Scanner kontrol ediliyor...";

        }


    } catch (error) {

        document.getElementById(
            "status"
        ).textContent =
            "🔴 Sunucu bağlantı hatası";

    }

}


// ============================================================
// MAÇLARI YÜKLE
// ============================================================

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


        const container =
            document.getElementById(
                "matches"
            );


        let matches =
            data.matches || [];


        document.getElementById(
            "matchcount"
        ).textContent =
            matches.length +
            " maç";


        if (
            matches.length === 0
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


        // =====================================================
        // SIRALAMA
        // =====================================================

        matches.sort(
            (a, b) => {

                // Önce çok güçlü
                if (
                    Boolean(b.very_strong)
                    !==
                    Boolean(a.very_strong)
                ) {

                    return (
                        Number(b.very_strong)
                        -
                        Number(a.very_strong)
                    );

                }


                // Sonra priority
                if (
                    (b.priority || 0)
                    !==
                    (a.priority || 0)
                ) {

                    return (
                        (b.priority || 0)
                        -
                        (a.priority || 0)
                    );

                }


                // Sonra normal sinyal
                if (
                    (b.signal || 0)
                    !==
                    (a.signal || 0)
                ) {

                    return (
                        (b.signal || 0)
                        -
                        (a.signal || 0)
                    );

                }


                // Sonra ilk yarı
                return (
                    (b.first_half_signal || 0)
                    -
                    (a.first_half_signal || 0)
                );

            }
        );


        // Yeni güçlü maç bildirimi
        notifyStrongMatches(
            matches
        );


        container.innerHTML =
            matches
                .map(createMatch)
                .join("");


    } catch (error) {

        console.log(
            "Maç verisi alınamadı:",
            error
        );

    }

}


// ============================================================
// REFRESH
// ============================================================

async function refreshAll() {

    await loadStatus();

    await loadMatches();

}


// İlk yükleme
refreshAll();


// Her 5 saniyede panel güncelle
setInterval(
    refreshAll,
    5000
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

    state =
        get_state()

    matches =
        load_matches()


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

    state =
        get_state()


    now =
        time.time()


    last_timestamp =
        state.get(
            "last_scan_timestamp"
        )


    # ========================================================
    # GERÇEK WATCHDOG HESABI
    # ========================================================

    seconds_since_scan = None

    scanner_healthy = False

    watchdog_message = (
        "Henüz başarılı tarama yok."
    )


    if last_timestamp is not None:

        try:

            seconds_since_scan = max(
                0,
                int(
                    now -
                    float(last_timestamp)
                )
            )


            scanner_healthy = (
                seconds_since_scan
                <=
                WATCHDOG_TIMEOUT
            )


            if scanner_healthy:

                watchdog_message = (
                    "Scanner sağlıklı."
                )

            else:

                watchdog_message = (
                    "⚠️ Son başarılı taramadan "
                    f"{seconds_since_scan} saniye geçti."
                )

        except Exception:

            seconds_since_scan = None

            scanner_healthy = False

            watchdog_message = (
                "Watchdog zaman bilgisi okunamadı."
            )


    # ========================================================
    # SONRAKİ TARAMA
    # ========================================================

    next_scan_in = None


    if last_timestamp is not None:

        try:

            elapsed =
                now -
                float(last_timestamp)


            next_scan_in = max(
                0,
                int(
                    CHECK_SECONDS -
                    elapsed
                )
            )

        except Exception:

            next_scan_in = None


    return jsonify({

        "running":
            state["running"],

        "api_key":
            state["api_key"],

        "scanner":
            state["scanner"],

        "scanner_healthy":
            scanner_healthy,

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

        "last_scan_timestamp":
            last_timestamp,

        "seconds_since_scan":
            seconds_since_scan,

        "next_scan_in":
            next_scan_in,

        "watchdog_message":
            watchdog_message,

        "error":
            state["error"]

    })


# ============================================================
# SCANNER BAŞLAT
# ============================================================

start_scanner()


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
    print(
        "🌐 FLASK WEB SUNUCUSU BAŞLIYOR"
    )
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
