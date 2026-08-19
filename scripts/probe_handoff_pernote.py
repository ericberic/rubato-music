import warnings, json, numpy as np, mido; warnings.filterwarnings('ignore')
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
D='data/scores/chopin_op11_movement_2/derived/cadenza_demos.machine.json'
n=json.load(open(D))['notes']; passes=[[n[0]]]
for a,b in zip(n,n[1:]):
    if b['beat']-a['beat']>3.0: passes.append([])
    passes[-1].append(b)
passes=passes[1:]
mid=mido.MidiFile('/Users/ehuang/code/Rubato/runs/take-6ca80e42/input/solo.mid')
neg=[]
for tr in mid.tracks:
    t=0
    for m in tr:
        t+=m.time
        if m.type=='note_on' and m.velocity>0: neg.append((t/mid.ticks_per_beat,m.note))
neg.sort()
TAUS=[0.25,0.5,1.0,2.0,4.0,8.0]
def feat(seq):                      # PER-NOTE, no clustering anywhere
    v=np.zeros((len(TAUS),12)); rows=[]; prev=None
    for b,p in seq:
        if prev is not None:
            d=np.exp(-(b-prev)/np.array(TAUS)); v*=d[:,None]
        v[:,p%12]+=1.0; prev=b
        rows.append((v/np.maximum(v.sum(1,keepdims=True),1e-9)).ravel())
    return np.array(rows)
def warp(seq,k):
    o=seq[0][0]; t=np.array([b-o for b,_ in seq]); T=t[-1]
    w={'none':t,'fast':t*0.7,'slow':t*1.4,'rit':t+0.35*t**2/T,
       'rubato':t+0.6*np.sin(2*np.pi*t/T)}[k]
    return [(o+float(x),p) for x,(_,p) in zip(w,seq)]
Fn=feat(neg)
def run(lead,kind):
    before=[]; prem=0; fp=[]; miss=0
    for h in range(len(passes)):
        X=[];y=[]
        for i,ps in enumerate(passes):
            if i==h: continue
            s=[(x['beat'],x['pitch']) for x in ps]; o=s[0][0]; e=s[-1][0]-o
            X.append(feat(s)); y+=[1 if (b-o)>=e-lead else 0 for b,_ in s]
        X.append(Fn); y+=[0]*len(Fn)
        clf=make_pipeline(StandardScaler(),LogisticRegression(max_iter=2000,C=2.0)).fit(np.vstack(X),np.array(y))
        s=[(x['beat'],x['pitch']) for x in passes[h]]; o=s[0][0]; e=s[-1][0]-o
        off=np.array([b-o for b,_ in s])
        p=clf.predict_proba(feat(warp(s,kind)))[:,1]
        hit=np.where(p>0.5)[0]
        if not len(hit): miss+=1; continue
        before.append(e-off[hit[0]])
        prem+=int(((p>0.5)&(off<e-lead-0.5)).sum())
        fp.append(float((clf.predict_proba(Fn)[:,1]>0.5).mean()))
    b=np.array(before) if before else np.array([np.nan])
    return f"{np.nanmedian(b):6.2f}b  prem{prem:3d}  FP{np.mean(fp) if fp else 0:5.1%}  miss{miss}/8"
print("PER-NOTE handoff detector (no clustering) -- warning before landing note\n")
print(f"{'label':>12s}"+"".join(f"{k:>28s}" for k in ('none','fast','rit','rubato')))
for L in (1.0,2.0,3.0):
    print(f"  last {L:.1f} b "+"".join(f"{run(L,k):>28s}" for k in ('none','fast','rit','rubato')))
