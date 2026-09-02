import os
import time
import json
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, render_template_string

# ============================================================
# GOL SİNYAL MERKEZİ - WEB ONLY
# PC'de çalışan bot mantığı temel alınmıştır.
# Telegram YOKTUR.
# ============================================================

app = Flask(__name__)

API_BASE = "https://v3.football.api-sports.io"
API_KEY = os.getenv("API_KEY", "").strip()

CHECK_SECONDS = int(os.getenv("CHECK_SECONDS", "30"))
SIGNAL_LIMIT = int(os.getenv("SIGNAL_LIMIT", "65"))
FIRST_HALF_LIMIT = int(os.getenv("FIRST_HALF_LIMIT", "65"))

# API çağrılarının sonsuza kadar beklememesi için
REQUEST_TIMEOUT = 15
LIVE_SNAPSHOT_FILE = "/tmp/gol_live_snapshot.json"

# Basit cache süreleri
LIVE_CACHE_TTL = 10
STATS_CACHE_TTL = 20

ALLOWED_LEAGUES = {
    # İNGİLTERE
    39, 40, 41, 42,
    # İSPANYA
    140, 141, 435,
    # İTALYA
    135, 136, 137,
    # ALMANYA
    78, 79, 80,
    # FRANSA
    61, 62, 63,
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
    # ROMANYA
    283, 284,
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
    # GÜNEY AFRİKA
    288
}

# ------------------------------------------------------------
# Ortak durum
# ------------------------------------------------------------

memory_lock = threading.Lock()
cache_lock = threading.Lock()
status_lock = threading.Lock()

match_memory: Dict[int, Dict[str, Any]] = {}

live_cache = {
    "ts": 0.0,
    "data": [],
    "all_live": [],
}

stats_cache: Dict[int, Dict[str, Any]] = {}

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
        league_id = ((match.get("league") or {}).get("id"))
        if league_id in ALLOWED_LEAGUES:
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


def calculate_first_half_signal(
    total: Dict[str, Any],
    minute: int,
    home_goals: int,
    away_goals: int
) -> Tuple[int, List[str], Optional[str]]:

    if minute < 15 or minute > 45:
        return 0, [], None

    score = 0
    reasons = []

    shots = total["shots"]
    target = total["target"]
    corners = total["corners"]
    inside = total["inside"]

    # ŞUT
    if shots >= 12:
        score += 22
        reasons.append("İlk yarıda şut sayısı çok yüksek")
    elif shots >= 9:
        score += 16
        reasons.append("İlk yarıda şut sayısı yüksek")
    elif shots >= 6:
        score += 9

    # İSABETLİ ŞUT
    if target >= 6:
        score += 25
        reasons.append("İlk yarıda isabetli şut çok yüksek")
    elif target >= 4:
        score += 17
        reasons.append("İlk yarıda isabetli şut yüksek")
    elif target >= 3:
        score += 9

    # KORNER
    if corners >= 6:
        score += 15
        reasons.append("İlk yarıda korner baskısı çok yüksek")
    elif corners >= 4:
        score += 9

    # CEZA SAHASI İÇİ
    if inside >= 7:
        score += 15
        reasons.append("İlk yarıda ceza sahası içi şut çok yüksek")
    elif inside >= 5:
        score += 9

    # 0-0
    if home_goals == 0 and away_goals == 0:
        score += 8
        reasons.append("İlk yarı 0-0, gol baskısı değerlendiriliyor")

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
                "match": snapshot,
                "stats_error": None,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "last_update": datetime.now().isoformat(timespec="seconds"),
            }
        return True

    normal_signal, normal_reasons = calculate_signal(
        total,
        minute,
        home_goals,
        away_goals,
    )

    first_half_signal, first_half_reasons, expected_team = calculate_first_half_signal(
        total,
        minute,
        home_goals,
        away_goals,
    )

    with memory_lock:
        current = match_memory.get(int(fixture_id))
        if current is None:
            return False

        # Gol olmuşsa PC çalışan sürümde toplam istatistik korunur,
        # yalnızca sinyal gönderilmiş bayrağı yeniden açılır.
        if tuple(current.get("score", current_score)) != current_score:
            current["score"] = current_score
            current["signal_sent"] = False

        current["match"] = snapshot
        current["current_stats"] = total
        current["current_signal"] = normal_signal
        current["current_reasons"] = normal_reasons
        current["first_half_signal"] = first_half_signal
        current["first_half_reasons"] = first_half_reasons
        current["expected_team"] = expected_team
        current["stats_error"] = None
        current["last_update"] = datetime.now().isoformat(timespec="seconds")

        # Web sürümünde "sent" sadece sinyalin eşik üstüne çıktığını izlemek için tutulur.
        if normal_signal >= SIGNAL_LIMIT:
            current["signal_sent"] = True

        if first_half_signal >= FIRST_HALF_LIMIT:
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
    while True:
        scan_started_ts = time.time()
        scan_started_iso = datetime.now().isoformat(timespec="seconds")

        with status_lock:
            scanner_status["running"] = True
            scanner_status["last_scan_started"] = scan_started_iso
            scanner_status["last_scan_error"] = None
            scanner_status["last_api_message"] = None

        analyzed_count = 0

        try:
            live_matches, live_error, api_live_count = get_live_matches()

            with status_lock:
                scanner_status["api_live_count"] = api_live_count
                scanner_status["eligible_live_count"] = len(live_matches)

            if live_error:
                with status_lock:
                    scanner_status["last_scan_error"] = live_error
                    scanner_status["last_api_message"] = live_error

                # Kota/servis hatasında gereksiz istatistik çağrısı yapılmaz.
                time.sleep(CHECK_SECONDS)
                continue

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

            remove_finished_matches(active_fixture_ids)

            with status_lock:
                scanner_status["analyzed_count"] = analyzed_count

        except Exception as exc:
            with status_lock:
                scanner_status["last_scan_error"] = str(exc)

        finally:
            duration = round(time.time() - scan_started_ts, 2)
            with status_lock:
                scanner_status["running"] = False
                scanner_status["last_scan_finished"] = datetime.now().isoformat(timespec="seconds")
                scanner_status["last_scan_duration"] = duration

        time.sleep(CHECK_SECONDS)


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
                "allowed": league_id in ALLOWED_LEAGUES,
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


@app.route("/api/status")
def api_status():
    # Bu endpoint API çağrısı ve DB kullanmaz; hızlı cevap verir.
    with status_lock:
        data = dict(scanner_status)

    data.update({
        "ok": True,
        "api_key_configured": bool(API_KEY),
        "check_seconds": CHECK_SECONDS,
        "signal_limit": SIGNAL_LIMIT,
        "first_half_limit": FIRST_HALF_LIMIT,
        "allowed_league_count": len(ALLOWED_LEAGUES),
    })

    return jsonify(data)


@app.route("/api/matches")
def api_matches():
    with memory_lock:
        items = []

        for fixture_id, item in match_memory.items():
            match = item.get("match") or {}
            stats = item.get("current_stats") or {}

            expected_team_key = item.get("expected_team")
            expected_team_name = None
            if expected_team_key == "home":
                expected_team_name = match.get("home_team")
            elif expected_team_key == "away":
                expected_team_name = match.get("away_team")

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
                "first_half_active": item.get("first_half_signal", 0) >= FIRST_HALF_LIMIT,
                "first_half_reasons": item.get("first_half_reasons", []),
                "expected_team": expected_team_name,
                "stats": {
                    "shots": stats.get("shots", 0),
                    "target": stats.get("target", 0),
                    "corners": stats.get("corners", 0),
                    "inside": stats.get("inside", 0),
                    "home": stats.get("home", {}),
                    "away": stats.get("away", {}),
                },
                "stats_error": item.get("stats_error"),
                "last_update": item.get("last_update"),
            })

    # Önce aktif normal sinyal, sonra ilk yarı sinyali, sonra skor.
    items.sort(
        key=lambda x: (
            x.get("signal_active", False),
            x.get("first_half_active", False),
            x.get("signal", 0),
            x.get("first_half_signal", 0),
        ),
        reverse=True,
    )

    return jsonify({
        "count": len(items),
        "matches": items,
    })


# ============================================================
# WEB ARAYÜZÜ
# ============================================================

PAGE = r"""
<!doctype html>
<html lang="tr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Gol Sinyal Merkezi</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Arial, Helvetica, sans-serif;
            background: #0c111b;
            color: #edf2f7;
        }
        .wrap {
            max-width: 1200px;
            margin: 0 auto;
            padding: 24px;
        }
        .top {
            display: flex;
            justify-content: space-between;
            gap: 16px;
            align-items: center;
            flex-wrap: wrap;
            margin-bottom: 20px;
        }
        h1 {
            margin: 0;
            font-size: 28px;
        }
        .sub {
            color: #9aa8bd;
            margin-top: 6px;
        }
        .status {
            padding: 10px 14px;
            border-radius: 999px;
            background: #182234;
            color: #d6e4ff;
            font-size: 14px;
        }
        .summary {
            display: grid;
            grid-template-columns: repeat(4, minmax(0,1fr));
            gap: 12px;
            margin-bottom: 20px;
        }
        .summary-card {
            background: #131c2a;
            border: 1px solid #233149;
            border-radius: 14px;
            padding: 16px;
        }
        .summary-card .label {
            font-size: 12px;
            color: #91a1b8;
            margin-bottom: 6px;
        }
        .summary-card .value {
            font-size: 24px;
            font-weight: 700;
        }
        .error {
            display: none;
            background: #3a1820;
            border: 1px solid #7d2b3b;
            color: #ffd8df;
            padding: 12px 14px;
            border-radius: 12px;
            margin-bottom: 18px;
        }
        .matches {
            display: grid;
            gap: 14px;
        }
        .card {
            background: #111a28;
            border: 1px solid #25334b;
            border-radius: 16px;
            overflow: hidden;
        }
        .card.signal {
            border-color: #24c477;
            box-shadow: 0 0 0 1px rgba(36,196,119,.15);
        }
        .card.firsthalf {
            border-color: #f1b44c;
        }
        .card-head {
            padding: 16px;
            display: flex;
            justify-content: space-between;
            gap: 16px;
            border-bottom: 1px solid #213049;
        }
        .league {
            font-size: 13px;
            color: #8ea0b8;
            margin-bottom: 8px;
        }
        .teams {
            font-size: 18px;
            font-weight: 700;
        }
        .scorebox {
            min-width: 92px;
            text-align: right;
        }
        .score {
            font-size: 24px;
            font-weight: 800;
        }
        .minute {
            color: #ffca69;
            font-weight: 700;
            margin-top: 4px;
        }
        .body {
            padding: 16px;
        }
        .signals {
            display: grid;
            grid-template-columns: repeat(2, minmax(0,1fr));
            gap: 12px;
            margin-bottom: 14px;
        }
        .sig {
            background: #182234;
            border-radius: 12px;
            padding: 12px;
        }
        .sig .title {
            color: #98a8bd;
            font-size: 12px;
        }
        .sig .num {
            font-size: 28px;
            font-weight: 800;
            margin-top: 4px;
        }
        .sig.active .num {
            color: #45db91;
        }
        .sig.first.active .num {
            color: #ffcb70;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(4, minmax(0,1fr));
            gap: 8px;
            margin-bottom: 14px;
        }
        .stat {
            background: #0d1521;
            border-radius: 10px;
            padding: 10px;
            text-align: center;
        }
        .stat .k {
            color: #8495ad;
            font-size: 11px;
        }
        .stat .v {
            font-size: 19px;
            font-weight: 700;
            margin-top: 3px;
        }
        .reasons {
            font-size: 13px;
            line-height: 1.6;
            color: #c7d2e3;
        }
        .tag {
            display: inline-block;
            margin: 4px 5px 0 0;
            padding: 5px 8px;
            border-radius: 999px;
            background: #1c293d;
        }
        .expected {
            margin-top: 10px;
            color: #ffca69;
            font-weight: 700;
        }
        .empty {
            padding: 34px;
            text-align: center;
            color: #8f9db1;
            background: #111a28;
            border: 1px dashed #2a3953;
            border-radius: 16px;
        }
        @media(max-width: 760px) {
            .summary { grid-template-columns: repeat(2, minmax(0,1fr)); }
            .stats { grid-template-columns: repeat(2, minmax(0,1fr)); }
            .signals { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
<div class="wrap">
    <div class="top">
        <div>
            <h1>⚽ Gol Sinyal Merkezi</h1>
            <div class="sub">
                Canlı maçlar gerçek API istatistikleriyle analiz edilir.
                <a href="/api/live-leagues" target="_blank" style="color:#7db7ff;margin-left:8px;">Canlı lig teşhisi</a>
            </div>
        </div>
        <div class="status" id="statusText">Bağlanıyor...</div>
    </div>

    <div id="errorBox" class="error"></div>

    <div class="summary">
        <div class="summary-card">
            <div class="label">API'deki canlı maç</div>
            <div class="value" id="apiLive">0</div>
        </div>
        <div class="summary-card">
            <div class="label">Uygun liglerde canlı</div>
            <div class="value" id="eligibleLive">0</div>
        </div>
        <div class="summary-card">
            <div class="label">Analiz edilen</div>
            <div class="value" id="analyzed">0</div>
        </div>
        <div class="summary-card">
            <div class="label">Web'de görünen maç</div>
            <div class="value" id="visibleMatches">0</div>
        </div>
    </div>

    <div id="matches" class="matches"></div>
</div>

<script>
function esc(v) {
    return String(v ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}

function num(v) {
    const n = Number(v || 0);
    return Number.isInteger(n) ? n : n.toFixed(1);
}

function reasonTags(reasons) {
    if (!reasons || !reasons.length) return '<span class="tag">Henüz özel neden yok</span>';
    return reasons.map(x => `<span class="tag">${esc(x)}</span>`).join("");
}

async function loadStatus() {
    try {
        const r = await fetch("/api/status", {cache: "no-store"});
        const s = await r.json();

        document.getElementById("apiLive").textContent = s.api_live_count ?? 0;
        document.getElementById("eligibleLive").textContent = s.eligible_live_count ?? 0;
        document.getElementById("analyzed").textContent = s.analyzed_count ?? 0;

        const text = s.running
            ? "Tarama yapılıyor..."
            : `Son tarama: ${s.last_scan_finished ? s.last_scan_finished.replace("T", " ") : "-"}`;

        document.getElementById("statusText").textContent = text;

        const errorBox = document.getElementById("errorBox");
        if (!s.api_key_configured) {
            errorBox.style.display = "block";
            errorBox.textContent = "API_KEY tanımlı değil. Render Environment bölümüne API_KEY ekle.";
        } else if (s.last_scan_error) {
            errorBox.style.display = "block";
            errorBox.textContent = s.last_scan_error;
        } else {
            errorBox.style.display = "none";
            errorBox.textContent = "";
        }
    } catch (e) {
        document.getElementById("statusText").textContent = "Durum alınamadı";
    }
}

async function loadMatches() {
    try {
        const r = await fetch("/api/matches", {cache: "no-store"});
        const data = await r.json();
        const matches = data.matches || [];

        document.getElementById("visibleMatches").textContent = matches.length;

        const root = document.getElementById("matches");

        if (!matches.length) {
            root.innerHTML = `
                <div class="empty">
                    Şu anda istatistiği alınabilen uygun canlı maç yok.
                    <br><br>
                    Maç ilk görüldüğünde hafızaya alınır; sinyal ikinci taramadan itibaren hesaplanır.
                </div>
            `;
            return;
        }

        root.innerHTML = matches.map(m => {
            const cls = m.signal_active ? "card signal" : (m.first_half_active ? "card firsthalf" : "card");
            const fhReasonHtml = m.first_half_signal > 0
                ? `<div style="margin-top:10px"><strong>İlk yarı nedenleri:</strong><br>${reasonTags(m.first_half_reasons)}</div>`
                : "";

            const expected = m.expected_team
                ? `<div class="expected">Beklenen baskın takım: ${esc(m.expected_team)}</div>`
                : "";

            return `
                <div class="${cls}">
                    <div class="card-head">
                        <div>
                            <div class="league">${esc(m.country || "")} ${m.country ? "•" : ""} ${esc(m.league || "")}</div>
                            <div class="teams">${esc(m.home_team)} - ${esc(m.away_team)}</div>
                        </div>
                        <div class="scorebox">
                            <div class="score">${m.home_goals ?? 0} - ${m.away_goals ?? 0}</div>
                            <div class="minute">${m.minute ?? 0}'</div>
                        </div>
                    </div>

                    <div class="body">
                        <div class="signals">
                            <div class="sig ${m.signal_active ? "active" : ""}">
                                <div class="title">Gol sinyali</div>
                                <div class="num">%${m.signal ?? 0}</div>
                            </div>
                            <div class="sig first ${m.first_half_active ? "active" : ""}">
                                <div class="title">İlk yarı gol sinyali</div>
                                <div class="num">%${m.first_half_signal ?? 0}</div>
                            </div>
                        </div>

                        <div class="stats">
                            <div class="stat"><div class="k">Toplam Şut</div><div class="v">${num(m.stats?.shots)}</div></div>
                            <div class="stat"><div class="k">İsabetli Şut</div><div class="v">${num(m.stats?.target)}</div></div>
                            <div class="stat"><div class="k">Korner</div><div class="v">${num(m.stats?.corners)}</div></div>
                            <div class="stat"><div class="k">Ceza Sahası İçi</div><div class="v">${num(m.stats?.inside)}</div></div>
                        </div>

                        <div class="reasons">
                            <strong>Gol sinyali nedenleri:</strong><br>
                            ${reasonTags(m.reasons)}
                            ${fhReasonHtml}
                            ${expected}
                        </div>
                    </div>
                </div>
            `;
        }).join("");
    } catch (e) {
        document.getElementById("matches").innerHTML =
            '<div class="empty">Maç verileri alınamadı.</div>';
    }
}

async function refreshAll() {
    await Promise.all([loadStatus(), loadMatches()]);
}

refreshAll();
setInterval(refreshAll, 5000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


# Gunicorn import ettiğinde scanner başlasın.
ensure_scanner_started()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
