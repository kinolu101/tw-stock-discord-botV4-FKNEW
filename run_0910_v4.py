# -*- coding: utf-8 -*-
"""09:10-09:20 反彈失敗放空觀察訊號。無自動下單。"""
import json, os, time, math
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import shioaji as sj
from dotenv import load_dotenv

TZ=ZoneInfo('Asia/Taipei')
INPUT=os.getenv('BREAKOUTS_FILE','breakouts_0905.json')
OUTPUT=os.getenv('SHORT_SIGNALS_FILE','short_signals_0910.json')
POLL=int(os.getenv('SHORT_POLL_SECONDS','15'))
TOP_N=int(os.getenv('TOP_N','3'))

def now(): return datetime.now(TZ)
def tick(p):
    return .01 if p<10 else .05 if p<50 else .1 if p<100 else .5 if p<500 else 1. if p<1000 else 5.
def down(p):
    v=p
    for _ in range(3):
        s=tick(v); v=round(math.floor((p+1e-8)/s)*s,2)
    return v
def up(p):
    v=p
    for _ in range(3):
        s=tick(v); v=round(math.ceil((p-1e-8)/s)*s,2)
    return v

def discord(msg):
    hook=os.getenv('DISCORD_WEBHOOK','')
    if not hook:
        print('未設定Webhook：',msg); return
    r=requests.post(hook,json={'content':msg},timeout=15);r.raise_for_status()

def load():
    with open(INPUT,encoding='utf8') as f: payload=json.load(f)
    stamp=datetime.strptime(payload['generated_at'],'%Y-%m-%d %H:%M:%S')
    if stamp.date()!=now().date(): raise RuntimeError('09:05資料不是今天，拒絕沿用')
    return payload.get('results',[])

def get_ticks(api,contract):
    t=api.ticks(contract=contract,date=now().strftime('%Y-%m-%d'))
    df=pd.DataFrame({'ts':pd.to_datetime(t.ts),'price':t.close,'volume':t.volume})
    if df.empty:return df
    if df.ts.dt.tz is not None: df['ts']=df.ts.dt.tz_convert(TZ).dt.tz_localize(None)
    return df[(df.volume>0)].sort_values('ts')

def bars(df):
    day=now().date(); start=datetime.combine(day,dtime(9)); t5=datetime.combine(day,dtime(9,5));t10=datetime.combine(day,dtime(9,10))
    def b(a,z):
        s=df[(df.ts>=a)&(df.ts<z)]
        if s.empty:return None
        return {'open':float(s.price.iloc[0]),'high':float(s.price.max()),'low':float(s.price.min()),'close':float(s.price.iloc[-1]),'vol':float(s.volume.sum())}
    return b(start,t5),b(t5,t10)

def setup(first,second,df):
    if not first or not second:return None
    if first['close']>=first['open']:return None
    # 第二根必須反彈，但不能收復第一根中點；收盤重新轉弱。
    midpoint=(first['open']+first['close'])/2
    if not (second['high']>first['close'] and second['high']<first['high'] and second['close']<second['open'] and second['close']<midpoint):return None
    sample=df[(df.ts>=datetime.combine(now().date(),dtime(9)))&(df.ts<datetime.combine(now().date(),dtime(9,10)))]
    if sample.empty or sample.volume.sum()<=0:return None
    vwap=float((sample.price*sample.volume).sum()/sample.volume.sum())
    if second['close']>=vwap:return None
    trigger=down(min(first['low'],second['low'])-tick(min(first['low'],second['low'])))
    stop=up(second['high']+tick(second['high']))
    risk=stop-trigger
    if risk<=0:return None
    tp1=down(trigger-1.5*risk);tp2=down(trigger-2*risk)
    return {'觸發價':trigger,'停損價':stop,'停利1':tp1,'停利2':tp2,'VWAP':round(vwap,2),'第一根':first,'第二根':second}

def run():
    load_dotenv();t=now()
    if t.weekday()>4 or t.time()<dtime(9,10):raise RuntimeError('必須在交易日09:10後執行，防止未完成K棒')
    candidates=load()[:40]
    if not candidates:
        discord('【09:10 反彈失敗觀察】09:05沒有合格候選股。');return
    api=sj.Shioaji(simulation=os.getenv('SHIOAJI_SIMULATION','false').lower()=='true')
    api.login(api_key=os.environ['SHIOAJI_API_KEY'],secret_key=os.environ['SHIOAJI_SECRET_KEY'])
    selected=[]; sent=set()
    try:
        # 先固定09:00-09:10已完成資料的反彈失敗結構，再監控是否破低
        for c in candidates:
            code=str(c.get('股票代號',''))
            try:
                contract=api.Contracts.Stocks[code]
                df=get_ticks(api,contract)
                a,b=bars(df);s=setup(a,b,df)
                if s: selected.append({**c,**s})
            except Exception as e:print('setup error',code,e)
        selected=selected[:TOP_N]
        discord('【09:10 反彈失敗觀察】'+('符合結構：'+', '.join(str(s['股票代號']) for s in selected)+'；等待破低確認。' if selected else '無符合反彈失敗結構的標的。'))
        end=datetime.combine(now().date(),dtime(9,20),tzinfo=TZ)
        while now()<end and len(sent)<len(selected):
            for s in selected:
                code=str(s['股票代號'])
                if code in sent:continue
                try:
                    contract=api.Contracts.Stocks[code]
                    df=get_ticks(api,contract)
                    # 必須在09:10之後觀察到真正的破低成交，不用09:10之前價格冒充
                    recent=df[df.ts>=datetime.combine(now().date(),dtime(9,10))]
                    if recent.empty:continue
                    hits=recent[recent.price<=s['觸發價']]
                    if hits.empty:continue
                    sent.add(code)
                    s['觸發時間']=str(hits.ts.iloc[0]);s['訊號']='反彈失敗破低（觀察）'
                    discord(f"【放空觀察訊號｜非下單】🔻 {code} {s.get('股票名稱','')}\n破低觸發 {s['觸發價']}｜停損 {s['停損價']}｜參考停利 {s['停利1']} / {s['停利2']}\n現股先賣後買帳戶資格、券源及當日限制尚未確認；請自行核實。")
                except Exception as e:print('monitor error',code,e)
            if now()<end:time.sleep(max(POLL,5))
    finally:
        try:api.logout()
        except Exception:pass
        with open(OUTPUT,'w',encoding='utf8') as f:json.dump({'generated_at':now().isoformat(),'results':selected},f,ensure_ascii=False,indent=2)
if __name__=='__main__':run()
