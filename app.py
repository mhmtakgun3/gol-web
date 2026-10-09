    `<div>Botun toplam başarısı<b>%${s.success.toFixed(1).replace(".",",")}</b></div>`+
    `<div>Toplam sanal bahis<b>${money(s.totalStaked)}</b></div>`+
    `<div>Gerçekleşen net kâr/zarar<b class="${netClass}">${s.net>=0?"+":""}${money(s.net)}</b></div>`;
  renderLearningStats();
}
function renderCoupons(coupons){
  const root=document.getElementById("coupons"),store=couponStore();root.innerHTML="";
  coupons=(coupons||[]).filter(c=>c.status!=="VOID");
  renderBankroll(store);
  let won=0,lost=0,legWon=0,legLost=0;
  for(const list of Object.values(store))for(const c of list||[]){
    if(c.strategyVersion!==couponStrategyVersion)continue;
    if(c.status==="WON")won++;if(c.status==="LOST")lost++;
    for(const l of c.legs||[]){if(l.status==="WON")legWon++;if(l.status==="LOST")legLost++;}
  }
  const settled=won+lost,settledLegs=legWon+legLost;
  document.getElementById("couponState").textContent=coupons.length
    ? `Bugün ${coupons.length} aktif kupon • v4.12 kupon başarısı: ${settled?Math.round(won/settled*100):0}% (${won}/${settled}) • Seçim başarısı: ${settledLegs?Math.round(legWon/settledLegs*100):0}% (${legWon}/${settledLegs})`
    : "Bu tarih için v4.12 aktif kupon yok. Eski kuponlar performans hesabından ayrı tutuluyor.";
  coupons.forEach((c,i)=>{
    const el=document.createElement("div");el.className="coupon";
    const status=c.status||"OPEN",statusText=status==="WON"?"TUTTU":status==="LOST"?"YATMADI":status==="VOID"?"İPTAL":"BEKLİYOR";
    const stake=Number(c.stake||couponStake),total=Number(c.total||1),potential=stake*total;
    const probability=Number(c.probability||0),expectedProfit=Number(c.expectedProfit||0);
    const risk=couponRisk(probability);
    const riskHtml=risk.key!=="none"?`<span class="coupon-risk ${risk.key}">${risk.label} • RİSK: ${risk.risk}</span>`:"";
    const couponType=c.legs?.length===2?"2 MAÇ":c.legs?.length===3?"3 MAÇ":c.legs?.length===4?"4 MAÇ":`${c.legs?.length||0} MAÇ`;
    el.innerHTML=`<div class="coupon-title">Kupon ${i+1} • <span class="${status.toLowerCase()}">${statusText}</span></div>`+
      riskHtml+`<div class="coupon-meta"><b>${couponType}</b> • Birlikte tutma ihtimali <b>%${(probability*100).toFixed(1).replace(".",",")}</b> • Risk <b>${risk.risk}</b></div>`+
      c.legs.map(l=>`<div class="coupon-leg"><b>${esc(l.home)} — ${esc(l.away)}</b><br>${esc(l.label)} • ${Number(l.odd||0).toFixed(2)} ${esc(l.bookmaker||"")}<br>Model olasılığı %${(Number(l.modelProbability||0)*100).toFixed(1).replace(".",",")} • Değer +${Number(l.valueEdge||0).toFixed(1).replace(".",",")}%${l.historicalN?` • Market geçmişi %${(Number(l.historicalRate||0)*100).toFixed(0)} (${l.historicalN} analiz)`:""} • <span class="${String(l.status||"OPEN").toLowerCase()}">${l.status==="WON"?"TUTTU":l.status==="LOST"?"YATTI":l.status==="VOID"?"İPTAL":"AÇIK"}</span></div>`).join("")+
      `<div class="coupon-total">Toplam oran ${total.toFixed(2)} • ${status==="VOID"?`Bahis iptal • Para ve başarı hesabına dahil değil`: `Bahis ${stake.toLocaleString("tr-TR")} TL • ${status==="OPEN"?`Potansiyel dönüş ${potential.toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`:status==="WON"?`Dönüş ${potential.toLocaleString("tr-TR",{maximumFractionDigits:0})} TL • Net +${(potential-stake).toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`:`Net -${stake.toLocaleString("tr-TR")} TL`}`}<br>${probability?`Kuponun birlikte tutma ihtimali %${(probability*100).toFixed(1).replace(".",",")} • Teorik beklenen değer ${expectedProfit>=0?"+":""}${expectedProfit.toLocaleString("tr-TR",{maximumFractionDigits:0})} TL`:`Eski kupon • olasılık kaydı yok`}</div>`+
      `<button type="button" class="remove-coupon">Kuponu kaldır</button>`;
    root.appendChild(el);
    el.querySelector(".remove-coupon").onclick=()=>{
      if(!confirm("Bu kuponu listeden kaldırmak istediğine emin misin? Kaldırılan kupon başarı ve kâr/zarar hesabına dahil edilmez."))return;
      const current=couponStore();
      const signature=c.signature||c.legs.map(l=>`${l.fixture}:${l.market}`).sort().join("|");
      current[loadedDate]=(current[loadedDate]||[]).filter(x=>{
        const xs=x.signature||x.legs.map(l=>`${l.fixture}:${l.market}`).sort().join("|");
        return xs!==signature;
      });
      saveCouponStore(current);
      renderCoupons(current[loadedDate]||[]);
    };
  });
}
function renderFixtures(){
  const selected=loadedDate;
  const filtered=leagueSelect.value==="ALL" ? loadedMatches
    : loadedMatches.filter(m=>String(m.country||"Diğer")===leagueSelect.value);
  state.textContent=filtered.length
    ? `${filtered.length} maç gösteriliyor. Analiz için bir maç seç.`
    : "Seçilen ligde başlamamış maç bulunamadı.";
  root.innerHTML="";
  for(const m of filtered){
      const card=document.createElement("article");card.className="match";
      card.innerHTML=`<div class="meta">🏆 ${esc(m.country)} • ${esc(m.league)} &nbsp; ⏰ ${esc(shownTime(m.kickoff))}</div>
        <div class="teams">${esc(m.home)} — ${esc(m.away)}</div>
        <button type="button" class="analyze-btn">Bu maçı analiz et</button><div class="analysis" hidden></div>`;
      const button=card.querySelector(".analyze-btn"),box=card.querySelector(".analysis");
      button.onclick=async()=>{
        button.disabled=true;box.hidden=false;box.textContent="Son maçlar inceleniyor…";
        try{
          const a=await getJson("/api/prematch/analyze?date="+encodeURIComponent(selected)+"&fixture="+encodeURIComponent(m.id));
          analyzedMatches.set(Number(m.id),{match:m,analysis:a});
          registerPickHistory(m,a);
          const picks=a.suggestions.length
            ? a.suggestions.map(p=>`<div class="pick"><b>${esc(p.label)}</b><div class="quote">Oran ${Number(p.quote.odd).toFixed(2)} · ${esc(p.quote.bookmaker)}</div><div class="reason">${esc(p.quote.market_name)}: ${esc(p.quote.selection)}${quoteTime(p.quote.updated)?` · Güncelleme: ${esc(quoteTime(p.quote.updated))}`:""}</div><div class="pick-why"><b>Neden bu tercih?</b><br>${esc(p.explanation || p.reason)}</div><div class="reason">Tahmini olasılık ${p.model_probability!=null?`${(Number(p.model_probability)*100).toFixed(1).replace(".",",")}%`:"-"} · Oranın ima ettiği ${p.implied_probability!=null?`${(Number(p.implied_probability)*100).toFixed(1).replace(".",",")}%`:"-"} · Değer farkı ${p.value_edge_pct!=null?`${Number(p.value_edge_pct)>=0?"+":""}${Number(p.value_edge_pct).toFixed(1).replace(".",",")}%`:"-"}</div><div class="reason">${esc(p.probability_basis||"Market özelinde form verileri kullanıldı.")}</div></div>`).join("")
            : `<div class="pas">PAS • ${a.candidate_count ? "Modelde eğilim var, ancak fiyat koşulu sağlanmadı." : "Yeterli ortak veri işareti yok."}</div>`;
          box.innerHTML=`<div style="display:flex;justify-content:flex-end;margin-bottom:8px"><button type="button" class="close-analysis">✕ Analizi kapat</button></div><div class="form"><div><b>${esc(a.home)}</b><br>${esc(formText(a.home_form,"home"))}</div>
            <div><b>${esc(a.away)}</b><br>${esc(formText(a.away_form,"away"))}</div></div>
            ${h2hHtml(a)}${lineupHtml(a)}${picks}${(a.joint_notes||[]).map(n=>`<div class="joint-note"><b>Birlikte gerçekleşme ihtimali</b><br>${esc(n)}</div>`).join("")}<div class="odds-note">${esc(a.odds_note)}</div><div class="hint">${esc(a.note)}</div>`;
          box.querySelector(".close-analysis").onclick=()=>{
            analyzedMatches.delete(Number(m.id));
            box.hidden=true;
            box.innerHTML="";
            button.disabled=false;
            button.textContent="Bu maçı tekrar analiz et";
          };
        }catch(e){box.innerHTML='<span class="error">'+esc(e.message)+'</span>'}
        finally{button.disabled=false}
      };
      root.appendChild(card);
  }
}
async function load(){
  const selected=day.value;state.textContent="Fikstür yükleniyor…";root.innerHTML="";
  leagueSelect.disabled=true;leagueSelect.innerHTML='<option value="ALL">Tüm ülkeler</option>';
  loadedMatches=[];loadedDate="";analyzedMatches=new Map();
  try{
    const data=await getJson("/api/prematch/fixtures?date="+encodeURIComponent(selected));
    loadedDate=selected;loadedMatches=data.matches;
    const countries=new Map();
    for(const m of loadedMatches){
      const country=String(m.country||"Diğer");
      if(!countries.has(country))countries.set(country,{count:0,leagues:new Set()});
      countries.get(country).count++;
      countries.get(country).leagues.add(String(m.league||"Lig"));
    }
    const options=[...countries.entries()].sort((a,b)=>a[0].localeCompare(b[0],"tr"));
    leagueSelect.innerHTML=`<option value="ALL">Tüm ülkeler (${loadedMatches.length} maç)</option>`+
      options.map(([country,item])=>`<option value="${esc(country)}">${esc(country)} · ${item.leagues.size} lig (${item.count} maç)</option>`).join("");
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
