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
</style></head><body><main>
<header><div><h1>📊 Popüler Bahis Radarı</h1><p>Halkın tercihleri • Kaynaklı veriler • Bağımsız değerlendirme</p></div><a href="/">← Canlı Gol Merkezi</a></header>
<section class="panel"><h2>Veri kaynakları</h2><div id="sources" class="grid">Kaynaklar kontrol ediliyor…</div></section>
<section class="panel"><h2>Platform karşılaştırması · Aynı maçın tercihleri</h2>
<p>Platformlar aynı tercihi listeliyorsa ayrı ayrı görünür. Misli yüzdeleri ile diğer platformların oynanma sayıları birleştirilmez. Nesine oranları doğrulanana kadar gösterilmez.</p>
<div id="comparisons" class="grid"></div></section>
<section class="panel"><div class="row"><h2 style="margin:0;flex:1">En çok oynanan tercihler</h2><button id="refresh">Yenile</button></div>
<p>Oynanma sayıları Bilyoner/iddaa.com/Nesine kaynaklarına, yüzdeler Misli kaynağına aittir. Popülerlik kazanma olasılığı değildir.</p>
<div class="row" style="margin:14px 0"><input id="search" placeholder="Takım veya market ara…"></div>
<div id="results" class="grid"></div><p id="updated" class="small"></p></section>
<section class="panel"><h2>Bot nasıl yorumluyor?</h2>
<p>Bu ilk aşamada yalnızca doğrulanabilen popülerlik verisi gösterilir. Model olasılığı veya bağımsız maç tahmini üretilmiş gibi gösterilmez. Sonraki aşamada her maç API-Football fikstürüyle eşleştirilip kadro, form ve haber analizine bağlanacaktır.</p>
<p class="warning">1.30 altındaki oranlar tekli bahis önerisi değildir. Popüler seçimler otomatik kupona eklenmez.</p></section>
</main><script>
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

@radar_bp.get("/radar")
def radar_page():
    return render_template_string(RADAR_HTML)
