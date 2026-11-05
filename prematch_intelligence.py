"""Independent prematch research and scenario module; no live scanner imports.
Internet headlines are leads for manual verification, NEVER treated as confirmed injuries.
"""
from __future__ import annotations
import math, os, time, threading, re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from difflib import SequenceMatcher
from urllib.parse import urlencode
from html import unescape
from xml.etree import ElementTree as ET
import requests

_NEWS_CACHE = {}
_LOCK = threading.Lock()

def _metric(d, key, fallback):
    try:
        n = float((d or {}).get(key))
        return n if math.isfinite(n) and n >= 0 else fallback
    except (ValueError, TypeError):
        return fallback

def _count(d):
    try: return max(0, int((d or {}).get('count') or 0))
    except (ValueError, TypeError): return 0

def _team_tokens(name):
    """Avoid generic tokens like FC, 1., United, and league names."""
    noise={'fc','1','sc','sv','vfl','vfb','fk','cf','club','football','soccer',
           'united','city','sporting','real','de','the','afc','ac','fsv'}
    words=re.findall(r"[a-z0-9]+", name.casefold())
    return [w for w in words if len(w)>=4 and w not in noise]


def _fixture_date(kickoff):
    if not kickoff: return None
    try:
        dt=datetime.fromisoformat(str(kickoff).replace('Z','+00:00'))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (ValueError, TypeError): return None


def _article_relevance(title, home, away):
    low=title.casefold()
    ht=_team_tokens(home); at=_team_tokens(away)
    # Need one DISTINCTIVE team token. For the Heidenheim fixture,
    # unrelated Bayern/DFB cup headlines are excluded.
    home_match=any(re.search(r'(?<!\w)'+re.escape(w)+r'(?!\w)',low) for w in ht)
    away_match=any(re.search(r'(?<!\w)'+re.escape(w)+r'(?!\w)',low) for w in at)
    if not (home_match or away_match): return 0
    score=3 if home_match and away_match else 1
    if re.search(r'injur|injured|suspend|lineup|starting xi|team news|preview|'
                 r'verletzt|aufstellung|sperre|vorschau|kader|personnel|'
                 r'press conference|match report',low): score+=2
    if re.search(r'live stream|watch online|free tips|betting tips|prediction|'
                 r'fixtures list|schedule|transfer rumours',low): score-=5
    return score


# Translation is presentation-only; original source titles are retained.
_TRANSLATION_CACHE = {}
def _translate_title_tr(title):
    title = str(title or '').strip()
    if not title: return title, False
    if title in _TRANSLATION_CACHE: return _TRANSLATION_CACHE[title]
    # Public translation endpoint: best effort, never fabricate a translation.
    # Fail closed to the original title if unavailable or malformed.
    try:
        response = requests.get('https://translate.googleapis.com/translate_a/single',
            params={'client':'gtx','sl':'auto','tl':'tr','dt':'t','q':title},
            timeout=(2,3), headers={'User-Agent':'Mozilla/5.0'})
        response.raise_for_status()
        payload=response.json()
        translated=''.join(part[0] for part in payload[0] if part and isinstance(part[0],str)).strip()
        if not translated or len(translated)>500:
            raise ValueError('empty or malformed translation')
        value=(unescape(translated), True)
    except (requests.RequestException,ValueError,TypeError,IndexError,KeyError,AttributeError):
        value=(title, False)
    if len(_TRANSLATION_CACHE)>500: _TRANSLATION_CACHE.clear()
    _TRANSLATION_CACHE[title]=value
    return value

def research_match(home, away, kickoff=None):
    """Filter dated RSS leads by exact team relevance; never claim verified injuries."""
    if os.getenv('PREMATCH_WEB_RESEARCH','0').lower() in ('0','false','off'):
        return {'status':'disabled','sources':[],'note':'İnternet araştırması kapalı.'}
    home=str(home or '').strip(); away=str(away or '').strip()
    if not _team_tokens(home) or not _team_tokens(away):
        return {'status':'no_results','sources':[],'note':'Takım adları araştırma için yetersiz.'}
    match_dt=_fixture_date(kickoff)
    reference=match_dt or datetime.now(timezone.utc)
    key=(home,away,reference.date().isoformat())
    with _LOCK:
        cached=_NEWS_CACHE.get(key)
        if cached and time.time()-cached[0]<3600: return cached[1]
    # Target team names instead of broad football news, with two independent searches.
    queries=[f'"{home}" "{away}"',f'"{home}" (injury OR lineup OR verletzt OR kader)',
             f'"{away}" (injury OR lineup OR verletzt OR kader)']
    found={}; failures=0
    for query in queries:
        try:
            url='https://news.google.com/rss/search?'+urlencode({'q':query,'hl':'en','gl':'GB','ceid':'GB:en'})
            response=requests.get(url,timeout=(2,4),headers={'User-Agent':'GolMerkeziPrematchResearch/2.0'})
            response.raise_for_status()
            root=ET.fromstring(response.content[:250000])
            for node in root.findall('./channel/item')[:20]:
                title=(node.findtext('title') or '').strip()
                link=(node.findtext('link') or '').strip()
                published=(node.findtext('pubDate') or '').strip()
                if not title or not link.startswith('https://'): continue
                score=_article_relevance(title,home,away)
                if score<=0: continue
                try:
                    pub=parsedate_to_datetime(published).astimezone(timezone.utc)
                except (TypeError,ValueError,IndexError,OverflowError): continue
                # Max 14 days before kickoff, and no post-match news.
                if not reference-timedelta(days=14)<=pub<=reference+timedelta(hours=2): continue
                unique=re.sub(r'\W+','',title.casefold())[:110]
                if unique not in found or found[unique][0]<score:
                    found[unique]=(score,{'title':title[:220],'url':link,
                        'published':published,'verified':False})
        except (requests.RequestException,ET.ParseError,ValueError):
            failures+=1
    items=[v[1] for v in sorted(found.values(),key=lambda x:x[0],reverse=True)[:5]]
    for item in items:
        original=item['title']
        translated, success=_translate_title_tr(original)
        item['original_title']=original
        item['title']=translated
        item['translated_tr']=success
        item['language_note']=('Türkçe çeviri' if success else
                               'Çeviri servisine ulaşılamadı; özgün başlık gösteriliyor')
    status='ok' if items else ('unavailable' if failures==len(queries) else 'no_results')
    note=('Yalnızca takımlarla ilgili ve maç tarihine yakın haber başlıkları listelenir. '
          'Türkçe başlıklar otomatik çeviridir; özgün haber bağlantısı korunur. '
          'Başlıklar doğrulanmış sakatlık/kadro bilgisi değildir; tahmine otomatik katılmaz.'
          if items else 'Maçla doğrudan ilgili güncel ve doğrulanabilir haber bulunamadı; eski veya alakasız sonuçlar gösterilmedi.')
    result={'status':status,'sources':items,'note':note}
    with _LOCK: _NEWS_CACHE[key]=(time.time(),result)
    return result

# Rules operate on one joint score distribution; no independent multiplication of builders.
RULES = [
 ('HOME','MS 1','result_home',lambda h,a:h>a),
 ('DRAW','MS X','result_draw',lambda h,a:h==a),
 ('AWAY','MS 2','result_away',lambda h,a:h<a),
 ('DC_1X','1X Çifte Şans','double_chance',lambda h,a:h>=a),
 ('DC_X2','X2 Çifte Şans','double_chance',lambda h,a:a>=h),
 ('DC_12','12 Çifte Şans','double_chance',lambda h,a:h!=a),
 ('HOME_OVER_0_5','Ev 0,5 ÜST','home_goals',lambda h,a:h>=1),
 ('AWAY_OVER_0_5','Dep 0,5 ÜST','away_goals',lambda h,a:a>=1),
 ('HOME_WIN_HALF','Ev sahibi bir devre kazanır','half_result',None),
 ('AWAY_WIN_HALF','Deplasman bir devre kazanır','half_result',None),
 ('HT_OVER_0_5','İY 0,5 ÜST','half_goals',None),
 ('HT_OVER_1_5','İY 1,5 ÜST','half_goals',None),
 ('HT_UNDER_1_5','İY 1,5 ALT','half_goals',None),
 ('OVER_1_5','1,5 ÜST','goals15',lambda h,a:h+a>=2),
 ('OVER_2_5','2,5 ÜST','goals25',lambda h,a:h+a>=3),
 ('UNDER_2_5','2,5 ALT','goals25',lambda h,a:h+a<=2),
 ('OVER_3_5','3,5 ÜST','goals35',lambda h,a:h+a>=4),
 ('UNDER_3_5','3,5 ALT','goals35',lambda h,a:h+a<=3),
 ('BTTS_YES','KG VAR','btts',lambda h,a:h>0 and a>0),
 ('BTTS_NO','KG YOK','btts',lambda h,a:h==0 or a==0),
 ('HOME_OVER_1_5','Ev 1,5 ÜST','home_goals',lambda h,a:h>=2),
 ('AWAY_OVER_1_5','Dep 1,5 ÜST','away_goals',lambda h,a:a>=2),
 ('BB_BTTS_OVER25','KG VAR + 2,5 ÜST','btts_builder',lambda h,a:h>0 and a>0 and h+a>=3),
 ('BB_HOME_OVER15','MS 1 + 1,5 ÜST','home_builder',lambda h,a:h>a and h+a>=2),
 ('BB_AWAY_OVER15','MS 2 + 1,5 ÜST','away_builder',lambda h,a:a>h and h+a>=2),
 ('BB_HOME_UNDER55','MS 1 + 5,5 ALT','home_builder',lambda h,a:h>a and h+a<=5),
 ('BB_AWAY_UNDER55','MS 2 + 5,5 ALT','away_builder',lambda h,a:a>h and h+a<=5),
 ('BB_HOME_OVER35','MS 1 + 3,5 ÜST','home_builder',lambda h,a:h>a and h+a>=4),
 ('BB_AWAY_OVER35','MS 2 + 3,5 ÜST','away_builder',lambda h,a:a>h and h+a>=4),
 ('BB_HOME_TEAM15','MS 1 + Ev 1,5 ÜST','home_builder',lambda h,a:h>a and h>=2),
 ('BB_AWAY_TEAM15','MS 2 + Dep 1,5 ÜST','away_builder',lambda h,a:a>h and a>=2),
]

def calibrate_with_market(analysis, market_odds):
    """Calibrate full-time score scenarios to independently observed 1X2 prices.

    This is a market-strength proxy, NOT verified squad values or an independent
    prediction. It avoids comparing raw goal averages across unequal leagues.
    All full-time markets use one coherent reweighted score distribution.
    """
    if not market_odds or len(market_odds) != 3:
        analysis['strength_context']={'status':'unavailable','note':'1X2 piyasa güç referansı eksik; kadro değeri doğrulanamadı.'}
        return analysis
    try:
        raw={k:1/float(market_odds[k]) for k in ('HOME','DRAW','AWAY')}
        if any(not math.isfinite(v) or v<=0 for v in raw.values()): raise ValueError()
        norm=sum(raw.values()); target={k:v/norm for k,v in raw.items()}
    except (ValueError,TypeError,ZeroDivisionError):
        analysis['strength_context']={'status':'unavailable','note':'1X2 oranları geçersiz.'}
        return analysis
    lh=analysis['scenario']['home_expected_goals']; la=analysis['scenario']['away_expected_goals']
    scores=[(h,a,math.exp(-lh-la)*lh**h*la**a/(math.factorial(h)*math.factorial(a))) for h in range(13) for a in range(13)]
    groups={'HOME':lambda h,a:h>a,'DRAW':lambda h,a:h==a,'AWAY':lambda h,a:h<a}
    base={k:sum(p for h,a,p in scores if fn(h,a)) for k,fn in groups.items()}
    z=sum(base.values()); base={k:v/z for k,v in base.items()}
    # 70% market consensus + 30% recent-form score model: not a market edge estimator.
    blend=.70
    adjusted={k:blend*target[k]+(1-blend)*base[k] for k in groups}
    weighted=[]
    for h,a,p in scores:
        outcome='HOME' if h>a else 'AWAY' if a>h else 'DRAW'
        weighted.append((h,a,p*adjusted[outcome]/max(base[outcome]*z,1e-12)))
    total=sum(p for _,_,p in weighted)
    for m in analysis['markets']:
        rule=next((r for r in RULES if r[0]==m['market']),None)
        if not rule or rule[3] is None or m['market'].startswith('HT_'): continue
        prob=sum(p for h,a,p in weighted if rule[3](h,a))/total
        m['model_probability']=round(prob,4)
        m['reason']=(f"Son 5 maçın gol/ev-deplasman verileri ve doğrulanmış 1X2 piyasa güç referansı "
                     f"birlikte değerlendirildi. {m['label']} senaryosu %{prob*100:.1f}. "
                     'Piyasa oranları kadro piyasa değeri değildir; sakatlık ve lig gücü ayrıca doğrulanmadı.')
    analysis['strength_context']={
        'status':'market_proxy','source':'1X2 bookmaker odds',
        'market_probabilities':{k:round(v,4) for k,v in target.items()},
        'adjusted_result_probabilities':{k:round(v,4) for k,v in adjusted.items()},
        'note':'Takım gücü için piyasa 1X2 olasılıkları referans alındı; kadro piyasa değeri, oyuncu seviyesi ve lig katsayısı doğrudan doğrulanmış değildir. Piyasa verisi kullanıldığı için hesaplanan EV bağımsız model avantajı kanıtı değildir.'}
    analysis['model_note']='Son 5 maç, iç/deplasman formu ve doğrulanmış 1X2 piyasa güç referansı kullanıldı. Kadro değerleri ve ligler arası güç katsayıları henüz doğrulanmadı; olasılıklar kalibre edilmemiş tahminlerdir.'
    return analysis

def analyze_match(home, away, h2h=None):
    ha, aa = home.get('all') or {}, away.get('all') or {}
    hv, av = home.get('home') or {}, away.get('away') or {}
    nh, na, nvh, nva = _count(ha), _count(aa), _count(hv), _count(av)
    quality = 'good' if min(nh,na)>=5 and min(nvh,nva)>=3 else ('limited' if min(nh,na)>=3 else 'poor')
    # Shrink small samples to league-neutral priors; never mistake missing for 0.
    # Venue samples and all-match samples blend based on observed sample counts.
    def blended(venue, overall, field, prior):
        vn, an = _count(venue), _count(overall)
        general = _metric(overall, field, prior)
        general = (an*general + 6*prior)/(an+6)
        venue_mean = _metric(venue,field,general)
        return (vn*venue_mean + 5*general)/(vn+5)
    ha_g=blended(hv,ha,'scored',1.40)
    ad_g=blended(av,aa,'conceded',1.40)
    aa_g=blended(av,aa,'scored',1.15)
    hd_g=blended(hv,ha,'conceded',1.15)
    lh=max(.35,min(3.0,(ha_g*ad_g)**.5))
    la=max(.35,min(3.0,(aa_g*hd_g)**.5))
    # Son maçların iç/dış saha formu zaten gol beklentisine dahildir.
    # İkili rekabet az sayıda maçtan oluşabileceğinden sadece küçük,
    # örneklemle sınırlı bir düzeltme yap; geçmiş karşılaşmaları kesin kanıt sayma.
    head=(h2h or {}).get('all') or {}
    hn=_count(head)
    if hn>=3:
        hw=_metric(head,'wins',0)/hn
        aw=_metric(head,'losses',0)/hn
        weight=min(.08, .012*hn)
        shift=max(-1.,min(1.,hw-aw))*weight
        lh=max(.35,min(3.,lh*(1+shift)))
        la=max(.35,min(3.,la*(1-shift)))
    context={
        'home_home_count':nvh,'away_away_count':nva,
        'home_home_win_rate':round(_metric(hv,'wins',0)/nvh,3) if nvh else None,
        'away_away_win_rate':round(_metric(av,'wins',0)/nva,3) if nva else None,
        'home_home_unbeaten_rate':round(_metric(hv,'unbeaten',0)/nvh,3) if nvh else None,
        'away_away_unbeaten_rate':round(_metric(av,'unbeaten',0)/nva,3) if nva else None,
        'home_home_scored':round(_metric(hv,'scored',0),2) if nvh else None,'away_away_scored':round(_metric(av,'scored',0),2) if nva else None,
        'home_home_conceded':round(_metric(hv,'conceded',0),2) if nvh else None,'away_away_conceded':round(_metric(av,'conceded',0),2) if nva else None,
        'h2h_count':hn,'h2h_home_wins':int(_metric(head,'wins',0)),
        'h2h_draws':int(_metric(head,'draws',0)),
        'h2h_away_wins':int(_metric(head,'losses',0)),
        'lineup_in_model':False,'opponent_strength_in_model':False,
        'shots_corners_in_model':False,
    }
    scores=[(h,a,math.exp(-lh-la)*lh**h*la**a/(math.factorial(h)*math.factorial(a)))
            for h in range(13) for a in range(13)]
    total=sum(p for _,_,p in scores)
    markets=[]
    # Devre pazarları için ayrı yarı Poisson dağılımı; iki yarı bağımsız
    # varsayımıdır, kesin ilk yarı oranı veya doğrulanmış başarı değildir.
    half_scores=[(h,a,math.exp(-(lh+la)/2)*(lh/2)**h*(la/2)**a /
                  (math.factorial(h)*math.factorial(a))) for h in range(13) for a in range(13)]
    half_z=sum(v for _,_,v in half_scores)
    half_home_win=sum(v for h,a,v in half_scores if h>a)/half_z
    half_away_win=sum(v for h,a,v in half_scores if a>h)/half_z
    for code,label,family,rule in RULES:
        if code=='HT_OVER_0_5': p=sum(v for h,a,v in half_scores if h+a>=1)/half_z
        elif code=='HT_OVER_1_5': p=sum(v for h,a,v in half_scores if h+a>=2)/half_z
        elif code=='HT_UNDER_1_5': p=sum(v for h,a,v in half_scores if h+a<=1)/half_z
        elif code=='HOME_WIN_HALF': p=1-(1-half_home_win)**2
        elif code=='AWAY_WIN_HALF': p=1-(1-half_away_win)**2
        else: p=sum(weight for h,a,weight in scores if rule(h,a))/total
        markets.append({'market':code,'label':label,'family':family,'model_probability':round(p,4),
            'reason':f'Ev gol beklentisi {lh:.2f}, deplasman {la:.2f}. {label} senaryosu için ortak skor dağılımından %{p*100:.1f} hesaplandı. Veri kalitesi: {quality}. Gerçekleşme garantisi değildir.'})
    return {'markets':markets,'context':context,'data_quality':quality,
        'scenario':{'home_expected_goals':round(lh,2),'away_expected_goals':round(la,2)},
        'model_note':'Son 5 genel ve iç/dış saha formu hesaba katılır; yeterli H2H varsa sınırlı ağırlık verilir. Rakip gücü, güncel kadro, oyun tarzı/şut ve lig kalibrasyonu modele dahil değildir; bunlar doğrulanmadan güçlü güven iddiası kurulamaz.'}

def risk_level(prob, quality):
    """Risk is an uncertainty label, never a validated win probability."""
    if quality == 'good' and prob >= .75: return 'green'
    if quality != 'poor' and prob >= .60: return 'yellow'
    return 'red'


def _scenario_family(market):
    if market in ('HOME', 'BB_HOME_UNDER55', 'BB_HOME_OVER35', 'BB_HOME_TEAM15'):
        return 'home_result'
    if market in ('AWAY', 'BB_AWAY_UNDER55', 'BB_AWAY_OVER35', 'BB_AWAY_TEAM15'):
        return 'away_result'
    if market in ('BTTS_YES', 'BB_BTTS_OVER25'):
        return 'btts_yes'
    if market == 'BTTS_NO': return 'btts_no'
    if market in ('OVER_1_5', 'OVER_2_5', 'OVER_3_5'): return 'goals_over'
    if market in ('UNDER_2_5', 'UNDER_3_5'): return 'goals_under'
    return market


def choose_markets(candidates, quality):
    """Market-neutral selection: no UNDER/OVER/BTTS/result category bonus.

    Only exact, independently verified bookmaker quotes can enter coupons.
    Builder markets remain visible for analysis until a matching combined quote exists.
    """
    if not candidates:
        return []
    for c in candidates:
        p=float(c.get('model_probability') or 0)
        c['risk_level']=risk_level(p,quality)
        q=c.get('quote') or {}
        try: odd=float(q.get('odd') or 0)
        except (TypeError,ValueError): odd=0
        c['price_verified']=bool(odd>=1.30)
        c['coupon_eligible']=bool(c.get('coupon_eligible') and c['price_verified'])
        c['manual_review']=not c['coupon_eligible']
    def rank(c):
        p=float(c.get('model_probability') or 0)
        q=c.get('quote') or {}
        odd=float(q.get('odd') or 0)
        # Identical objective for every market: price-relative expected value.
        return (p*odd-1, p) if odd>=1.30 else (-999,p)
    priced=sorted((c for c in candidates if c['coupon_eligible']),key=rank,reverse=True)
    informative=sorted((c for c in candidates if not c['coupon_eligible']),
                       key=lambda c:float(c.get('model_probability') or 0),reverse=True)
    selected=(priced or informative)[:2]
    for i,c in enumerate(selected):
        c['is_main']=i==0
        c['decision_role']='ANA MAÇ SENARYOSU' if i==0 else 'ALTERNATİF'
        c['explanation']=(c.get('reason') or '')+(' Doğrulanmış oran yok veya kupon koşullarını karşılamıyor; yalnızca analiz.' if not c['coupon_eligible'] else ' Gerçek oranla değerlendirildi.')
    return selected
