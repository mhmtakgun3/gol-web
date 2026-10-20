"""Gol Merkezi Popüler Bahis Radarı — yalnızca kamuya açık veriler.
Bir platform veri sunmuyorsa durum açıkça belirtilir; popülerlik başarı demek değildir.
"""
from __future__ import annotations
from flask import Blueprint, jsonify, render_template_string, request
from html.parser import HTMLParser
from datetime import datetime, timezone
import re
import requests
import math
import time
import threading

radar_bp = Blueprint("populer_radar", __name__)
URLS = {
 "Bilyoner": "https://www.bilyoner.com/iddaa/populer-bahisler",
 "Misli": "https://www.misli.com/iddaa/anlik-bahis",
 "Nesine": "https://www.nesine.com/",
 "iddaa.com": "https://www.iddaa.com/",
}
NESINE_API = "https://kapi.nesine.com/api/v1/KStreamsAll/PreBetAll?orderType=0&sportId=0&timeEpoch=-1"
MISLI_API = "https://apivx.misli.com/api/web/v1/statistics/iddaa/most-played?type=ALL&limit=100"
IDDAA_API = "https://sportsbookv2.iddaa.com/SportsBook/get-populer-bets?limit=100"
BILYONER_API = "https://www.bilyoner.com/api/v3/mobile/aggregator/gamelist/events/selections/popular?bulletinType=2&mainPage=false&tabType=1"
_cache = {"at": 0, "data": None}
_lock = threading.Lock()

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bits=[]
        self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ("script","style","noscript"): self.skip+=1
    def handle_endtag(self,tag):
        if tag in ("script","style","noscript") and self.skip:self.skip-=1
        if tag in ("div","li","p","article","h3","h4","br"):self.bits.append("\n")
    def handle_data(self,data):
        if not self.skip:self.bits.append(data)

def parse_bilyoner(html):
    ex=TextExtractor()
    ex.feed(html)
    text=re.sub(r"[ \t]+"," ","".join(ex.bits))
    text=re.sub(r"\n\s*\n+","\n",text)
    # Her bahis kaydının kimliği oynanma sayısıyla belirlenir; yoksa kayıt üretme.
    rx=re.compile(r"(?P<title>[^\n]{5,150}?)\s+(?P<count>\d[\d.,]*)\s*Kez\s*Oynand[ıi]",re.I)
    hits=[]
    for m in rx.finditer(text):
        title=m.group("title").strip(" .:-")
        if not ("-" in title or "–" in title):continue
        count=int(re.sub(r"\D","",m.group("count")))
        if count<1:continue
        following=text[m.end():m.end()+190]
        market=re.search(r"(MS\s*[012X]|\d[,.]5\s*(?:Üst|Alt)|KG\s*(?:Var|Yok))",following,re.I)
        odd=None
        if market:
            after=following[market.end():market.end()+60]
            match=re.search(r"(?<!\d)(\d{1,2}[,.]\d{2})(?!\d)",after)
            if match:
                v=float(match.group(1).replace(",","."))
                if 1.01<=v<=100:odd=v
        # Kaynak başlığında iki takım bulunmadığında sahte eşleştirme yapma.
        hits.append({"source":"Bilyoner","title":title,"count":count,
                     "market":market.group(1) if market else None,
                     "odd":odd,"url":URLS["Bilyoner"]})
    uniq={}
    for h in hits:
        k=(h["title"].casefold(),h["market"])
        if k not in uniq or h["count"]>uniq[k]["count"]:uniq[k]=h
    return sorted(uniq.values(),key=lambda h:h["count"],reverse=True)[:60]

def parse_bilyoner_json(payload):
    """Only accept explicit market/count/odd fields, no inferred betting data."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Beklenmeyen Bilyoner JSON biçimi")
    results = []
    for obj in payload["data"]:
        if not isinstance(obj, dict):
            continue
        market = obj.get("market")
        if not isinstance(market, dict):
            continue
        title = str(obj.get("en") or "").strip()
        selection = str(market.get("n") or "").strip()
        count = market.get("count", obj.get("count"))
        odd = market.get("val")
        try:
            count = int(count)
            odd = float(odd) if odd is not None else None
        except (TypeError, ValueError, OverflowError):
            continue
        if not title or not selection or count <= 0 or (odd is not None and (not math.isfinite(odd) or odd < 1.01 or odd > 1000)):
            continue
        fixture_id = obj.get("id")
        results.append({
            "source": "Bilyoner", "title": title, "count": count,
            "market": selection, "market_name": str(market.get("mrn") or ""),
            "odd": odd, "fixture_id": fixture_id,
            "league": str(obj.get("lgn") or ""),
            "kickoff": str(obj.get("esd") or ""),
            "url": URLS["Bilyoner"]
        })
    # Distinct market selections for same match must remain separate.
    unique = {}
    for row in results:
        key = (str(row["fixture_id"]), row["market_name"], row["market"])
        if key not in unique or row["count"] > unique[key]["count"]:
            unique[key] = row
    return sorted(unique.values(), key=lambda x: x["count"], reverse=True)


def fetch_bilyoner():
    response = requests.get(BILYONER_API, timeout=12, headers={
        "User-Agent": "Mozilla/5.0 (compatible; GolMerkeziRadar/1.1)",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bilyoner.com/iddaa/populer-bahisler",
    })
    response.raise_for_status()
    return parse_bilyoner_json(response.json())



def parse_iddaa_json(payload):
    """iddaa futbol popüler bahisleri. Yalnızca açıkça dönen kayıtları kullan."""
    if not isinstance(payload,dict) or payload.get("isSuccess") is not True:
        raise ValueError("iddaa API başarısız yanıt")
    data=payload.get("data")
    if not isinstance(data,dict) or not isinstance(data.get("1"),list):
        raise ValueError("iddaa futbol veri biçimi beklenenden farklı")
    result=[]
    for item in data["1"]:
        if not isinstance(item,dict) or item.get("isDeleted") is True:
            continue
        title=str(item.get("eventName") or "").strip()
        market_name=str(item.get("marketName") or "").strip()
        outcome=str(item.get("outcomeName") or "").strip()
        try:
            count=int(item.get("totalPlayed"))
            # webOdd sitede gösterilen orandır; odd alanıyla karıştırmayız.
            raw_odd=item.get("webOdd")
            odd=float(raw_odd) if raw_odd is not None else None
        except (TypeError,ValueError,OverflowError):
            continue
        if not title or not market_name or not outcome or count<=0:
            continue
        if odd is not None and (not math.isfinite(odd) or not 1.01<=odd<=1000):
            continue
        label=outcome
        if str(item.get("marketType"))=="1" and market_name.lower().startswith("maç sonucu"):
            label={"1":"MS 1","2":"MS 2","0":"MS 0","X":"MS X"}.get(outcome,outcome)
        elif "Alt" in market_name or "Üst" in market_name:
            line=str(item.get("specialOddsValue") or "").strip()
            if line and outcome in ("Alt","Üst"):
                label=line.replace(".",",")+" "+outcome
        elif "Karşılıklı" in market_name:
            label="KG "+outcome
        result.append({
            "source":"iddaa.com","title":title,"count":count,
            "market":label,"market_name":market_name,
            "odd":odd,"fixture_id":item.get("eventId"),
            "league":str(item.get("competitionName") or ""),
            "kickoff":str(item.get("eventDate") or ""),
            "url":"https://www.iddaa.com/",
            "played_ratio":item.get("playedRatio"),
            "market_key":str(item.get("marketUniqueKey") or "")
        })
    unique={}
    for row in result:
        key=(str(row["fixture_id"]),row["market_name"],row["market"])
        if key not in unique or row["count"]>unique[key]["count"]:
            unique[key]=row
    return sorted(unique.values(),key=lambda r:r["count"],reverse=True)

def fetch_iddaa():
    response=requests.get(IDDAA_API,timeout=12,headers={
        "User-Agent":"Mozilla/5.0 (compatible; GolMerkeziRadar/1.2)",
        "Accept":"application/json, text/plain, */*",
        "Referer":"https://www.iddaa.com/"
    })
    response.raise_for_status()
    return parse_iddaa_json(response.json())

def parse_misli_json(payload):
    """Misli most-played API; percentage is shown as a percentage, never a count."""
    if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("data"), list):
        raise ValueError("Beklenmeyen Misli JSON biçimi")
    result = []
    for item in payload["data"]:
        if not isinstance(item, dict) or item.get("sportType") != "SOCCER":
            continue
        home, away = str(item.get("homeTeam") or "").strip(), str(item.get("awayTeam") or "").strip()
        outcome = str(item.get("mostPlayedOutcome") or "").strip()
        if not home or not away or not outcome:
            continue
        try:
            odd = float(item["odd"])
            percentage = float(item["percentage"])
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        if not (math.isfinite(odd) and 1.01 <= odd <= 1000 and math.isfinite(percentage) and 0 <= percentage <= 100):
            continue
        market_type = str(item.get("marketType") or "")
        sub_type = str(item.get("marketSubType") or "")
        if market_type == "1" and sub_type == "1":
            label = {"1": "MS 1", "2": "MS 2", "0": "MS 0", "X": "MS X"}.get(outcome, outcome)
            market_name = "Maç Sonucu"
        elif market_type == "2" and sub_type == "101":
            line = item.get("specialOddsValue")
            if line is None:
                continue  # Avoid inventing the goal line.
            label = str(line).replace(".", ",") + " " + outcome
            market_name = "Altı/Üstü " + str(line).replace(".", ",")
        elif market_type == "2" and sub_type == "89":
            label = "KG " + outcome
            market_name = "Karşılıklı Gol"
        else:
            label = outcome
            market_name = "Market " + market_type + "/" + sub_type
        result.append({
            "source": "Misli", "title": home + " - " + away,
            "count": None, "percentage": percentage, "market": label,
            "market_name": market_name, "odd": odd,
            "fixture_id": item.get("eventId"), "betradar_id": item.get("bri"),
            "market_id": item.get("marketId"), "outcome_no": item.get("outcomeNo"),
            "kickoff": item.get("dateTime"), "league": "", "url": URLS["Misli"]
        })
    unique = {}
    for row in result:
        key = (str(row["fixture_id"]), str(row["market_id"]), str(row["outcome_no"]))
        if key not in unique or row["percentage"] > unique[key]["percentage"]:
            unique[key] = row
    return sorted(unique.values(), key=lambda r: r["percentage"], reverse=True)


def fetch_misli():
    response = requests.get(MISLI_API, timeout=12, headers={
        "User-Agent": "Mozilla/5.0 (compatible; GolMerkeziRadar/1.3)",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.misli.com/"
    })
    response.raise_for_status()
    return parse_misli_json(response.json())


def parse_nesine_json(payload):
    """Nesine prebet records. C is count; MIO/MAO are not confirmed current odds."""
    if not isinstance(payload, dict) or payload.get("sc") != 200:
        raise ValueError("Nesine API başarısız yanıt")
    outer = payload.get("d")
    items = outer.get("data") if isinstance(outer, dict) else None
    if not isinstance(items, list):
        raise ValueError("Nesine veri biçimi beklenenden farklı")
    results = []
    for item in items:
        if not isinstance(item, dict) or str(item.get("SI")) != "1":
            continue
        title = str(item.get("EN") or "").strip()
        market_name = str(item.get("MN") or "").strip()
        outcome = str(item.get("ONA") or "").strip()
        try:
            count = int(item.get("C"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not title or not market_name or not outcome or count <= 0:
            continue
        if market_name == "Maç Sonucu":
            label = {"1": "MS 1", "2": "MS 2", "0": "MS 0", "X": "MS X"}.get(outcome, outcome)
        elif market_name == "Toplam Gol":
            label = outcome
        elif market_name == "Karşılıklı Gol":
            label = "KG " + outcome
        else:
            label = outcome
        results.append({
            "source": "Nesine", "title": title, "count": count,
            "market": label, "market_name": market_name,
            "odd": None, "fixture_id": item.get("EID"),
            "betradar_id": item.get("BRID"), "market_id": item.get("MID"),
            "outcome_no": item.get("ON"), "league": "", "kickoff": "",
            "url": URLS["Nesine"], "odds_note": "Nesine MIO/MAO güncel oran olarak doğrulanmadı"
        })
    unique = {}
    for row in results:
        key = (str(row["fixture_id"]), str(row["market_id"]), str(row["outcome_no"]))
        if key not in unique or row["count"] > unique[key]["count"]:
            unique[key] = row
    return sorted(unique.values(), key=lambda r: r["count"], reverse=True)


def fetch_nesine():
    response = requests.get(NESINE_API, timeout=12, headers={
        "User-Agent": "Mozilla/5.0 (compatible; GolMerkeziRadar/1.4)",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nesine.com/"
    })
    response.raise_for_status()
    return parse_nesine_json(response.json())


def compare_sources(entries):
    """Fixture and normalized market selection; retain platform-specific metrics."""
    groups = {}
    for row in entries:
        fid = row.get("fixture_id")
        if fid is None:
            continue
        key = str(fid)
        group = groups.setdefault(key, {"fixture_id": fid, "title": row["title"],
            "kickoff": row.get("kickoff"), "league": row.get("league"), "selections": []})
        # Selection names are normalized by parsers (e.g. MS 2 / 2,5 Üst).
        selection_key = str(row.get("market") or "").casefold().strip()
        match = next((x for x in group["selections"] if x["_key"] == selection_key), None)
        if match is None:
            match = {"_key": selection_key, "market": row.get("market"),
                "market_name": row.get("market_name"), "sources": []}
            group["selections"].append(match)
        match["sources"].append({"name": row["source"], "count": row.get("count"),
            "percentage": row.get("percentage"), "odd": row.get("odd")})
    for group in groups.values():
        for selection in group["selections"]:
            selection.pop("_key")
            selection["platform_count"] = len({r["name"] for r in selection["sources"]})
        group["selections"].sort(key=lambda r: (-r["platform_count"],
            -max((s.get("count") or 0) for s in r["sources"])))
        group["platform_count"] = len({r["name"] for sel in group["selections"] for r in sel["sources"]})
        group["max_popularity"] = max((s.get("count") or 0) for sel in group["selections"] for s in sel["sources"])
    return sorted(groups.values(), key=lambda g: (-g["platform_count"], -g["max_popularity"]))

def get_radar(force=False):
    with _lock:
        if not force and _cache["data"] is not None and time.time() - _cache["at"] < 600:
            return _cache["data"]
        result = {"updated": datetime.now(timezone.utc).isoformat(), "entries": [], "sources": []}
        for name,url in URLS.items():
            if name not in ("Bilyoner","iddaa.com","Misli","Nesine"):
                result["sources"].append({"name":name,"status":"Henüz otomatik veri bağlantısı doğrulanmadı","url":url})
                continue
            try:
                rows={"Bilyoner":fetch_bilyoner,"iddaa.com":fetch_iddaa,"Misli":fetch_misli,"Nesine":fetch_nesine}[name]()
                result["entries"].extend(rows)
                status=f"{len(rows)} popüler futbol tercihi alındı" if rows else "API kullanılabilir kayıt döndürmedi"
            except (requests.RequestException,ValueError,TypeError) as exc:
                status="API erişimi veya veri biçimi hatası: "+str(exc)[:120]
            result["sources"].append({"name":name,"status":status,"url":url})
        result["comparisons"]=compare_sources(result["entries"])
        _cache.update(at=time.time(), data=result)
        return result

@radar_bp.get("/api/radar/populer")
def radar_api():
    return jsonify(get_radar(force=request.args.get("refresh") == "1"))

RADAR_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gol Merkezi | Popüler Bahis Radarı</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 10% 0%,#173653 0%,#091524 55%,#070e18 100%);color:#e8f2ff}
main{max-width:1120px;margin:auto;padding:28px 18px 65px}
header,.panel{border:1px solid #294763;background:#10263acc;border-radius:18px;padding:22px;margin-bottom:17px}
header{display:flex;justify-content:space-between;align-items:center;gap:20px}
h1{margin:0 0 8px;font-size:clamp(23px,4vw,32px)}h2{font-size:18px;margin:0 0 14px}
p{color:#aac0d2;line-height:1.5;margin:5px 0}a{color:#74d9c1;text-decoration:none}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
button,input{border-radius:9px;padding:11px 13px;border:1px solid #385571;background:#0c1d30;color:#eef6ff;font:inherit}
button{cursor:pointer;background:#087e68;border-color:#21a88a;font-weight:700}
button:hover{background:#0d967b}input{flex:1;min-width:190px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:12px}
.source,.bet{padding:15px;background:#0b1b2d;border:1px solid #2b4259;border-radius:12px}
.bet strong{display:block;font-size:16px;margin-bottom:7px}
.small{font-size:12px;color:#aac0d2}.count{font-weight:800;color:#ffce6b}
.tag{display:inline-block;padding:4px 9px;border-radius:20px;background:#183b4a;color:#7ee5d0;margin:3px 5px 0 0;font-size:12px}
.warning{color:#ffce8a}.empty{padding:20px;border:1px dashed #52677a;border-radius:12px;color:#a9c0d1}
/* GOL MERKEZI PRO 2.0 — radar workspace */
body{padding-left:254px!important;background:radial-gradient(ellipse at 80% 0%,rgba(21,103,133,.25),transparent 60%),linear-gradient(135deg,#050c18,#091a2b 52%,#070f1e)!important}
.pro-sidebar{position:fixed;z-index:10000;left:0;top:0;bottom:0;width:254px;display:flex;flex-direction:column;padding:27px 15px 20px;background:linear-gradient(175deg,#0d1f31,#06101e);border-right:1px solid rgba(97,169,198,.24);box-shadow:15px 0 60px #0004}
.pro-identity{display:flex;align-items:center;gap:11px;color:#f1f8ff!important;text-decoration:none;font-size:15px;font-weight:900;letter-spacing:.045em;padding:4px 7px 30px;line-height:1.3}
.pro-identity strong{display:block;font-size:10px;letter-spacing:.2em;color:#45e7d4;margin-top:4px}
.pro-logo{width:43px;height:43px;flex:none;display:grid;place-items:center;border-radius:14px;background:linear-gradient(135deg,#125c71,#0a294b);font-size:23px}
.pro-section-label{font-size:10px;letter-spacing:.18em;color:#6f93a9;font-weight:800;padding:0 12px 12px}
.pro-sidebar>a:not(.pro-identity){display:flex;align-items:center;gap:12px;padding:15px 13px;border-radius:12px;margin-bottom:8px;text-decoration:none;font-size:13px;font-weight:750;color:#aec6d9;border:1px solid transparent}
.pro-sidebar>a.active:not(.pro-identity){background:linear-gradient(100deg,#24c6b533,#1e638221);color:#5df2db;border:1px solid #40ddc544;box-shadow:inset 3px 0 #3be3ca}
.pro-sidebar-bottom{margin-top:auto;padding:17px 10px 4px;border-top:1px solid #5fa7c733;font-size:10px;letter-spacing:.12em;color:#b2d0df;font-weight:800}
.pro-sidebar-bottom small{display:block;color:#7290a5;letter-spacing:0;margin-top:8px;font-weight:500}
.pro-pulse{display:inline-block;width:8px;height:8px;border-radius:50%;background:#42e3a4;box-shadow:0 0 12px #42e3a4;margin-right:6px}
main{max-width:1600px!important;padding:34px 36px 65px!important}
header,.panel{background:linear-gradient(145deg,#11283dea,#091627f0)!important;border:1px solid #5fa7c733!important;border-radius:18px!important;padding:25px!important;box-shadow:0 16px 45px #0003!important}
header h1{font-size:clamp(25px,2.6vw,38px)!important;letter-spacing:-.04em}
.source,.bet{background:#071a2dcc!important;border:1px solid #5fa7c733!important;border-radius:15px!important;padding:20px!important}
.grid{gap:16px!important}button{background:linear-gradient(110deg,#118e7b,#0c6e83)!important;border:1px solid #5df0d255!important}
@media(max-width:760px){body{padding-left:0!important;padding-top:69px!important}.pro-sidebar{width:100%;height:69px;bottom:auto;flex-direction:row;align-items:center;padding:9px 12px;gap:5px;border-right:0;border-bottom:1px solid #5fa7c733}.pro-identity{padding:0;margin-right:auto;font-size:11px;gap:6px}.pro-identity strong{font-size:8px}.pro-logo{width:33px;height:33px;font-size:17px}.pro-section-label,.pro-sidebar-bottom{display:none}.pro-sidebar>a:not(.pro-identity){font-size:0;padding:10px 9px;margin:0;gap:0}.pro-sidebar>a:not(.pro-identity) span{display:none}main{padding:15px 11px 40px!important}header,.panel{padding:18px!important}}

/* PRO 3.0: premium atmosphere and readable market cards */
body{background:radial-gradient(ellipse 80% 55% at 72% -12%,rgba(0,194,190,.25),transparent 65%),radial-gradient(ellipse 55% 65% at 14% 76%,rgba(28,84,174,.24),transparent 70%),linear-gradient(130deg,#030916 0%,#071c32 48%,#020713 100%)!important;background-attachment:fixed!important}
main{position:relative;isolation:isolate}
main:before{content:"";position:fixed;z-index:-1;pointer-events:none;top:80px;right:6%;width:38vw;height:38vw;border-radius:50%;background:radial-gradient(circle,rgba(18,193,190,.11),transparent 70%);filter:blur(18px)}
header,.panel{background:linear-gradient(130deg,rgba(15,40,66,.94),rgba(5,16,34,.92))!important;border:1px solid rgba(85,199,218,.22)!important;box-shadow:0 22px 70px rgba(0,0,0,.34),inset 0 1px rgba(255,255,255,.04)!important}
header{border-top:1px solid rgba(83,237,215,.46)!important}
#comparisons.grid{grid-template-columns:repeat(3,minmax(0,1fr))!important}
#results.grid,#botResults.grid{grid-template-columns:repeat(3,minmax(0,1fr))!important}
#sources.grid{grid-template-columns:repeat(4,minmax(0,1fr))!important}
.bet,.source{min-width:0;overflow-wrap:anywhere;box-shadow:0 10px 30px rgba(0,0,0,.17)}
.bet strong{line-height:1.45}
.radar-tabs{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.radar-tabs button{flex:1;min-width:210px;padding:16px!important;background:rgba(12,35,58,.85)!important;color:#a8c4d7!important;border:1px solid rgba(91,165,194,.35)!important;text-align:left}
.radar-tabs button.active{background:linear-gradient(110deg,rgba(19,155,155,.40),rgba(17,79,124,.5))!important;color:#f2ffff!important;border-color:#38ddc6!important;box-shadow:inset 0 0 0 1px rgba(56,221,198,.15),0 8px 26px rgba(0,196,177,.13)}
.radar-tabs button:focus-visible{outline:2px solid #62f1dc;outline-offset:3px}
#botArea .panel{border-color:rgba(67,204,189,.32)!important}
@media(max-width:1280px){#comparisons.grid,#results.grid,#botResults.grid{grid-template-columns:repeat(2,minmax(0,1fr))!important}#sources.grid{grid-template-columns:repeat(2,minmax(0,1fr))!important}}
@media(max-width:760px){#comparisons.grid,#results.grid,#botResults.grid,#sources.grid{grid-template-columns:1fr!important}.radar-tabs button{min-width:0;flex-basis:100%}}
</style></head><body><aside class="pro-sidebar" aria-label="Ana navigasyon"><a class="pro-identity" href="/"><span class="pro-logo">⚽</span><span>GOL MERKEZİ<strong>PRO ANALYTICS</strong></span></a><div class="pro-section-label">KONTROL MERKEZİ</div><a href="/">◉ <span>Canlı Gol Merkezi</span></a><a href="/tahmin">▦ <span>Maç Analizi & Kupon</span></a><a href="/radar" class="active">◈ <span>Piyasa Radarı</span></a><div class="pro-sidebar-bottom"><span class="pro-pulse"></span> ANALİZ PLATFORMU <small>Gol Merkezi • Pro Workspace</small></div></aside><main>
<header><div><h1>📊 Popüler Bahis Radarı</h1><p>Halkın tercihleri • Kaynaklı veriler • Bağımsız değerlendirme</p></div><a href="/">← Canlı Gol Merkezi</a></header>
<section class="panel"><div class="radar-tabs" role="tablist" aria-label="Radar bölümleri"><button id="tabPopular" class="active" role="tab" aria-selected="true" onclick="showTab('popular')">📊 Popüler Bahisler</button><button id="tabBot" role="tab" aria-selected="false" onclick="showTab('bot')">🤖 Bot Analizleri ve Kuponlar</button></div><p>Bot analizleri yalnızca API-Football ile eşleşen maçlarda, istek üzerine çalışır.</p></section>
<div id="popularArea"><section class="panel"><h2>Veri kaynakları</h2><div id="sources" class="grid">Kaynaklar kontrol ediliyor…</div></section>
<section class="panel"><h2>Platform karşılaştırması · Aynı maçın tercihleri</h2>
<p>Platformlar aynı tercihi listeliyorsa ayrı ayrı görünür. Misli yüzdeleri ile diğer platformların oynanma sayıları birleştirilmez. Nesine oranları doğrulanana kadar gösterilmez.</p>
<div id="comparisons" class="grid"></div></section>
<section class="panel"><div class="row"><h2 style="margin:0;flex:1">En çok oynanan tercihler</h2><button id="refresh">Yenile</button></div>
<p>Oynanma sayıları Bilyoner/iddaa.com/Nesine kaynaklarına, yüzdeler Misli kaynağına aittir. Popülerlik kazanma olasılığı değildir.</p>
<div class="row" style="margin:14px 0"><input id="search" placeholder="Takım veya market ara…"></div>
<div id="results" class="grid"></div><p id="updated" class="small"></p></section>
</div><div id="botArea" style="display:none"><section class="panel"><div class="row"><h2 style="margin:0;flex:1">🤖 Bağımsız Bot Analizleri</h2><button id="analyzeBtn" onclick="runAnalysis()">Maçları Analiz Et</button></div><p>Bot yalnızca Bilyoner, iddaa.com, Misli ve Nesine popüler listelerindeki tercihleri değerlendirir. Başka bahis üretmez. Kadro/haber doğrulaması bu sürümde yoktur.</p><p id="radarProgress" class="small" aria-live="polite">Analiz henüz başlamadı.</p><div id="botResults" class="grid"><div class="empty">Analizi başlatmak için butona bas.</div></div></section><section class="panel"><h2>🎟️ Bot Kuponu</h2><p>Yalnızca radarın popüler tercihleri kullanılır. En az 3 farklı maç, her seçimde doğrulanmış 1.30+ oran. %60+ tercihler önceliklidir; %50–60 tercihler de kupona girebilir. Oran avantajı zorunlu değildir; yeterli aday yoksa kupon oluşturulmaz.</p><div id="couponResult" class="empty">Henüz analiz yapılmadı.</div></section></div><section class="panel"><h2>Bot nasıl yorumluyor?</h2>
<p>Bot son maç formunu ve ev/deplasman performansını kalibre edilmemiş Poisson gol modeliyle yorumlar. %60+ öncelikli, %50–60 değerlendirilebilir ve %50 altı riskli seçenekleri ayırır. Oranın gerektirdiği başa baş olasılığı açıklar; oran avantajı tek başına kupon engeli değildir. Kadro, sakatlık ve haber kontrolü bu hafif sürümde yapılmaz; model yüzdeleri gerçek başarı garantisi değildir.</p>
<p class="warning">1.30 altındaki oranlar tekli bahis önerisi değildir. Popüler seçimler otomatik kupona eklenmez.</p></section>
</main><script>
function showTab(which){document.getElementById('popularArea').style.display=which==='popular'?'':'none';document.getElementById('botArea').style.display=which==='bot'?'':'none';for(const [id,active] of [['tabPopular',which==='popular'],['tabBot',which==='bot']]){const b=document.getElementById(id);b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active));}}
async function radarJSON(url){const r=await fetch(url);const ct=r.headers.get('content-type')||'';if(!ct.includes('application/json'))throw Error('Sunucu JSON yerine '+r.status+' döndürdü. Render loglarını kontrol edin.');const d=await r.json();if(!r.ok)throw Error(d.error||('HTTP '+r.status));return d;}
function renderCoupon(analyses){const eligible=[];for(const a of analyses){const options=(a.picks||[]).filter(p=>p.eligible);options.sort((x,y)=>(y.model_probability??0)-(x.model_probability??0));if(options.length)eligible.push({...options[0],title:a.title,fixture_id:a.fixture_id});}const el=document.getElementById('couponResult');if(eligible.length<3){el.textContent='Kupon oluşturulmadı: Popüler listedeki tercihlerden en az 3 farklı maçta doğrulanmış 1.30+ oran ve %50+ model olasılığı bulunamadı. ('+eligible.length+'/3)';return;}eligible.sort((a,b)=>(b.model_probability??0)-(a.model_probability??0));const legs=eligible.slice(0,3);const total=legs.reduce((x,p)=>x*p.odd,1);el.innerHTML=legs.map(x=>'<p>'+esc(x.title)+' — '+esc(x.market)+' ('+Number(x.odd).toFixed(2)+')</p>').join('')+'<p>Toplam oran: '+total.toFixed(2)+' · Sanal tutar: 1.000 TL · Olası dönüş: '+(total*1000).toLocaleString('tr-TR')+' TL</p><p class="warning">Taslak kupon. Oranlar değişebilir, kazanç garanti değildir.</p>';}
function renderAnalysis(a){return `<article class="bet"><strong>${esc(a.title)}</strong><p class="small">Veri kalitesi: ${esc(a.data_quality||'bilinmiyor')} · ${esc(a.lineup_note||'')}</p>${a.error?`<p class="warning">${esc(a.error)}</p>`:(a.picks||[]).map((p,i)=>`<div style="border-top:1px solid #294763;padding:12px 0"><div class="row" style="justify-content:space-between"><b>${i+1}. ${esc(p.market)}</b><span class="tag">${esc(p.decision)}</span></div><p class="small">Model: <b>%${(100*p.model_probability).toFixed(1)}</b> · Oran: <b>${p.odd?Number(p.odd).toFixed(2):'doğrulanmadı'}</b> ${esc(p.bookmaker||'')} ${p.break_even_pct!=null?'· Başa baş: %'+Number(p.break_even_pct).toFixed(1):''}</p><p>${esc(p.reason||'')}</p><p class="small">Radarın popüler tercihlerinden · ${p.eligible?'Kupon adayı':'Otomatik kupon dışında'}</p></div>`).join('')}</article>`;}
async function runAnalysis(){
 const btn=document.getElementById('analyzeBtn'),out=document.getElementById('botResults');
 btn.disabled=true;
 const analyses=[],jobs=[],seen=new Set(),warnings=[];
 const progress=document.getElementById('radarProgress');
 let done=0,success=0,failed=0;
 function updateProgress(message){
   progress.textContent='İlerleme: '+done+'/'+jobs.length+' maç · Başarılı: '+success+' · Hatalı/atlanan: '+failed+(message?' · '+message:'');
 }
 out.innerHTML='<div class="empty">Popüler maçlar fikstürle eşleştiriliyor…</div>';
 progress.textContent='Fikstür hazırlanıyor…';
 try{
   // Separate small HTTP requests keep each analysis within Render Free limits.
   // Scan the next seven days; do not stop after five or six matches.
   for(let day=0;day<7;day++){
     try{
       const d=await radarJSON('/api/radar/analiz?phase=prepare&day='+day);
       if(d.warning)warnings.push((day+1)+'. gün: '+d.warning);
       for(const m of d.matches||[]){
         const key=String(m.fixture_id);
         if(!seen.has(key)){seen.add(key);jobs.push(m);}
       }
     }catch(e){warnings.push((day+1)+'. gün fikstürü: '+e.message);}
     progress.textContent='Fikstür taranıyor: '+(day+1)+'/7 gün · Eşleşen: '+jobs.length+' maç';
   }
   out.innerHTML='';
   if(!jobs.length){
     out.innerHTML='<div class="empty">Popüler listeden API-Football ile eşleşen maç bulunamadı. Kupon oluşturulmadı.</div>';
   }
   for(const m of jobs){
     updateProgress('Şimdi: '+m.title);
     try{
       const r=await radarJSON('/api/radar/analiz?phase=match&day='+encodeURIComponent(m.day)+'&fixture='+encodeURIComponent(m.fixture_id));
       if(!r.analysis)throw Error('Analiz yanıtı eksik');
       analyses.push(r.analysis);success++;
     }catch(e){
       failed++;
       analyses.push({fixture_id:m.fixture_id,title:m.title,picks:[],error:'Analiz yapılamadı: '+e.message});
     }
     done++;
     out.innerHTML=analyses.map(renderAnalysis).join('');
     updateProgress();
     renderCoupon(analyses);
     // Yield to the browser so progress and cards render between requests.
     await new Promise(resolve=>setTimeout(resolve,0));
   }
   if(warnings.length){
     const box=document.createElement('p');box.className='warning';
     box.textContent='Fikstür uyarıları: '+warnings.join(' | ');
     out.appendChild(box);
   }
   updateProgress('Tarama tamamlandı. Fikstürle eşleşmeyen popüler maçlar için analiz üretilemez.');
   renderCoupon(analyses);
 }catch(e){
   out.insertAdjacentHTML('beforeend','<p class="warning">Analiz hatası: '+esc(e.message)+'</p>');
 }finally{btn.disabled=false;}
}

const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let entries=[],comparisons=[];
function drawComparisons(){
const q=document.getElementById("search").value.toLocaleLowerCase("tr");
const filtered=comparisons.filter(g=>(g.title+" "+g.selections.map(s=>s.market).join(" ")).toLocaleLowerCase("tr").includes(q));
document.getElementById("comparisons").innerHTML=filtered.length?filtered.map(g=>`<article class="bet">
<strong>${esc(g.title)}</strong><p class="small">${esc(g.league||"")} · ${g.platform_count} platformda listeleniyor</p>
${g.selections.map(sel=>`<div style="padding:9px 0;border-top:1px solid #294763">
<b>${esc(sel.market)}</b> <span class="small">(${sel.platform_count} platform)</span>
${sel.sources.map(a=>`<div class="small">${esc(a.name)}: <span class="count">${a.count!=null?Number(a.count).toLocaleString("tr-TR")+" oynanma":a.percentage!=null?"%"+Number(a.percentage).toLocaleString("tr-TR",{maximumFractionDigits:3})+" popülerlik":"veri yok"}</span> · oran ${a.odd?Number(a.odd).toFixed(2):"doğrulanmadı"}</div>`).join("")}
</div>`).join("")}</article>`).join("")
:'<div class="empty">Karşılaştırılabilir maç bulunamadı.</div>';
}
function draw(){drawComparisons();
const q=document.getElementById("search").value.toLocaleLowerCase("tr");
const filtered=entries.filter(e=>(e.title+" "+(e.market||"")).toLocaleLowerCase("tr").includes(q));
document.getElementById("results").innerHTML=filtered.length?filtered.map(e=>`<article class="bet"><strong>${esc(e.title)}</strong>
<span class="count">${e.count!=null?Number(e.count).toLocaleString("tr-TR")+" kez oynandı":e.percentage!=null?"%"+Number(e.percentage).toLocaleString("tr-TR",{maximumFractionDigits:3})+" popülerlik":"Popülerlik bilgisi yok"}</span><div>
${e.market?`<span class="tag">${esc(e.market)}</span>`:""}${e.odd?`<span class="tag">Gösterilen oran ${Number(e.odd).toFixed(2)}</span>`:""}
</div><p class="small">Kaynak: ${esc(e.source)} · <a href="${esc(e.url)}" target="_blank" rel="noopener">Kaynağa git ↗</a></p></article>`).join("")
:'<div class="empty">Eşleşen doğrulanmış popüler bahis verisi bulunamadı. Tahmini rakam gösterilmiyor.</div>';
}
async function load(force=false){
document.getElementById("results").innerHTML='<div class="empty">Veriler yükleniyor…</div>';
try{const r=await fetch("/api/radar/populer"+(force?"?refresh=1":""));if(!r.ok)throw Error("Sunucu "+r.status);
const d=await r.json();entries=d.entries||[];comparisons=d.comparisons||[];
document.getElementById("sources").innerHTML=(d.sources||[]).map(s=>`<div class="source"><strong>${esc(s.name)}</strong><p class="small">${esc(s.status)}</p><a href="${esc(s.url)}" target="_blank" rel="noopener">Siteyi aç ↗</a></div>`).join("");
document.getElementById("updated").textContent="Son veri kontrolü: "+new Date(d.updated).toLocaleString("tr-TR");
draw()}catch(e){document.getElementById("results").textContent="Veri hatası: "+e.message}}
document.getElementById("search").oninput=draw;
document.getElementById("refresh").onclick=()=>load(true);load();
</script></body></html>"""



# --- Radar v2: independent prematch analysis bridge (on demand, cached) ---
import unicodedata
from flask import current_app
_analysis_cache = {}
_analysis_lock = threading.Lock()

def _normalized_team(text):
    text = unicodedata.normalize("NFKD", str(text or "").casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", text.replace("ı", "i"))

def _team_similarity(a, b):
    x, y = _normalized_team(a), _normalized_team(b)
    if not x or not y: return False
    return x == y or (min(len(x),len(y)) >= 5 and (x in y or y in x))

def _radar_teams(title):
    bits = re.split(r"\s+[-–]\s+", str(title), maxsplit=1)
    return bits if len(bits) == 2 else (None, None)

def _match_fixture(row, fixtures):
    home, away = _radar_teams(row.get("title"))
    if not home: return None
    matches = [f for f in fixtures if
       _team_similarity(home, ((f.get("teams") or {}).get("home") or {}).get("name")) and
       _team_similarity(away, ((f.get("teams") or {}).get("away") or {}).get("name"))]
    return matches[0] if len(matches) == 1 else None

def _market_code(label):
    x = str(label or "").casefold().replace(".", ",").replace(" ", "")
    mapping = {"ms1":"HOME", "1":"HOME", "ms2":"AWAY", "2":"AWAY",
       "2,5üst":"OVER_2_5", "2,5alt":"UNDER_2_5",
       "3,5üst":"OVER_3_5", "3,5alt":"UNDER_3_5",
       "1,5üst":"OVER_1_5", "kgvar":"BTTS_YES", "kgyok":"BTTS_NO",
       "ev1,5üst":"HOME_OVER_1_5", "dep1,5üst":"AWAY_OVER_1_5"}
    return mapping.get(x)

def _date_for_radar():
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Europe/Istanbul")).date()
    except Exception:
        today = datetime.now(timezone.utc).date()
    return today

def _analysis_for_fixture(fixture, day):
    fid = (fixture.get("fixture") or {}).get("id")
    key = str(fid)
    with _analysis_lock:
        cached = _analysis_cache.get(key)
        if cached and time.time()-cached[0] < 900: return cached[1]
    # Use existing /api/prematch/analyze engine; never alter the live scanner.
    fn = current_app.view_functions.get("api_prematch_analyze")
    if fn is None: raise ValueError("Mevcut maç önü analiz servisi bulunamadı")
    with current_app.test_request_context("/api/prematch/analyze", query_string={"date":day.isoformat(),"fixture":fid}):
        response = fn()
    status = response[1] if isinstance(response, tuple) else 200
    response = response[0] if isinstance(response, tuple) else response
    data = response.get_json()
    if status != 200: raise ValueError(data.get("error") or "Analiz başarısız")
    with _analysis_lock:
        _analysis_cache[key] = (time.time(), data)
        if len(_analysis_cache)>80:
            oldest=min(_analysis_cache,key=lambda k:_analysis_cache[k][0]);_analysis_cache.pop(oldest,None)
    return data

# Free-tier: each request does one bounded unit of work, never six full analyses.
from datetime import timedelta
from zoneinfo import ZoneInfo
from flask import current_app

def _module():
    import sys
    return sys.modules.get("app") or sys.modules.get("__main__")

def _api_form(mod, team_id, kickoff):
    """Fetch last 30 games with a short per-call deadline; compute true 8-game form."""
    if not mod.API_KEY:
        raise ValueError("API_KEY tanımlı değil")
    try:
        response = requests.get(mod.API_BASE + "/fixtures", params={"team":team_id,"last":30,"timezone":"Europe/Istanbul"},
            headers={"x-apisports-key":mod.API_KEY}, timeout=(2,5))
        response.raise_for_status()
        payload=response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ValueError("Takım formu alınamadı: " + str(exc)[:110])
    if payload.get("errors"):
        raise ValueError("API-Football: " + str(payload["errors"])[:100])
    finished=[]
    for item in payload.get("response") or []:
        fix=item.get("fixture") or {}; sides=item.get("teams") or {}; goals=item.get("goals") or {}
        if (fix.get("status") or {}).get("short") not in ("FT","AET","PEN") or (fix.get("date") or "") >= kickoff:
            continue
        home=(sides.get("home") or {}).get("id")==team_id
        away=(sides.get("away") or {}).get("id")==team_id
        if not (home or away) or goals.get("home") is None or goals.get("away") is None: continue
        a=int(goals["home" if home else "away"]); b=int(goals["away" if home else "home"])
        finished.append({"date":fix.get("date") or "","venue":"home" if home else "away","scored":a,"conceded":b,
            "win":a>b,"draw":a==b,"btts":a>0 and b>0,"over15":a+b>=2,"over25":a+b>=3,
            "under25":a+b<=2,"over35":a+b>=4,"under35":a+b<=3,"scored2":a>=2,
            "ht_scored":None,"ht_conceded":None})
    finished.sort(key=lambda r:r["date"],reverse=True)
    recent=finished[:8]
    if len(recent)<5: return {"count":len(recent),"insufficient":True}
    return {"count":len(recent),"all":mod._prematch_metrics(recent),
        "home":mod._prematch_metrics([r for r in finished if r["venue"]=="home"][:8]),
        "away":mod._prematch_metrics([r for r in finished if r["venue"]=="away"][:8])}

def _cached_form(mod, team_id, kickoff):
    key="form:"+str(team_id)
    with _analysis_lock:
        cached=_analysis_cache.get(key)
        if cached and time.time()-cached[0]<1800:return cached[1]
    data=_api_form(mod,team_id,kickoff)
    with _analysis_lock:
        _analysis_cache[key]=(time.time(),data)
        if len(_analysis_cache)>60:
            oldest=min(_analysis_cache,key=lambda k:_analysis_cache[k][0]);_analysis_cache.pop(oldest,None)
    return data

def _radar_rows():
    # Popular tab loads these feeds first; cached data avoids 4 network calls per analysis.
    if _cache.get("data") is None:
        return None
    return _cache["data"].get("entries") or []

@radar_bp.get("/api/radar/analiz")
def radar_analysis():
    try:
        phase=request.args.get("phase", "prepare")
        mod=_module()
        if not mod or not hasattr(mod,"prematch_fixtures"):
            return jsonify({"error":"Maç önü servisleri yüklenmedi"}),503
        rows=_radar_rows()
        if rows is None:
            return jsonify({"error":"Önce Popüler Bahisler sekmesinde verilerin yüklenmesini bekleyin."}),409
        if phase=="prepare":
            offset=max(0,min(6,int(request.args.get("day","0"))))
            day=datetime.now(ZoneInfo("Europe/Istanbul")).date()+timedelta(days=offset)
            # One day only; next day is a separate HTTP request.
            fixtures,error=mod.prematch_fixtures(day)
            if error:return jsonify({"day":day.isoformat(),"matches":[],"warning":str(error)[:150]})
            groups={}
            for row in rows:
                if _market_code(row.get("market")):
                    groups.setdefault(_normalized_team(row.get("title")),[]).append(row)
            matches=[];seen=set()
            for group in groups.values():
                fixture=_match_fixture(group[0],fixtures or [])
                if fixture is None:continue
                fid=(fixture.get("fixture") or {}).get("id")
                if fid in seen:continue
                seen.add(fid)
                matches.append({"fixture_id":fid,"day":day.isoformat(),"title":group[0]["title"]})
            return jsonify({"day":day.isoformat(),"matches":matches})
        if phase!="match":return jsonify({"error":"Geçersiz analiz aşaması"}),400
        fid=int(request.args.get("fixture","0"))
        day=mod.prematch_day(request.args.get("day",""))
        if not day or fid<=0:return jsonify({"error":"Geçersiz maç veya tarih"}),400
        fixtures,error=mod.prematch_fixtures(day)
        if error:return jsonify({"error":str(error)[:150]}),503
        fixture=next((f for f in fixtures or [] if (f.get("fixture") or {}).get("id")==fid),None)
        if not fixture:return jsonify({"error":"Maç artık fikstürde bulunmuyor"}),404
        teams=fixture.get("teams") or {}; h=teams.get("home") or {}; a=teams.get("away") or {}
        kickoff=(fixture.get("fixture") or {}).get("date") or ""
        home=_cached_form(mod,int(h["id"]),kickoff)
        away=_cached_form(mod,int(a["id"]),kickoff)
        from prematch_intelligence import analyze_match
        model=analyze_match(home,away,{})
        markets={m["market"]:m for m in model["markets"]}
        title=h.get("name","")+" - "+a.get("name","")
        matching=[r for r in rows if _match_fixture(r,[fixture]) and _market_code(r.get("market")) in markets]
        picks=[]
        # Only evaluate actual popular selections with a corresponding model market.
        # Never create a market from the model's full catalogue.
        by_market={}
        for row in matching:
            code=_market_code(row.get("market"))
            if code in markets:
                by_market.setdefault(code,[]).append(row)
        for code, rlist in by_market.items():
            item=markets[code]
            priced=[]
            for r in rlist:
                if r.get("source")=="Nesine":
                    continue  # Nesine MIO/MAO semantics have not been verified.
                try:
                    value=float(r.get("odd"))
                except (TypeError,ValueError,OverflowError):
                    continue
                if math.isfinite(value) and 1.30<=value<=100:
                    priced.append((value,r))
            best_odd,best=max(priced,key=lambda pair:pair[0]) if priced else (None,None)
            prob=float(item["model_probability"])
            edge=(prob*best_odd-1) if best_odd else None
            # A moderate negative price edge is allowed as an explicitly labelled
            # speculative selection, rather than silently rejecting every pick.
            # Never bypass the verified 1.30 minimum or data-quality check.
            if model["data_quality"]!="good":
                decision="Veri yetersiz"; level="riskli"
            elif best is None:
                decision="1.30+ doğrulanmış oran yok"; level="bilgi"
            elif prob>=0.60:
                decision="Öncelikli kupon adayı"; level="guclu"
            elif prob>=0.50:
                decision="Değerlendirilebilir (risk içerir)"; level="degerlendirilebilir"
            else:
                decision="Riskli / kupon dışı"; level="riskli"
            qualified=best is not None and level in ("guclu","degerlendirilebilir")
            label=next((r.get("market") for r in rlist if r.get("market")),item.get("label") or code)
            lh=(model.get("scenario") or {}).get("home_expected_goals")
            la=(model.get("scenario") or {}).get("away_expected_goals")
            explanation=(f"Son maç verilerinden ev sahibi için {lh}, deplasman için {la} gol beklentisi hesaplandı. "
                if lh is not None and la is not None else "")
            explanation+=f"Popüler listede bulunan {label} tercihinin model olasılığı %{prob*100:.1f}. "
            if best:
                explanation+=(f"{best['source']} sitesinde görülen oran {best_odd:.2f}; bu oran için başa baş eşik yaklaşık %{100/best_odd:.1f}. ")
                if edge>=0:
                    explanation+="Model olasılığı, oranın başa baş eşiğinin üzerinde; fiyat açısından olumlu görünüyor. "
                else:
                    explanation+="Dikkat: Oranın gerektirdiği başarı olasılığı model tahmininden yüksek; fiyat açısından avantaj görünmüyor. Bu bilgi kupona girişi tek başına engellemez. "
            else:
                explanation+="Bu popüler tercih için doğrulanmış en az 1.30 oran bulunamadı. "
            if model["data_quality"]!="good":
                explanation+="Form verisi yeterli olmadığından kupona alınmadı."
            elif qualified:
                explanation+="Kupon havuzuna alındı; sonuç garantisi yoktur."
            else:
                explanation+="Kupon havuzuna alınmadı."
            picks.append({"code":code,"market":label,"source":best["source"] if best else None,
                "model_probability":prob,"reason":explanation,"odd":best_odd,
                "bookmaker":best["source"] if best else None,
                "break_even_pct":round(100/best_odd,1) if best_odd else None,
                "value_edge_pct":round(100*(prob-1/best_odd),1) if best_odd else None,
                "eligible":qualified,"level":level,"decision":decision,
                "popular":True,"listed_sources":sorted({r["source"] for r in rlist})})
        rank={"guclu":0,"degerlendirilebilir":1,"riskli":2,"bilgi":3}
        picks.sort(key=lambda p:(rank.get(p["level"],4), -p["model_probability"]))
        # Show every distinct popular market for this fixture, no invented alternatives.
        return jsonify({"analysis":{"fixture_id":fid,"title":title,"day":day.isoformat(),
            "data_quality":model["data_quality"],"lineup_note":"Kadro/haber kontrolü bu hafif sürümde yapılmadı.",
            "picks":picks,"error":None},"note":"Oranlar radar anlık görüntüsündendir; oynama öncesi yeniden doğrulanmalıdır."})
    except (ValueError,KeyError,TypeError,requests.RequestException) as exc:
        return jsonify({"error":str(exc)[:200]}),503
    except Exception:
        current_app.logger.exception("Radar analiz hatası")
        return jsonify({"error":"Sunucu analiz sırasında beklenmeyen hata verdi. Render logunu kontrol edin."}),500

@radar_bp.get("/radar")
def radar_page():
    return render_template_string(RADAR_HTML)
