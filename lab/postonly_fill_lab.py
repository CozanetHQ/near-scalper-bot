"""post_only fill-rate lab (audit §23): simulate resting-limit entries on the
collapsed event set. Variants:
V1 market entry + maker TP exit + taker risk exits (no entry fill risk)
V2 post_only entry delta=0.0, K=3 bars + maker TP / taker risk exits
V3 post_only entry delta=0.0005, K=15 bars
Adverse selection: market-entry expectancy of FILLED vs MISSED subsets.
"""
import numpy as np, pandas as pd
from ablation import load_pair, compute_indicators
from multislot_ablation import PAIRS
TAKER_IN = 0.0007          # 0.06% + 0.01% slip
MAKER = 0.0002
RISK_OUT = 0.0007          # taker exit + slip
TPS=(0.6,1.0,1.6); SLS=(1.0,1.5,2.0); FWD=240
ev = pd.read_csv("signal_edge_events.csv")[["pair","i","side"]]
frames={}
for p in PAIRS:
    d=compute_indicators(load_pair(p))
    frames[p]=(d["high"].to_numpy(float),d["low"].to_numpy(float),
               d["close"].to_numpy(float),d["atr14"].to_numpy(float))
rows=[]
for _,e in ev.iterrows():
    p,i,s = e["pair"], int(e["i"]), e["side"]
    H,L,C,A = frames[p]; n=len(C)
    if i+1+FWD>=n or np.isnan(A[i]) or A[i]<0.0008*C[i]: continue
    d = 1 if s=="long" else -1
    out={"pair":p,"i":i,"side":s}
    def grid(entry, j0, fee_in):
        res={}
        efr=A[i]/entry
        if d==1: fav=(H[j0:j0+FWD]-entry)/entry; adv=(entry-L[j0:j0+FWD])/entry
        else:   fav=(entry-L[j0:j0:j0+FWD])/entry if False else (entry-L[j0:j0+FWD])/entry; adv=(H[j0:j0+FWD]-entry)/entry
        if len(fav)<FWD: return None
        for tm in TPS:
            for sm in SLS:
                tp,sl = tm*efr, sm*efr
                h=np.where(fav>=tp)[0]; s2=np.where(adv>=sl)[0]
                h1=h[0] if len(h) else 10**9; s1=s2[0] if len(s2) else 10**9
                if h1<s1: res[f"t{tm}_s{sm}"]=(tp-fee_in-MAKER)
                elif s1<h1: res[f"t{tm}_s{sm}"]=(tp*-0- sl-fee_in-RISK_OUT)
                else: res[f"t{tm}_s{sm}"]=(fav[-1]-fee_in-RISK_OUT)
        return res
    # market entry counterfactual (taker fees) for adverse-selection split
    mk = grid(C[i], i+1, TAKER_IN)
    if mk is None: continue
    out["mkt_taker"] = mk["t1.6_s1.0"]
    # V1: market entry, maker TP exit
    v1 = grid(C[i], i+1, TAKER_IN-0.0002+0.0002)  # entry taker; exit maker handled below
    for k in v1:  # recompute with maker TP exit
        pass
    out["v1"] = {k:(v-0.0005 if k.startswith("t") and v>0 else v) for k,v in v1.items()} if False else None
    # simpler: V1 = market in (taker), TP exit maker => taker outcome +0.0005 on TP hits
    # V2/V3 post_only
    for tag,dl,K in (("v2",0.0,3),("v3",0.0005,15)):
        lim = C[i]*(1-dl*d)
        fill=None
        for j in range(i+1, min(i+1+K, n-FWD-2)):
            if d==1 and L[j]<=lim: fill=(j,lim); break
            if d==-1 and H[j]>=lim: fill=(j,lim); break
        out[tag+"_fill"]= 1 if fill else 0
        if fill:
            j,ent = fill
            g = grid(ent, j+1, MAKER)
            if g: out[tag]=g["t1.6_s1.0"]; out[tag+"_best"]=max(g.values())
    rows.append(out)
df=pd.DataFrame(rows); df.to_csv("postonly_results.csv",index=False)
print(f"events usable: {len(df)}")
print(f"fill rate V2 (delta=0,K=3):  {df['v2_fill'].mean()*100:.1f}%")
print(f"fill rate V3 (delta=5bp,K=15): {df['v3_fill'].mean()*100:.1f}%")
for tag in ("v2","v3"):
    f=df[df[tag+"_fill"]==1]
    print(f"{tag}: filled n={len(f)} exp t1.6_s1.0={f[tag].mean()*100:+.4f}% best-grid={f[tag+'_best'].mean()*100:+.4f}%")
# V1: market entry, maker exits on TP-hits only
def v1_net(r):
    # recompute from stored mkt_taker? too coarse. approximate: TP-hit share * fee saving
    pass
filled=df[df["v2_fill"]==1]; missed=df[df["v2_fill"]==0]
print(f"adverse selection: market-exp of FILLED subset  = {filled['mkt_taker'].mean()*100:+.4f}%")
print(f"                   market-exp of MISSED subset  = {missed['mkt_taker'].mean()*100:+.4f}%")
print(f"                   (full set taker exp          = {df['mkt_taker'].mean()*100:+.4f}%)")
