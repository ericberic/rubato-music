import json, numpy as np, mido
from sklearn.linear_model import LogisticRegression

p=json.load(open('data/scores/chopin_op11_movement_2/derived/cadenza_demos.machine.json'))
notes=p['notes']
passes=[[notes[0]]]
for a,b in zip(notes,notes[1:]):
    if b['beat']-a['beat']>3.0: passes.append([])
    passes[-1].append(b)
passes=passes[1:]

TAUS=[0.25,0.5,1.0,2.0,4.0,8.0]      # beats; multi-scale memory of "notes so far"
BIN=0.25; NBIN=int(round(10.5/BIN))   # 42 quarter-beat classes
OUT=NBIN                              # one extra class: not in the region

def features(seq):
    """Causal: feature k uses only notes 0..k. No absolute elapsed time."""
    v=np.zeros((len(TAUS),12)); rows=[]; prev=None
    for b,pitch in seq:
        if prev is not None:
            dt=b-prev
            for i,t in enumerate(TAUS): v[i]*=np.exp(-dt/t)
        v[:,pitch%12]+=1.0; prev=b
        n=v/np.maximum(v.sum(1,keepdims=True),1e-9)
        rows.append(np.concatenate([n.ravel(), [np.log1p(v.sum(1)).mean()]]))
    return np.array(rows)

def label(off): return min(int(off/BIN), NBIN-1)

# negatives: Eric's own non-cadenza playing
mid=mido.MidiFile('runs/take-6ca80e42/input/solo.mid')
neg=[]
for tr in mid.tracks:
    t=0
    for m in tr:
        t+=m.time
        if m.type=='note_on' and m.velocity>0: neg.append((t/mid.ticks_per_beat,m.note))
neg.sort(); Xneg=features(neg)

errs=[]; hits=[]; fps=[]
for h in range(len(passes)):
    X=[];y=[]
    for i,ps in enumerate(passes):
        if i==h: continue
        o=ps[0]['beat']; seq=[(n['beat'],n['pitch']) for n in ps]
        X.append(features(seq)); y+= [label(b-o) for b,_ in seq]
    X.append(Xneg); y+=[OUT]*len(Xneg)
    clf=LogisticRegression(max_iter=3000,C=2.0).fit(np.vstack(X), np.array(y))

    ps=passes[h]; o=ps[0]['beat']; seq=[(n['beat'],n['pitch']) for n in ps]
    P=clf.predict_proba(features(seq))
    cls=clf.classes_
    inreg=cls<NBIN
    pred=cls[np.argmax(P,axis=1)]
    true=np.array([b-o for b,_ in seq])
    ok=pred<NBIN
    est=np.where(ok, pred*BIN+BIN/2, np.nan)
    e=np.abs(est-true)[ok]
    errs.append(e); hits.append(ok.mean())
    Pn=clf.predict_proba(Xneg); fps.append((Pn[:,inreg].sum(1)>0.5).mean())
    print(f"  pass {h}: in-region called {ok.mean():5.1%} | median |err| {np.median(e):5.3f} b | "
          f"within 0.5b {np.mean(e<0.5):5.1%} | within 1b {np.mean(e<1.0):5.1%}")

a=np.concatenate(errs)
print(f"\nALL causal, notes-so-far only, leave-one-out:")
print(f"  median |err| {np.median(a):.3f} beats | p90 {np.percentile(a,90):.3f} | "
      f"within 0.5b {np.mean(a<0.5):.1%} | within 1b {np.mean(a<1.0):.1%}")
print(f"  region correctly entered on {np.mean(hits):.1%} of notes")
print(f"  false-positive rate on Eric's non-cadenza playing: {np.mean(fps):.2%}")
print(f"  chance: {NBIN} bins -> {1/NBIN:.1%} exact, ~3.5 beats mean error")
