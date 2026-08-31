import os
import time
import json
import sqlite3
import threading
import fcntl
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
FIRST_HALF_LIMIT = 65
VERY_STRONG_SIGNAL = 80

API_TIMEOUT = 20
API_RETRIES = 3

WATCHDOG_LIMIT = 120

DB_FILE = "/tmp/gol_scanner.db"
LOCK_FILE = "/tmp/gol_scanner.lock"


# ============================================================
# İZİN VERİLEN LİGLER
# ============================================================

ALLOWED_LEAGUES = {
    39, 40, 41, 42,
    140, 141, 435,
    135, 136, 137,
    78, 79, 80,
    61, 62, 63,
    203, 204, 205, 206,
    88, 89,
    144, 145,
    94, 95,
    179, 180, 181, 182,
    218, 219,
    207, 208,
    197,
    106, 107,
    345, 346,
    119, 120,
    103, 104,
    113, 114,
    253,
    71, 72,
    128,
    262, 263,
    98, 99,
    292, 293,
    188,
    283, 284,
    286,
    210,
    172,
    357,
    244,
    164,
    288
}


# ============================================================
# RUNTIME
# ============================================================

scanner_thread = None
scanner_lock_file = None


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

                last_success_timestamp REAL DEFAULT 0,

                error TEXT,
                watchdog_message TEXT
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
                last_success_timestamp,
                error,
                watchdog_message
            )
            VALUES (
                1,
                1,
                ?,
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
                NULL,
                'Henüz başarılı tarama yok.'
            )
        """, (
            1 if API_KEY else 0,
            1 if API_KEY else 0,
            len(ALLOWED_LEAGUES)
        ))

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
        "last_success_timestamp",
        "error",
        "watchdog_message"
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
                "error": None,
                "watchdog_message": "Henüz başarılı tarama yok."
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
# BAŞLAT
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

    for attempt in range(1, API_RETRIES + 1):
        try:
            response = requests.get(
                f"{API_URL}/{endpoint}",
                headers=headers,
                params=params,
                timeout=API_TIMEOUT
            )

            print(
                f"API -> {endpoint} | "
                f"HTTP {response.status_code} | "
                f"Deneme {attempt}/{API_RETRIES}"
            )

            if response.status_code == 200:
                data = response.json()

                if data.get("errors"):
                    print(
                        "❌ API hatası:",
                        data.get("errors")
                    )

                    if attempt < API_RETRIES:
                        time.sleep(1.5)
                        continue

                    return None

                return data

            print(
                "❌ API HTTP hatası:",
                response.status_code
            )

            if attempt < API_RETRIES:
                time.sleep(1.5)

        except requests.RequestException as exc:
            print(
                "❌ API bağlantı hatası:",
                repr(exc)
            )

            if attempt < API_RETRIES:
                time.sleep(1.5)

        except Exception as exc:
            print(
                "❌ API beklenmeyen hata:",
                repr(exc)
            )

            if attempt < API_RETRIES:
                time.sleep(1.5)

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
        return None

    matches = data.get("response", [])

    print(
        f"📡 API canlı maç sayısı: {len(matches)}"
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
        f"✅ Uygun liglerde canlı maç: {len(result)}"
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
        return None

    return data.get("response", [])


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
    if not stats or len(stats) < 2:
        return None

    home = stats[0].get("statistics", [])
    away = stats[1].get("statistics", [])

    home_data = {
        "shots": stat_value(home, "Total Shots"),
        "target": stat_value(home, "Shots on Goal"),
        "corners": stat_value(home, "Corner Kicks"),
        "inside": stat_value(home, "Shots insidebox")
    }

    away_data = {
        "shots": stat_value(away, "Total Shots"),
        "target": stat_value(away, "Shots on Goal"),
        "corners": stat_value(away, "Corner Kicks"),
        "inside": stat_value(away, "Shots insidebox")
    }

    return home_data, away_data


def get_total_stats(stats):
    team_stats = get_team_stats(stats)

    if not team_stats:
        return None

    home, away = team_stats

    return {
        "shots": home["shots"] + away["shots"],
        "target": home["target"] + away["target"],
        "corners": home["corners"] + away["corners"],
        "inside": home["inside"] + away["inside"]
    }


# ============================================================
# SİNYAL
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
        reasons.append("Çok fazla şut")
    elif total_shots >= 11:
        score += 14
        reasons.append("Şut sayısı yüksek")
    elif total_shots >= 8:
        score += 8

    if total_target >= 8:
        score += 25
        reasons.append("İsabetli şut çok yüksek")
    elif total_target >= 6:
        score += 18
        reasons.append("İsabetli şut yüksek")
    elif total_target >= 4:
        score += 10

    if total_corners >= 9:
        score += 15
        reasons.append("Korner baskısı yüksek")
    elif total_corners >= 6:
        score += 9
    elif total_corners >= 4:
        score += 4

    if total_inside >= 10:
        score += 15
        reasons.append("Ceza sahası şutları yüksek")
    elif total_inside >= 7:
        score += 10

    if 55 <= minute <= 75:
        score += 5
    elif minute >= 76:
        score += 8

    total_goals = goals_home + goals_away

    if total_goals == 0 and minute >= 55:
        score += 8
        reasons.append("55+ dakika ve skor 0-0")

    elif (
        abs(goals_home - goals_away) == 1
        and minute >= 60
    ):
        score += 5
        reasons.append("Maç tek farklı")

    return min(score, 100), reasons


# ============================================================
# İLK YARI
# ============================================================

def calculate_first_half_signal(match, team_stats):
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

    total_shots = home["shots"] + away["shots"]

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

    total_target = home["target"] + away["target"]

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

    total_corners = home["corners"] + away["corners"]

    if total_corners >= 6:
        score += 15
        reasons.append(
            "Korner baskısı yüksek"
        )
    elif total_corners >= 4:
        score += 9

    total_inside = home["inside"] + away["inside"]

    if total_inside >= 7:
        score += 15
        reasons.append(
            "Ceza sahası şutları yüksek"
        )
    elif total_inside >= 5:
        score += 9

    if goals_home + goals_away == 0:
        score += 8
        reasons.append("Skor hâlâ 0-0")

    home_pressure = (
        home["shots"]
        + home["target"] * 2.5
        + home["corners"] * 1.2
        + home["inside"] * 1.5
    )

    away_pressure = (
        away["shots"]
        + away["target"] * 2.5
        + away["corners"] * 1.2
        + away["inside"] * 1.5
    )

    if home_pressure > away_pressure * 1.20:
        expected_team = (
            "🏠 "
            + match["teams"]["home"]["name"]
        )

    elif away_pressure > home_pressure * 1.20:
        expected_team = (
            "✈️ "
            + match["teams"]["away"]["name"]
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

def analyze_match(match, previous=None):
    fixture_id = match["fixture"]["id"]

    fixture_status = (
        match
        .get("fixture", {})
        .get("status", {})
    )

    minute = (
        fixture_status.get("elapsed")
        or 0
    )

    home = match["teams"]["home"]["name"]
    away = match["teams"]["away"]["name"]

    league_name = (
        match
        .get("league", {})
        .get("name", "Bilinmeyen Lig")
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

    # 15 dakikadan önce analiz yapmıyoruz.
    # Fakat eski veri varsa maçın güncel dakika/skorunu
    # koruyarak göstermeye devam ediyoruz.
    if minute < 15:
        if previous:
            result = dict(previous)

            result["minute"] = minute
            result["score_home"] = goals_home
            result["score_away"] = goals_away
            result["home"] = home
            result["away"] = away
            result["league"] = league_name

            return result

        return None

    print(
        f"🔎 Analiz: {home} - {away} | "
        f"{minute}' | {goals_home}-{goals_away}"
    )

    stats = get_stats(fixture_id)

    current_stats = get_total_stats(stats)
    team_stats = get_team_stats(stats)

    # ========================================================
    # İSTATİSTİK GELMEDİYSE ÖNCEKİ TARAMAYI KORU
    # ========================================================

    if not current_stats or not team_stats:
        print(
            f"⚠️ {home} - {away} "
            f"istatistik alınamadı."
        )

        if previous:
            result = dict(previous)

            # Dakika ve skor MUTLAKA güncellenecek.
            result["minute"] = minute
            result["score_home"] = goals_home
            result["score_away"] = goals_away
            result["home"] = home
            result["away"] = away
            result["league"] = league_name

            result["stats_cached"] = True

            return result

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
        home_stats["shots"]
        + home_stats["target"] * 2.5
        + home_stats["corners"] * 1.2
        + home_stats["inside"] * 1.5
    )

    away_pressure = (
        away_stats["shots"]
        + away_stats["target"] * 2.5
        + away_stats["corners"] * 1.2
        + away_stats["inside"] * 1.5
    )

    if home_pressure > away_pressure * 1.20:
        normal_expected_team = "🏠 " + home

    elif away_pressure > home_pressure * 1.20:
        normal_expected_team = "✈️ " + away

    else:
        normal_expected_team = "⚽ Her iki takım"

    priority = max(
        signal,
        first_signal
    )

    strong_signal = signal >= SIGNAL_LIMIT

    strong_first_half = (
        first_signal >= FIRST_HALF_LIMIT
    )

    very_strong = (
        priority >= VERY_STRONG_SIGNAL
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

        "expected_team": normal_expected_team,

        "first_half_signal": int(first_signal),
        "first_half_reasons": first_reasons,

        "strong_signal": strong_signal,
        "strong_first_half": strong_first_half,
        "very_strong": very_strong,

        "priority": int(priority),

        "stats_cached": False
    }


# ============================================================
# SIRALAMA
# ============================================================

def sort_matches(matches):
    matches.sort(
        key=lambda x: (
            bool(x.get("very_strong", False)),
            bool(x.get("strong_signal", False)),
            bool(x.get("strong_first_half", False)),
            int(x.get("priority", 0)),
            int(x.get("signal", 0)),
            int(x.get("first_half_signal", 0))
        ),
        reverse=True
    )

    return matches


# ============================================================
# SCANNER LOCK
# ============================================================

def acquire_scanner_lock():
    global scanner_lock_file

    try:
        lock_file = open(
            LOCK_FILE,
            "w"
        )

        fcntl.flock(
            lock_file.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB
        )

        scanner_lock_file = lock_file

        return lock_file

    except Exception:
        return None


# ============================================================
# WATCHDOG
# ============================================================

def watchdog_loop():
    print("🐕 Watchdog başladı.")

    while True:
        try:
            state = get_state()

            last_success = float(
                state.get(
                    "last_success_timestamp",
                    0
                ) or 0
            )

            if last_success <= 0:
                update_state(
                    scanner=state.get("scanner", 0),
                    watchdog_message="Henüz başarılı tarama yok."
                )

            else:
                seconds = (
                    time.time()
                    - last_success
                )

                if seconds > WATCHDOG_LIMIT:
                    message = (
                        f"⚠️ Scanner sağlıksız: "
                        f"{int(seconds)} saniyedir "
                        f"başarılı tarama yok."
                    )

                    print(message)

                    update_state(
                        scanner=0,
                        watchdog_message=message,
                        error="Watchdog: başarılı tarama zaman aşımı."
                    )

                else:
                    update_state(
                        watchdog_message="Scanner sağlıklı."
                    )

        except Exception as exc:
            print(
                "❌ Watchdog hatası:",
                repr(exc)
            )

        time.sleep(15)


# ============================================================
# SCANNER
# ============================================================

def scanner_loop():
    print("")
    print("=" * 60)
    print("🚀 MAÇ TARAMA MOTORU BAŞLADI")
    print("=" * 60)

    previous_matches = {}

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
            print("📡 CANLI MAÇLAR TARAMASI")
            print("=" * 60)

            matches = get_live_matches()

            # =================================================
            # API CANLI MAÇ DÖNDÜRMEDİ
            # =================================================

            if matches is None:
                raise RuntimeError(
                    "Canlı maç API sorgusu başarısız."
                )

            live_count = len(matches)

            update_state(
                live_match_count=live_count,
                eligible_match_count=live_count
            )

            analyzed = []

            current_ids = set()

            for match in matches:
                fixture_id = (
                    match
                    .get("fixture", {})
                    .get("id")
                )

                if not fixture_id:
                    continue

                current_ids.add(
                    fixture_id
                )

                old = previous_matches.get(
                    fixture_id
                )

                try:
                    result = analyze_match(
                        match,
                        old
                    )

                    if result:
                        analyzed.append(
                            result
                        )

                except Exception as exc:
                    print(
                        f"❌ Maç analiz hatası "
                        f"{fixture_id}:",
                        repr(exc)
                    )

                    # Maçın eski verisi varsa kaybetme.
                    if old:
                        fallback = dict(old)

                        fallback["minute"] = (
                            match
                            .get("fixture", {})
                            .get("status", {})
                            .get("elapsed")
                            or old.get("minute", 0)
                        )

                        fallback["score_home"] = (
                            match
                            .get("goals", {})
                            .get("home")
                            or 0
                        )

                        fallback["score_away"] = (
                            match
                            .get("goals", {})
                            .get("away")
                            or 0
                        )

                        analyzed.append(
                            fallback
                        )

            # =================================================
            # ESKİ MAÇLARDAN SADECE HALA CANLI OLANLARI TUT
            # =================================================

            new_previous = {}

            for item in analyzed:
                fixture_id = item.get(
                    "fixture_id"
                )

                if fixture_id:
                    new_previous[
                        fixture_id
                    ] = item

            previous_matches = new_previous

            # =================================================
            # SIRALA
            # =================================================

            analyzed = sort_matches(
                analyzed
            )

            # =================================================
            # KAYDET
            # =================================================

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

                analyzed_match_count=len(
                    analyzed
                ),

                match_count=len(
                    analyzed
                ),

                updated=finish_time,

                last_scan_finished=finish_time,

                last_success_timestamp=now_timestamp,

                error=None,

                watchdog_message="Scanner sağlıklı."
            )

            print("")
            print(
                f"📡 Canlı maç: {live_count}"
            )

            print(
                f"📊 Gösterilen/analyzed maç: "
                f"{len(analyzed)}"
            )

            if len(analyzed) < live_count:
                print(
                    f"⚠️ {live_count - len(analyzed)} "
                    f"maçta henüz kullanılabilir "
                    f"istatistik bulunamadı."
                )

            print(
                f"⏱ Tarama süresi: "
                f"{time.time() - cycle_start:.1f} saniye"
            )

        except Exception as exc:
            error_text = str(exc)

            print("")
            print("=" * 60)
            print("❌ TARAMA MOTORU HATASI")
            print("=" * 60)
            print(
                "HATA:",
                repr(exc)
            )
            print("=" * 60)

            update_state(
                scanner=1,
                scan_in_progress=0,
                error=error_text,
                watchdog_message=(
                    "Son tarama başarısız: "
                    + error_text
                )
            )

        elapsed = (
            time.time()
            - cycle_start
        )

        sleep_time = max(
            1,
            CHECK_SECONDS - int(elapsed)
        )

        print(
            f"😴 {sleep_time} saniye sonra "
            f"yeni tarama..."
        )

        time.sleep(
            sleep_time
        )


# ============================================================
# SCANNER BAŞLAT
# ============================================================

def start_scanner():
    global scanner_thread

    if (
        scanner_thread is not None
        and scanner_thread.is_alive()
    ):
        return

    lock = acquire_scanner_lock()

    if lock is None:
        print(
            "ℹ️ Bu Gunicorn worker "
            "scanner lock alamadı."
        )
        return

    scanner_thread = threading.Thread(
        target=scanner_loop,
        name="FootballScanner",
        daemon=True
    )

    scanner_thread.start()

    print(
        "✅ Scanner thread başlatıldı."
    )


def start_watchdog():
    thread = threading.Thread(
        target=watchdog_loop,
        name="ScannerWatchdog",
        daemon=True
    )

    thread.start()

    print(
        "🐕 Watchdog thread başlatıldı."
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
    font-family: Arial, Helvetica, sans-serif;
}

.header {
    background: linear-gradient(
        135deg,
        #0f172a,
        #172554
    );

    padding: 18px 15px;
    text-align: center;

    border-bottom: 1px solid #334155;

    position: sticky;
    top: 0;
    z-index: 50;
}

.header h1 {
    margin: 0;
    font-size: 24px;
}

.header p {
    margin: 5px 0 0;
    color: #94a3b8;
    font-size: 13px;
}

.container {
    max-width: 1050px;
    margin: auto;
    padding: 12px;
}

.status {
    background: #111c2d;
    border: 1px solid #26364d;
    padding: 13px;
    border-radius: 12px;
    margin-bottom: 12px;
}

.status-grid {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(135px, 1fr));
    gap: 7px;
}

.status-item {
    background: #0b1423;
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 12px;
}

.status-item b {
    display: block;
    color: #94a3b8;
    font-size: 10px;
    margin-bottom: 3px;
}

.section-title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 15px 2px 8px;
}

.section-title h2 {
    margin: 0;
    font-size: 17px;
}

.section-title span {
    color: #94a3b8;
    font-size: 11px;
}

.match {
    background: #111c2d;
    border: 1px solid #26364d;
    border-radius: 11px;
    padding: 11px;
    margin-bottom: 8px;
}

.match.very-strong {
    border: 2px solid #22c55e;
    background: linear-gradient(
        135deg,
        #10291d,
        #111c2d
    );
}

.match.strong {
    border: 2px solid #16a34a;
}

.match.first-half {
    border: 2px solid #f59e0b;
}

.match-top {
    display: flex;
    justify-content: space-between;
    gap: 10px;
}

.league {
    color: #60a5fa;
    font-size: 11px;
    font-weight: bold;
}

.minute {
    color: #cbd5e1;
    font-size: 12px;
}

.teams-row {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    gap: 8px;
    margin-top: 7px;
}

.team {
    font-size: 15px;
    font-weight: bold;
}

.team.away {
    text-align: right;
}

.score {
    font-size: 21px;
    font-weight: 900;
}

.stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 5px;
    margin-top: 9px;
}

.stat {
    background: #0b1423;
    padding: 6px 4px;
    border-radius: 7px;
    text-align: center;
    color: #94a3b8;
    font-size: 9px;
}

.stat b {
    display: block;
    font-size: 15px;
    color: #f8fafc;
    margin-top: 2px;
}

.signal-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 8px;
}

.signal-score {
    font-size: 17px;
    font-weight: 900;
}

.signal-normal {
    color: #94a3b8;
}

.signal-good {
    color: #4ade80;
}

.signal-very {
    color: #22c55e;
}

.badge {
    display: inline-block;
    padding: 4px 7px;
    border-radius: 20px;
    font-size: 9px;
    font-weight: bold;
    margin-left: 5px;
}

.badge-green {
    background: #14532d;
    color: #86efac;
}

.badge-orange {
    background: #713f12;
    color: #fde68a;
}

.expected {
    margin-top: 7px;
    padding: 7px 9px;
    background: #0b1423;
    border-radius: 7px;
    font-size: 11px;
}

.details {
    margin-top: 7px;
    color: #cbd5e1;
    font-size: 10px;
}

.reasons {
    margin-top: 5px;
    color: #94a3b8;
}

.reason {
    margin-top: 2px;
}

.cached {
    color: #fbbf24;
    font-size: 9px;
    margin-top: 5px;
}

.empty {
    text-align: center;
    padding: 45px 15px;
    color: #94a3b8;
    background: #111c2d;
    border: 1px solid #26364d;
    border-radius: 12px;
}

.notification-container {
    position: fixed;
    top: 72px;
    right: 12px;
    width: min(350px, calc(100vw - 24px));
    z-index: 9999;
}

.notification {
    background: #10291d;
    border: 1px solid #22c55e;
    box-shadow: 0 8px 30px rgba(0,0,0,.45);
    border-radius: 12px;
    padding: 13px;
    margin-bottom: 8px;
}

.notification-title {
    color: #4ade80;
    font-size: 13px;
    font-weight: 900;
}

.notification-match {
    margin-top: 5px;
    font-size: 14px;
    font-weight: bold;
}

.notification-score {
    margin-top: 4px;
    color: #bbf7d0;
    font-size: 12px;
}

@media (max-width: 600px) {

    .container {
        padding: 8px;
    }

    .header h1 {
        font-size: 20px;
    }

    .team {
        font-size: 13px;
    }

    .score {
        font-size: 19px;
    }
}

</style>

</head>

<body>

<div class="header">

<h1>⚽ GOL SİNYAL MERKEZİ</h1>

<p>
Yüksek gol ihtimali olan canlı maçlar öne çıkarılır
</p>

</div>

<div class="container">

<div class="status">

<div class="status-grid">

<div class="status-item">
<b>SİSTEM</b>
<span id="status">Bağlanıyor...</span>
</div>

<div class="status-item">
<b>SCANNER</b>
<span id="scanner">-</span>
</div>

<div class="status-item">
<b>CANLI</b>
<span id="livecount">0</span>
</div>

<div class="status-item">
<b>ANALİZ</b>
<span id="analyzedcount">0</span>
</div>

<div class="status-item">
<b>SON GÜNCELLEME</b>
<span id="updated">-</span>
</div>

<div class="status-item">
<b>SON BAŞARILI TARAMA</b>
<span id="scanfinish">-</span>
</div>

<div class="status-item">
<b>WATCHDOG</b>
<span id="watchdog">-</span>
</div>

</div>

</div>

<div
class="section-title"
id="veryStrongTitle"
style="display:none;"
>

<h2>🔥 ÇOK YÜKSEK GOL SİNYALİ</h2>

<span>%80+</span>

</div>

<div id="veryStrongMatches"></div>

<div
class="section-title"
id="strongTitle"
style="display:none;"
>

<h2>🟢 YÜKSEK GOL SİNYALİ</h2>

<span>%65+</span>

</div>

<div id="strongMatches"></div>

<div
class="section-title"
id="otherTitle"
style="display:none;"
>

<h2>📊 DİĞER ANALİZLER</h2>

<span>Daha düşük sinyaller</span>

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
Şu anda analiz edilen canlı maç yok.
</b>

<br><br>

Sistem otomatik olarak taramaya devam ediyor.

</div>

</div>

<div
id="notificationContainer"
class="notification-container"
></div>


<script>

const notifiedSignals = new Set();

let firstLoad = true;


function escapeHtml(text) {

    const div =
        document.createElement("div");

    div.textContent =
        text ?? "";

    return div.innerHTML;
}


function showNotification(match) {

    const container =
        document.getElementById(
            "notificationContainer"
        );

    const box =
        document.createElement("div");

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
            <b>%${match.signal}</b>

            &nbsp; | &nbsp;

            ${match.minute}'

        </div>
    `;

    container.appendChild(box);

    setTimeout(
        () => box.remove(),
        7000
    );

    if (
        "Notification" in window &&
        Notification.permission === "granted"
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

            console.log(e);

        }

    }
}


function requestNotificationPermission() {

    if (!("Notification" in window)) {
        return;
    }

    if (
        Notification.permission === "default"
    ) {

        Notification.requestPermission()
        .catch(() => {});

    }
}


document.addEventListener(
    "click",
    requestNotificationPermission,
    {once: true}
);


function createMatch(match) {

    let className = "match";

    if (match.very_strong) {

        className += " very-strong";

    } else if (match.strong_signal) {

        className += " strong";

    } else if (match.strong_first_half) {

        className += " first-half";

    }


    let signalClass =
        "signal-normal";

    if (match.signal >= 80) {

        signalClass = "signal-very";

    } else if (match.signal >= 65) {

        signalClass = "signal-good";

    }


    let badge = "";

    if (match.very_strong) {

        badge = `
            <span class="badge badge-green">
                🔥 ÇOK GÜÇLÜ
            </span>
        `;

    } else if (match.strong_signal) {

        badge = `
            <span class="badge badge-green">
                🟢 GÜÇLÜ
            </span>
        `;

    } else if (match.strong_first_half) {

        badge = `
            <span class="badge badge-orange">
                ⚡ İLK YARI
            </span>
        `;

    }


    let firstHalf = "";

    if (match.first_half_signal >= 65) {

        firstHalf = `
            <span class="badge badge-orange">
                ⚡ İlk Yarı
                %${match.first_half_signal}
            </span>
        `;

    }


    let reasons = "";

    if (
        match.signal_reasons &&
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


    let cached = "";

    if (match.stats_cached) {

        cached = `
            <div class="cached">
                ⚠️ İstatistik geçici olarak alınamadı;
                önceki istatistik kullanılıyor.
            </div>
        `;

    }


    return `

        <div
            class="${className}"
            data-fixture="${match.fixture_id}"
        >

            <div class="match-top">

                <div class="league">
                    🏆 ${escapeHtml(match.league)}
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

                <div>

                    <span
                        class="signal-score ${signalClass}"
                    >
                        🔥 %${match.signal}
                    </span>

                    ${badge}

                    ${firstHalf}

                </div>

            </div>

            <div class="expected">

                ⚽

                <b>Gol beklenen taraf:</b>

                ${escapeHtml(
                    match.expected_team
                )}

            </div>

            ${
                reasons
                ?
                `
                <div class="details">

                    <b>Neden?</b>

                    <div class="reasons">
                        ${reasons}
                    </div>

                </div>
                `
                :
                ""
            }

            ${cached}

        </div>

    `;

}


function renderMatches(matches) {

    const veryStrong =
        matches.filter(
            m => m.very_strong
        );

    const strong =
        matches.filter(
            m =>
                !m.very_strong &&
                (
                    m.strong_signal ||
                    m.strong_first_half
                )
        );

    const other =
        matches.filter(
            m =>
                !m.very_strong &&
                !m.strong_signal &&
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
        veryStrong.map(
            createMatch
        ).join("");


    document.getElementById(
        "strongMatches"
    ).innerHTML =
        strong.map(
            createMatch
        ).join("");


    document.getElementById(
        "otherMatches"
    ).innerHTML =
        other.map(
            createMatch
        ).join("");


    document.getElementById(
        "empty"
    ).style.display =
        matches.length
        ? "none"
        : "block";


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
                    notifiedSignals.has(key)
                ) {
                    return;
                }

                notifiedSignals.add(key);

                showNotification(match);

            }
        );

    }


    if (firstLoad) {

        matches.forEach(
            match => {

                if (match.signal >= 65) {

                    notifiedSignals.add(
                        String(
                            match.fixture_id
                        ) +
                        "-" +
                        String(
                            match.signal
                        )
                    );

                }

            }
        );

        firstLoad = false;
    }


    if (notifiedSignals.size > 500) {
        notifiedSignals.clear();
    }
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

        if (!response.ok) {
            throw new Error(
                "HTTP " + response.status
            );
        }

        const data =
            await response.json();


        document.getElementById(
            "status"
        ).textContent =
            data.error
            ? "🔴 Hata"
            : "🟢 Çalışıyor";


        let scannerText;

        if (!data.scanner) {

            scannerText =
                "🔴 Durdu";

        } else if (
            data.scan_in_progress
        ) {

            scannerText =
                "🟢 Tarıyor";

        } else {

            scannerText =
                "🟢 Hazır";

        }

        document.getElementById(
            "scanner"
        ).textContent =
            scannerText;


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


        document.getElementById(
            "watchdog"
        ).textContent =
            data.scanner_healthy
            ? "🟢 Sağlıklı"
            : "🔴 " +
              (
                data.watchdog_message ||
                "Kontrol ediliyor..."
              );

    } catch (error) {

        document.getElementById(
            "status"
        ).textContent =
            "🔴 Bağlantı hatası";

        document.getElementById(
            "scanner"
        ).textContent =
            "🔴 Sunucu bağlantısı yok";

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

        if (!response.ok) {
            throw new Error(
                "HTTP " + response.status
            );
        }

        const data =
            await response.json();

        renderMatches(
            data.matches || []
        );

    } catch (error) {

        console.log(
            "Maç verisi alınamadı:",
            error
        );

    }
}


async function refreshAll() {

    await Promise.all([
        loadStatus(),
        loadMatches()
    ]);

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
        "matches": matches,
        "updated": state["updated"],
        "error": state["error"],
        "scanner_running": state["scanner"],
        "scan_in_progress": state["scan_in_progress"],
        "last_scan_started": state["last_scan_started"],
        "last_scan_finished": state["last_scan_finished"],
        "scanner_healthy": is_scanner_healthy(state),
        "watchdog_message": state["watchdog_message"]
    })


# ============================================================
# HEALTH
# ============================================================

def is_scanner_healthy(state=None):
    if state is None:
        state = get_state()

    last_success = float(
        state.get(
            "last_success_timestamp",
            0
        ) or 0
    )

    if last_success <= 0:
        return False

    return (
        time.time() - last_success
    ) <= WATCHDOG_LIMIT


# ============================================================
# API STATUS
# ============================================================

@app.route("/api/status")
def api_status():
    state = get_state()

    last_success = float(
        state.get(
            "last_success_timestamp",
            0
        ) or 0
    )

    if last_success > 0:
        seconds_since_scan = int(
            max(
                0,
                time.time() - last_success
            )
        )
    else:
        seconds_since_scan = None

    healthy = is_scanner_healthy(
        state
    )

    return jsonify({

        "running":
            state["running"],

        "api_key":
            state["api_key"],

        "scanner":
            state["scanner"],

        "scanner_healthy":
            healthy,

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

        "last_success_timestamp":
            state["last_success_timestamp"],

        "seconds_since_scan":
            seconds_since_scan,

        "next_scan_in":
            (
                max(
                    0,
                    CHECK_SECONDS -
                    int(
                        time.time()
                        -
                        last_success
                    )
                )
                if last_success > 0
                else None
            ),

        "error":
            state["error"],

        "watchdog_message":
            state["watchdog_message"]

    })


# ============================================================
# THREADLERİ BAŞLAT
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

    print(
        f"🔑 API KEY: "
        f"{'OK' if API_KEY else 'YOK'}"
    )

    print(
        f"📊 İzin verilen lig: "
        f"{len(ALLOWED_LEAGUES)}"
    )

    print("=" * 60)
    print("")

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
