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
    if os.getenv('PREMATCH_WEB_RESEARCH','1').lower() in ('0','false','off'):
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
 ('BB_HOME_UNDER55','MS 1 + 5,5 ALT','home_builder',lambda h,a:h>a and h+a<=5),
 ('BB_AWAY_UNDER55','MS 2 + 5,5 ALT','away_builder',lambda h,a:a>h and h+a<=5),
 ('BB_HOME_OVER35','MS 1 + 3,5 ÜST','home_builder',lambda h,a:h>a and h+a>=4),
 ('BB_AWAY_OVER35','MS 2 + 3,5 ÜST','away_builder',lambda h,a:a>h and h+a>=4),
 ('BB_HOME_TEAM15','MS 1 + Ev 1,5 ÜST','home_builder',lambda h,a:h>a and h>=2),
 ('BB_AWAY_TEAM15','MS 2 + Dep 1,5 ÜST','away_builder',lambda h,a:a>h and a>=2),
]

def analyze_match(home, away, h2h=None):
    ha, aa = home.get('all') or {}, away.get('all') or {}
    hv, av = home.get('home') or {}, away.get('away') or {}
    nh, na, nvh, nva = _count(ha), _count(aa), _count(hv), _count(av)
    quality = 'good' if min(nh,na)>=8 and min(nvh,nva)>=4 else ('limited' if min(nh,na)>=5 else 'poor')
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
    scores=[(h,a,math.exp(-lh-la)*lh**h*la**a/(math.factorial(h)*math.factorial(a)))
            for h in range(13) for a in range(13)]
    total=sum(p for _,_,p in scores)
    markets=[]
    for code,label,family,rule in RULES:
        p=sum(weight for h,a,weight in scores if rule(h,a))/total
        markets.append({'market':code,'label':label,'family':family,'model_probability':round(p,4),
            'reason':f'Ev gol beklentisi {lh:.2f}, deplasman {la:.2f}. {label} senaryosu için ortak skor dağılımından %{p*100:.1f} hesaplandı. Veri kalitesi: {quality}. Gerçekleşme garantisi değildir.'})
    return {'markets':markets,'data_quality':quality,
        'scenario':{'home_expected_goals':round(lh,2),'away_expected_goals':round(la,2)},
        'model_note':'Form örneklemine göre daraltılmış ortak Poisson modeli; rakip gücü, kadro ve lig bazlı kalibrasyon henüz yok. Haber başlıkları doğrulanmadan modele katılmaz.'}

def risk_level(prob, quality):
    # Display-only heuristic, not a validated confidence interval.
    if quality == 'good' and prob >= .72: return 'green'
    if quality != 'poor' and prob >= .60: return 'yellow'
    return 'red'

def choose_markets(candidates, quality):
    """Separate analytical scenarios from actual price eligibility."""
    for c in candidates:
        c['risk_level']=risk_level(c['model_probability'],quality)
        c['decision_role']={'green':'GÜÇLÜ ANALİZ','yellow':'DENGELİ ANALİZ','red':'RİSKLİ ANALİZ'}[c['risk_level']]
    priced=[c for c in candidates if c.get('coupon_eligible') and c.get('quote')
            and float(c['quote'].get('odd') or 0)>=1.30 and c['model_probability']>=.60]
    priced.sort(key=lambda c:(c['model_probability'],c.get('expected_value') or 0),reverse=True)
    builders=sorted((c for c in candidates if c['market'].startswith('BB_')),
                    key=lambda c:c['model_probability'],reverse=True)
    # No fake priced builder: show best model scenario even if all probabilities are low.
    pool=priced+builders
    selected=[]
    def family(c):
        code=c['market']
        if code.startswith('BB_HOME_') or code=='HOME': return 'home_win'
        if code.startswith('BB_AWAY_') or code=='AWAY': return 'away_win'
        if code=='BB_BTTS_OVER25': return 'btts_goals'
        return c['family']
    for c in pool:
        if any(family(x)==family(c) for x in selected): continue
        selected.append(c)
        if len(selected)>=2: break
    for i,c in enumerate(selected):
        c['is_main']=i==0
        c['decision_role']=('ANA ANALİZ · ' if i==0 else 'ALTERNATİF · ')+c['decision_role']
        c['manual_review']=not bool(c.get('coupon_eligible'))
    return selected
