function bankrollStats(store){
  const all=Object.values(store).flatMap(x=>Array.isArray(x)?x:[]);
  const won=all.filter(c=>c.status==="WON"),lost=all.filter(c=>c.status==="LOST"),open=all.filter(c=>(c.status||"OPEN")==="OPEN");
  const settled=[...won,...lost],stakeOf=c=>Number(c.stake||couponStake);
  const totalStaked=all.reduce((s,c)=>s+stakeOf(c),0);
  const settledStaked=settled.reduce((s,c)=>s+stakeOf(c),0);
  const returned=won.reduce((s,c)=>s+stakeOf(c)*Number(c.total||1),0);
  return {total:all.length,won:won.length,lost:lost.length,open:open.length,totalStaked,returned,
    net:returned-settledStaked,success:settled.length?won.length/settled.length*100:0};
}
function renderBankroll(store){
  const s=bankrollStats(store),netClass=s.net>=0?"profit":"loss",money=n=>`${Number(n||0).toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`;
  document.getElementById("bankroll").innerHTML=
    `<div>Toplam kupon<b>${s.total}</b></div>`+
    `<div>Tutan / Yatan<b><span class="won">${s.won}</span> / <span class="lost">${s.lost}</span></b></div>`+
    `<div>Botun toplam başarısı<b>%${s.success.toFixed(1).replace(".",",")}</b></div>`+
    `<div>Toplam sanal bahis<b>${money(s.totalStaked)}</b></div>`+
    `<div>Gerçekleşen net kâr/zarar<b class="${netClass}">${s.net>=0?"+":""}${money(s.net)}</b></div>`;
}
function renderCoupons(coupons){
  const root=document.getElementById("coupons"),store=couponStore();root.innerHTML="";
  renderBankroll(store);
  let won=0,lost=0,legWon=0,legLost=0;
  for(const list of Object.values(store))for(const c of list||[]){
    if(c.status==="WON")won++;if(c.status==="LOST")lost++;
    for(const l of c.legs||[]){if(l.status==="WON")legWon++;if(l.status==="LOST")legLost++;}
  }
  const settled=won+lost,settledLegs=legWon+legLost;
  document.getElementById("couponState").textContent=coupons.length
    ? `Bu gün için ${coupons.length} kupon • Genel kupon başarısı: ${settled?Math.round(won/settled*100):0}% (${won}/${settled}) • Seçim başarısı: ${settledLegs?Math.round(legWon/settledLegs*100):0}% (${legWon}/${settledLegs})`
    : "Bu tarih için kayıtlı kupon yok.";
  coupons.forEach((c,i)=>{
    const el=document.createElement("div");el.className="coupon";
    const status=c.status||"OPEN",statusText=status==="WON"?"TUTTU":status==="LOST"?"YATMADI":"BEKLİYOR";
    const stake=Number(c.stake||couponStake),total=Number(c.total||1),potential=stake*total;
    const probability=Number(c.probability||0),expectedProfit=Number(c.expectedProfit||0);
    el.innerHTML=`<div class="coupon-title">Kupon ${i+1} • <span class="${status.toLowerCase()}">${statusText}</span></div>`+
      c.legs.map(l=>`<div class="coupon-leg"><b>${esc(l.home)} — ${esc(l.away)}</b><br>${esc(l.label)} • ${l.odd.toFixed(2)} ${esc(l.bookmaker)}<br>Model güveni ${l.confidence}/100${l.score?` • Skor ${esc(l.score)}`:""} • <span class="${l.status.toLowerCase()}">${l.status==="WON"?"TUTTU":l.status==="LOST"?"YATTI":"AÇIK"}</span></div>`).join("")+
      `<div class="coupon-total">Toplam oran ${total.toFixed(2)} • Bahis ${stake.toLocaleString("tr-TR")} TL • ${status==="OPEN"?`Potansiyel dönüş ${potential.toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`:status==="WON"?`Dönüş ${potential.toLocaleString("tr-TR",{maximumFractionDigits:0})} TL • Net +${(potential-stake).toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`:`Net -${stake.toLocaleString("tr-TR")} TL`}<br>${probability?`İhtiyatlı model tutma olasılığı %${(probability*100).toFixed(1).replace(".",",")} • Beklenen değer ${expectedProfit>=0?"+":""}${expectedProfit.toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`:"Eski kupon • olasılık kaydı yok"}</div>`;
    root.appendChild(el);
  });
}
function renderFixtures(){
  const selected=loadedDate;
  const filtered=leagueSelect.value==="ALL" ? loadedMatches
    : loadedMatches.filter(m=>String(m.league_id)===leagueSelect.value);
  state.textContent=filtered.length
    ? `${filtered.length} maç gösteriliyor. Analiz için bir maç seç.`
    : "Seçilen ligde başlamamış maç bulunamadı.";
  root.innerHTML="";
  for(const m of filtered){
      const card=document.createElement("article");card.className="match";
      card.innerHTML=`<div class="meta">🏆 ${esc(m.country)} • ${esc(m.league)} &nbsp; ⏰ ${esc(shownTime(m.kickoff))}</div>
        <div class="teams">${esc(m.home)} — ${esc(m.away)}</div>
        <button type="button">Bu maçı analiz et</button><div class="analysis" hidden></div>`;
      const button=card.querySelector("button"),box=card.querySelector(".analysis");
      button.onclick=async()=>{
        button.disabled=true;box.hidden=false;box.textContent="Son maçlar inceleniyor…";
        try{
          const a=await getJson("/api/prematch/analyze?date="+encodeURIComponent(selected)+"&fixture="+encodeURIComponent(m.id));
          analyzedMatches.set(Number(m.id),{match:m,analysis:a});
          const picks=a.suggestions.length
            ? a.suggestions.map(p=>`<div class="pick"><b>${esc(p.label)}</b><div class="quote">Oran ${Number(p.quote.odd).toFixed(2)} · ${esc(p.quote.bookmaker)}</div><div class="reason">${esc(p.quote.market_name)}: ${esc(p.quote.selection)}${quoteTime(p.quote.updated)?` · Güncelleme: ${esc(quoteTime(p.quote.updated))}`:""}</div><div class="pick-why"><b>Neden bu tercih?</b><br>${esc(p.explanation || p.reason)}</div></div>`).join("")
            : `<div class="pas">PAS • ${a.candidate_count ? "Modelde eğilim var, ancak fiyat koşulu sağlanmadı." : "Yeterli ortak veri işareti yok."}</div>`;
          box.innerHTML=`<div class="form"><div><b>${esc(a.home)}</b><br>${esc(formText(a.home_form,"home"))}</div>
            <div><b>${esc(a.away)}</b><br>${esc(formText(a.away_form,"away"))}</div></div>
            ${h2hHtml(a)}${lineupHtml(a)}${picks}${(a.joint_notes||[]).map(n=>`<div class="joint-note"><b>Birlikte gerçekleşme ihtimali</b><br>${esc(n)}</div>`).join("")}<div class="odds-note">${esc(a.odds_note)}</div><div class="hint">${esc(a.note)}</div>`;
        }catch(e){box.innerHTML='<span class="error">'+esc(e.message)+'</span>'}
        finally{button.disabled=false}
      };
      root.appendChild(card);
  }
}
async function load(){
  const selected=day.value;state.textContent="Fikstür yükleniyor…";root.innerHTML="";
  leagueSelect.disabled=true;leagueSelect.innerHTML='<option value="ALL">Tüm ligler</option>';
  loadedMatches=[];loadedDate="";analyzedMatches=new Map();
  try{
    const data=await getJson("/api/prematch/fixtures?date="+encodeURIComponent(selected));
    loadedDate=selected;loadedMatches=data.matches;
    const leagues=new Map();
    for(const m of loadedMatches){
      const id=String(m.league_id);
      if(!leagues.has(id))leagues.set(id,{name:`${m.country||""} • ${m.league||"Lig"}`,count:0});
      leagues.get(id).count++;
    }
    const options=[...leagues.entries()].sort((a,b)=>a[1].name.localeCompare(b[1].name,"tr"));
    leagueSelect.innerHTML=`<option value="ALL">Tüm ligler (${loadedMatches.length})</option>`+
      options.map(([id,item])=>`<option value="${esc(id)}">${esc(item.name)} (${item.count})</option>`).join("");
    leagueSelect.disabled=options.length===0;
    renderFixtures();renderCoupons(couponStore()[loadedDate]||[]);settleCoupons();
  }catch(e){state.innerHTML='<span class="error">'+esc(e.message)+'</span>'}
}
leagueSelect.onchange=renderFixtures;
document.getElementById("makeCoupons").onclick=buildCoupons;
document.getElementById("load").onclick=load;load();
</script></body></html>"""


@app.route("/tahmin")
def prematch_page():
    today = datetime.now(ISTANBUL_TZ).date()
    next_saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    return render_template_string(
        PREMATCH_PAGE, today=today.isoformat(),
        initial_day=next_saturday.isoformat(),
        last_day=(today + timedelta(days=7)).isoformat(),
    )


# Gunicorn import ettiğinde scanner başlasın.
ensure_scanner_started()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
