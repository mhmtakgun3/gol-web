"""Gol Merkezi Popüler Bahis Radarı — yalnızca kamuya açık veriler.
Bir platform veri sunmuyorsa durum açıkça belirtilir; popülerlik başarı demek değildir.
"""
from __future__ import annotations
from flask import Blueprint, jsonify, render_template_string
from html.parser import HTMLParser
from datetime import datetime, timezone
import re
import requests
import time
import threading

radar_bp = Blueprint("populer_radar", __name__)
URLS = {
 "Bilyoner": "https://www.bilyoner.com/iddaa/populer-bahisler",
 "Misli": "https://www.misli.com/iddaa/anlik-bahis",
 "Nesine": "https://www.nesine.com/",
 "iddaa.com": "https://www.iddaa.com/",
}
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

def get_radar(force=False):
    with _lock:
        if not force and _cache["data"] is not None and time.time()-_cache["at"]<600:
            return _cache["data"]
        result={"updated":datetime.now(timezone.utc).isoformat(),"entries":[],
                "sources":[]}
        for name,url in URLS.items():
            if name!="Bilyoner":
                result["sources"].append({"name":name,"status":"Henüz otomatik veri bağlantısı doğrulanmadı",
                                          "url":url})
                continue
            try:
                r=requests.get(url,timeout=12,headers={"User-Agent":"Mozilla/5.0 (compatible; GolMerkeziRadar/1.0)"})
                r.raise_for_status()
                entries=parse_bilyoner(r.text)
                result["entries"]=entries
                result["sources"].append({"name":name,
                   "status":f"{len(entries)} kayıt okundu" if entries else "Veri okunamadı; site yapısı değişmiş olabilir",
                   "url":url})
            except requests.RequestException as exc:
                result["sources"].append({"name":name,"status":"Erişim hatası: "+str(exc)[:120],"url":url})
        _cache.update(at=time.time(),data=result)
        return result

@radar_bp.get("/api/radar/populer")
def radar_api():
    return jsonify(get_radar())

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
<section class="panel"><div class="row"><h2 style="margin:0;flex:1">En çok oynanan tercihler</h2><button id="refresh">Yenile</button></div>
<p>Oynanma sayısı yalnızca ilgili platforma aittir; farklı sitelerin rakamları toplanmaz. Popülerlik kazanma olasılığı değildir.</p>
<div class="row" style="margin:14px 0"><input id="search" placeholder="Takım veya market ara…"></div>
<div id="results" class="grid"></div><p id="updated" class="small"></p></section>
<section class="panel"><h2>Bot nasıl yorumluyor?</h2>
<p>Bu ilk aşamada yalnızca doğrulanabilen popülerlik verisi gösterilir. Model olasılığı veya bağımsız maç tahmini üretilmiş gibi gösterilmez. Sonraki aşamada her maç API-Football fikstürüyle eşleştirilip kadro, form ve haber analizine bağlanacaktır.</p>
<p class="warning">1.30 altındaki oranlar tekli bahis önerisi değildir. Popüler seçimler otomatik kupona eklenmez.</p></section>
</main><script>
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let entries=[];
function draw(){
const q=document.getElementById("search").value.toLocaleLowerCase("tr");
const filtered=entries.filter(e=>(e.title+" "+(e.market||"")).toLocaleLowerCase("tr").includes(q));
document.getElementById("results").innerHTML=filtered.length?filtered.map(e=>`<article class="bet"><strong>${esc(e.title)}</strong>
<span class="count">${Number(e.count).toLocaleString("tr-TR")} kez oynandı</span><div>
${e.market?`<span class="tag">${esc(e.market)}</span>`:""}${e.odd?`<span class="tag">Gösterilen oran ${e.odd.toFixed(2)}</span>`:""}
</div><p class="small">Kaynak: ${esc(e.source)} · <a href="${esc(e.url)}" target="_blank" rel="noopener">Kaynağa git ↗</a></p></article>`).join("")
:'<div class="empty">Eşleşen doğrulanmış popüler bahis verisi bulunamadı. Tahmini rakam gösterilmiyor.</div>';
}
async function load(){
document.getElementById("results").innerHTML='<div class="empty">Veriler yükleniyor…</div>';
try{const r=await fetch("/api/radar/populer");if(!r.ok)throw Error("Sunucu "+r.status);
const d=await r.json();entries=d.entries||[];
document.getElementById("sources").innerHTML=(d.sources||[]).map(s=>`<div class="source"><strong>${esc(s.name)}</strong><p class="small">${esc(s.status)}</p><a href="${esc(s.url)}" target="_blank" rel="noopener">Siteyi aç ↗</a></div>`).join("");
document.getElementById("updated").textContent="Son sorgu: "+new Date(d.updated).toLocaleString("tr-TR");
draw()}catch(e){document.getElementById("results").textContent="Veri hatası: "+e.message}}
document.getElementById("search").oninput=draw;
document.getElementById("refresh").onclick=load;load();
</script></body></html>"""

@radar_bp.get("/radar")
def radar_page():
    return render_template_string(RADAR_HTML)
