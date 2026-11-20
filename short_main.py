"""TW sell-first daytrade paper-signal engine. NEVER places an order."""
import argparse, csv, json, os, queue, re, time, math, threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import requests
from dotenv import load_dotenv

TZ=ZoneInfo('Asia/Taipei')
ROOT=Path(__file__).resolve().parent

@dataclass
class Settings:
    capital: float=500000
    risk_fraction: float=.005
    daily_loss_fraction: float=.01
    min_rr: float=1.8
    max_positions: int=2
    max_entries: int=3
    max_subscriptions: int=20
    slippage_ticks: int=1
    fee_rate: float=.001425*.6  # 示例券商折扣，請自行調整
    tax_rate: float=.0015  # 現股當沖證交稅假設值，應核對當日法規


def tick_size(p):
    if p < 10: return .01
    if p < 50: return .05
    if p < 100: return .1
    if p < 500: return .5
    if p < 1000: return 1.
    return 5.

def price_round(p, direction):
    step=tick_size(p)
    return round((math.floor(p/step+1e-9) if direction=='down' else math.ceil(p/step-1e-9))*step, 4)

def notify(message):
    print(datetime.now(TZ).strftime('%H:%M:%S'), message, flush=True)
    url=os.getenv('DISCORD_WEBHOOK','').strip()
    if url:
        try: requests.post(url,json={'content':message[:1900]},timeout=5).raise_for_status()
        except Exception as exc: print('Discord failed:',exc,flush=True)

def append_csv(name, row):
    p=ROOT/name
    with p.open('a',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(row))
        if p.stat().st_size==0: w.writeheader()
        w.writerow(row)

def load_eligibility(today, path=ROOT/'eligibility.csv'):
    if not path.exists(): raise RuntimeError('Missing eligibility.csv; fail closed')
    df=pd.read_csv(path,dtype={'code':str,'date':str}).fillna('')
    required={'date','code','name','exchange','ordinary','daytrade_sell_first','broker_approved','short_source_confirmed','restricted','disposition','full_delivery'}
    if not required.issubset(df.columns): raise RuntimeError('Eligibility columns missing')
    ok={}
    for _,r in df.iterrows():
        code=str(r.code).strip(); name=str(r['name'])
        if str(r.date)!=today.isoformat() or not re.fullmatch(r'\d{4}',code): continue
        if 'KY' in name.upper() or 'ETF' in name.upper() or str(r.exchange) not in ('TSE','OTC'): continue
        if all(str(r[k]).strip()=='1' for k in ('ordinary','daytrade_sell_first','broker_approved','short_source_confirmed')) and all(str(r[k]).strip()=='0' for k in ('restricted','disposition','full_delivery')):
            ok[code]=name
    if not ok: raise RuntimeError('No verified eligible ordinary shares today; fail closed')
    return ok

def daily_filter(d, open_price):
    if len(d)<65 or open_price<=0: return False
    c=d['close'].astype(float); v=d['volume'].astype(float)
    ma={n:c.rolling(n).mean() for n in (5,10,20,60)}
    down=all(ma[n].iloc[-1]<ma[n].iloc[-2] for n in (5,10,20))
    gap=open_price/c.iloc[-1]-1
    return (down and c.iloc[-1]<ma[20].iloc[-1] and c.iloc[-1]<ma[60].iloc[-1]
            and v.iloc[-1]>=3_000_000 and v.tail(20).mean()>=2_000_000
            and -.04<=gap<=-.005 and 20<=open_price<=300)

class Engine:
    def __init__(self, cfg, names, baselines, historical):
        self.cfg=cfg; self.names=names; self.baselines=baselines; self.historical=historical
        self.minutes=defaultdict(dict); self.last_ts={}; self.opens={}; self.candidates=set()
        self.qualified={}; self.positions={}; self.entries=0; self.realized=0.
        self.last_price={}; self.phase=set(); self.halted=False; self.lock=threading.RLock()

    def ingest(self, code, ts, price, shares, amount=None):
        if code not in self.names or self.halted: return
        if ts.tzinfo is None: ts=ts.replace(tzinfo=TZ)
        ts=ts.astimezone(TZ)
        if ts.date()!=datetime.now(TZ).date() and os.getenv('DEMO_MODE')!='1': return
        if not dtime(9,0)<=ts.time()<=dtime(13,30): return
        if price<=0 or shares<=0: return
        with self.lock:
            prev=self.last_ts.get(code)
            if prev and ts<prev: self.halted=True; notify('資料時間倒退，停止新訊號'); return
            self.last_ts[code]=ts
            self.last_price[code]=price
            m=ts.replace(second=0,microsecond=0)
            bar=self.minutes[code].get(m)
            if bar is None:
                bar={'open':price,'high':price,'low':price,'close':price,'volume':0,'amount':0.}
                self.minutes[code][m]=bar
            bar['high']=max(bar['high'],price);bar['low']=min(bar['low'],price);bar['close']=price
            bar['volume']+=shares;bar['amount']+=float(amount if amount is not None else price*shares)
            if code not in self.opens: self.opens[code]=price
            self._watch_position(code,ts,price)
            if code in self.qualified and dtime(9,10)<=ts.time()<dtime(9,20):
                self._trigger(code,ts,price)

    def bars(self, code, start, end):
        today=(self.last_ts.get(code) or datetime.now(TZ)).date()
        selected=[v for t,v in sorted(self.minutes[code].items()) if t.date()==today and start<=t.time()<end]
        if len(selected)!=int((datetime.combine(today,end)-datetime.combine(today,start)).total_seconds()/60): return None
        return {'open':selected[0]['open'],'high':max(x['high'] for x in selected),'low':min(x['low'] for x in selected),'close':selected[-1]['close'],'volume':sum(x['volume'] for x in selected),'amount':sum(x['amount'] for x in selected)}

    def at_0900(self):
        # 09:00 僅確認已開盤者；其他候選會在09:05前補進
        notify('09:00 初選開始，日線候選依開盤價確認；僅盤前核實資格股')
        self.phase.add('0900')

    def at_0905(self):
        with self.lock:
            count=0
            for code in self.names:
                a=self.bars(code,dtime(9),dtime(9,5))
                if not a or code not in self.historical or not daily_filter(self.historical[code],self.opens.get(code,0)):continue
                avg=self.baselines.get(code,0)
                if avg<=0: continue
                rvol=a['volume']/avg
                span=a['high']-a['low']
                vwap=a['amount']/a['volume'] if a['volume'] else 0
                if span<=0:continue
                if not (a['close']<a['open'] and (a['open']-a['close'])/span>=.5 and
                        (a['close']-a['low'])/span<=.35 and a['close']<vwap and rvol>=1.5):continue
                self.candidates.add(code);count+=1
                notify(f'09:05 候選 {code} {self.names[code]} | RVOL {rvol:.2f} | VWAP {vwap:.2f}')
            notify(f'09:05 第一階段合格 {count} 檔')
            self.phase.add('0905')

    def at_0910(self):
        with self.lock:
            for code in self.candidates:
                a=self.bars(code,dtime(9),dtime(9,5));b=self.bars(code,dtime(9,5),dtime(9,10))
                if not a or not b or a['open']<=a['close']:continue
                rebound=(b['high']-a['close'])/(a['open']-a['close'])
                midpoint=(a['open']+a['close'])/2
                total_vol=a['volume']+b['volume']
                vwap=(a['amount']+b['amount'])/total_vol if total_vol else 0
                if not (.25<=rebound<=.75 and b['high']<=midpoint and b['high']<vwap
                        and b['close']<b['open'] and b['close']<midpoint and b['close']<vwap):continue
                entry=price_round(b['low']-tick_size(b['low']),'down')
                stop=price_round(b['high']+tick_size(b['high']),'up')
                risk=stop-entry
                if risk<=0 or risk/entry>.025:continue
                self.qualified[code]={'entry':entry,'stop':stop,'risk':risk}
                notify(f'09:10 反彈失敗 {code} {self.names[code]} | 跌破 {entry:.2f} 才模擬放空 | 停損 {stop:.2f}')
            notify(f'09:10 合格 {len(self.qualified)} 檔，等待09:20前破低')
            self.phase.add('0910')

    def _trigger(self,code,ts,price):
        s=self.qualified[code]
        if code in self.positions or self.entries>=self.cfg.max_entries or len(self.positions)>=self.cfg.max_positions:return
        if self.realized<=-self.cfg.capital*self.cfg.daily_loss_fraction:return
        if price>s['entry']:return
        if datetime.now(TZ)-ts>timedelta(seconds=10) and os.getenv('DEMO_MODE')!='1':return
        # 模擬賣出採不利一檔滑價；實際成交價可能更差
        entry=price_round(price-tick_size(price)*self.cfg.slippage_ticks,'down')
        risk=s['stop']-entry
        if risk<=0:return
        qty=int(self.cfg.capital*self.cfg.risk_fraction/risk//1000)*1000
        if qty<1000:return
        # 保守估算至少需有1.8R的下跌空間至昨日低點/近期支撐
        d=self.historical[code]
        support=float(d['low'].tail(20).min())
        if entry-support < self.cfg.min_rr*risk:return
        self.positions[code]={'entry':entry,'stop':s['stop'],'risk':risk,'qty':qty,'open_qty':qty,'first':False,'opened':ts.isoformat()}
        self.entries+=1
        notify(f'🔻模擬放空 {code} {self.names[code]} {entry:.2f} x {qty}股 | 停損{s["stop"]:.2f} | T1 {price_round(entry-1.5*risk,"down"):.2f} | T2 {price_round(entry-2.5*risk,"down"):.2f}')
        append_csv('paper_trades.csv',{'time':ts.isoformat(),'code':code,'action':'SHORT','price':entry,'shares':qty,'pnl':0,'reason':'breakdown'})

    def _watch_position(self,code,ts,price):
        p=self.positions.get(code)
        if not p:return
        if price>=p['stop']:
            self._exit(code,ts,price,p['open_qty'],'STOP');return
        if price<=p['entry']-2.5*p['risk']:
            self._exit(code,ts,price,p['open_qty'],'TARGET2');return
        if not p['first'] and price<=p['entry']-1.5*p['risk']:
            qty=(p['open_qty']//2000)*1000
            if qty>=1000:
                p['first']=True;self._exit(code,ts,price,qty,'TARGET1')

    def _exit(self,code,ts,price,qty,reason):
        p=self.positions[code]
        cover=price_round(price+tick_size(price)*self.cfg.slippage_ticks,'up')
        fees=(p['entry']+cover)*qty*self.cfg.fee_rate
        tax=p['entry']*qty*self.cfg.tax_rate
        pnl=(p['entry']-cover)*qty-fees-tax
        self.realized+=pnl;p['open_qty']-=qty
        append_csv('paper_trades.csv',{'time':ts.isoformat(),'code':code,'action':'COVER','price':cover,'shares':qty,'pnl':round(pnl,2),'reason':reason})
        notify(f'模擬回補 {code} {cover:.2f} x {qty}股 | {reason} | 淨損益 {pnl:,.0f} 元')
        if p['open_qty']==0:del self.positions[code]

    def force_close(self,now):
        for code in list(self.positions):
            ts=self.last_ts.get(code)
            if ts is None or now-ts>timedelta(seconds=60):
                notify(f'⚠ {code} 13:00行情過期，標記UNPRICED，不能假設已回補')
                append_csv('paper_trades.csv',{'time':now.isoformat(),'code':code,'action':'UNPRICED','price':'','shares':self.positions[code]['open_qty'],'pnl':'','reason':'stale_feed'})
            else:self._exit(code,now,self.last_price[code],self.positions[code]['open_qty'],'TIME_EXIT')
        self.halted=True


def read_baselines():
    p=ROOT/'baseline_5m.csv'
    if not p.exists():raise RuntimeError('Missing baseline_5m.csv')
    d=pd.read_csv(p,dtype={'code':str})
    return {str(r.code):float(r.avg_first5_shares) for _,r in d.iterrows() if float(r.avg_first5_shares)>0}

def run_live():
    import shioaji as sj
    from cloud_io import download_inputs
    download_inputs(ROOT)
    load_dotenv(ROOT/'.env')
    today=datetime.now(TZ).date()
    if datetime.now(TZ).time()>=dtime(8,55):raise RuntimeError('Must start before 08:55; late-start fails closed')
    if today.weekday()>=5 or today.isoformat() in set(os.getenv('TWSE_HOLIDAYS','').split(',')):raise RuntimeError('Not a trading day')
    names=load_eligibility(today)
    universe=pd.read_csv(ROOT/'universe.csv',dtype=str)['code'].tolist()
    names={c:n for c,n in names.items() if c in universe}
    if not names:raise RuntimeError('No overlap between verified eligibility and universe')
    baselines=read_baselines()
    cfg=Settings(capital=float(os.getenv('CAPITAL_TWD','500000')),max_subscriptions=int(os.getenv('MAX_SUBSCRIPTIONS','20')))
    api=sj.Shioaji(simulation=False)
    api.login(api_key=os.environ['SHIOAJI_API_KEY'],secret_key=os.environ['SHIOAJI_SECRET_KEY'])
    historical={};contracts={}
    for code in names:
        if code not in baselines:continue
        contract=api.Contracts.Stocks[code]
        if contract is None:continue
        try:
            k=api.kbars(contract,start=(today-timedelta(days=140)).isoformat(),end=(today-timedelta(days=1)).isoformat())
            d=pd.DataFrame({'ts':pd.to_datetime(k.ts),'open':k.Open,'high':k.High,'low':k.Low,'close':k.Close,'volume':k.Volume})
            d=d.sort_values('ts').drop_duplicates('ts').reset_index(drop=True)
            if len(d)>=65:historical[code]=d;contracts[code]=contract
        except Exception as exc:print('history failed',code,exc)
    if datetime.now(TZ).time()>=dtime(8,59):raise RuntimeError('History preparation too late; fail closed')
    # 限制訂閱數：按最近20日成交股數排序；不是全市場完整掃描
    ranked=sorted(contracts,key=lambda c:float(historical[c]['volume'].tail(20).mean()),reverse=True)[:cfg.max_subscriptions]
    if not ranked:raise RuntimeError('No qualified contracts with enough history and baseline')
    engine=Engine(cfg,{c:names[c] for c in ranked},baselines,{c:historical[c] for c in ranked})
    q=queue.Queue(maxsize=200000)
    @api.on_tick_stk_v1()
    def on_tick(*args):
        tick=args[-1]
        try:
            if getattr(tick,'intraday_odd',False) or getattr(tick,'simtrade',False):return
            dt=getattr(tick,'datetime',None)
            if dt is None:dt=datetime.fromisoformat(f'{tick.date} {tick.time}')
            if dt.tzinfo is None:dt=dt.replace(tzinfo=TZ)
            # TickSTKv1.volume 為成交股數，需以實際帳號行情核對
            q.put_nowait((str(tick.code),dt,float(tick.close),int(tick.volume),float(tick.amount)))
        except queue.Full:engine.halted=True
        except Exception as exc:print('tick error',exc,flush=True)
    for code in ranked:api.quote.subscribe(contracts[code],quote_type=sj.constant.QuoteType.Tick,version=sj.constant.QuoteVersion.v1)
    notify(f'訂閱 {len(ranked)} 檔。僅模擬，不送委託。')
    try:
        while True:
            now=datetime.now(TZ);clock=now.time()
            try:
                code,ts,price,shares,amount=q.get(timeout=.2)
                if now-ts>timedelta(seconds=15):engine.halted=True;notify('Tick 延遲超過15秒，停止新訊號')
                engine.ingest(code,ts,price,shares,amount)
            except queue.Empty:pass
            if dtime(9,0)<=clock and '0900' not in engine.phase:engine.at_0900()
            if dtime(9,5)<=clock and '0905' not in engine.phase:engine.at_0905()
            if dtime(9,10)<=clock and '0910' not in engine.phase:engine.at_0910()
            if dtime(13,0)<=clock:
                engine.force_close(now);break
    finally:
        try:
            from cloud_io import upload_results
            upload_results(ROOT)
        except Exception as exc: print('Cloud result upload failed:', exc, flush=True)
        for code in ranked:
            try:api.quote.unsubscribe(contracts[code],quote_type=sj.constant.QuoteType.Tick)
            except Exception:pass
        api.logout()

def run_demo():
    os.environ['DEMO_MODE']='1'
    today=datetime.now(TZ).date()
    # 構造完整的弱勢日線與合成 Tick（非真實行情）
    d=pd.DataFrame({'close':[100-i*.5 for i in range(70)],'volume':[4_000_000]*70})
    d['low']=d['close']-8;d['high']=d['close']+2
    e=Engine(Settings(),{'1234':'示例股票'},{'1234':150000},{'1234':d})
    e.at_0900()
    # 第一根：跌勢，第二根：反彈失敗；每分鐘各2筆
    vals=[(9,0,65.0,64.4),(9,1,64.3,63.9),(9,2,63.8,63.4),(9,3,63.3,63.0),(9,4,62.9,62.5),
          (9,5,62.7,63.0),(9,6,63.1,63.2),(9,7,63.1,62.9),(9,8,62.8,62.7),(9,9,62.6,62.5)]
    for h,m,a,b in vals:
        for sec,p in ((0,a),(30,b)):
            e.ingest('1234',datetime(today.year,today.month,today.day,h,m,sec,tzinfo=TZ),p,25000,p*25000)
    e.at_0905();e.at_0910()
    for minute,price in ((10,62.3),(11,61.8),(12,61.2),(13,60.7),(14,60.2)):
        e.ingest('1234',datetime(today.year,today.month,today.day,9,minute,tzinfo=TZ),price,10000,price*10000)
    e.force_close(datetime(today.year,today.month,today.day,13,0,tzinfo=TZ))
    notify('離線示範完成；檢查 paper_trades.csv')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--demo',action='store_true');group.add_argument('--live',action='store_true')
    args=parser.parse_args()
    run_demo() if args.demo else run_live()
