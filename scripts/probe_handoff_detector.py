import warnings; warnings.filterwarnings('ignore')
exec(open('/tmp/evt2.py').read().split('def evtfeat')[0])

def feats(cs):
    """Features from a FIXED cluster list, so warping cannot change segmentation."""
    cum=np.zeros(NP); ch=np.zeros(12); rows=[]; sizes=[]; allp=[]
    for _,ps in cs:
        for p in ps:
            if p in PI: cum[PI[p]]+=1
            ch[p%12]+=1; allp.append(p)
        sizes.append(len(ps)); a=np.array(allp)
        rows.append([*(cum/max(cum.sum(),1)), *(ch/max(ch.sum(),1)),
            *(([0.0]*8+[float(s) for s in sizes[-8:]])[-8:]),
            np.log1p(len(sizes)), np.log1p(len(allp)),
            (a.mean()-60)/24,(a.min()-60)/24,(a.max()-60)/24])
    return np.array(rows)

NEG=[(b,[p]) for b,p in neg]
def run(lead):
    before=[]; prem=0; fp=[]; miss=0
    for h in range(len(passes)):
        X=[];y=[]
        for i,ps in enumerate(passes):
            if i==h: continue
            cs=cluster([(n['beat'],n['pitch']) for n in ps]); o=cs[0][0]; end=cs[-1][0]-o
            X.append(feats(cs)); y+=[1 if (c[0]-o)>=end-lead else 0 for c in cs]
        X.append(feats(NEG)); y+=[0]*len(NEG)
        clf=make_pipeline(StandardScaler(),LogisticRegression(max_iter=3000,C=2.0)).fit(np.vstack(X),np.array(y))
        cs=cluster([(n['beat'],n['pitch']) for n in passes[h]]); o=cs[0][0]
        end=cs[-1][0]-o; off=np.array([c[0]-o for c in cs])
        p=clf.predict_proba(feats(cs))[:,1]
        hit=np.where(p>0.5)[0]
        if not len(hit): miss+=1; continue
        before.append(end-off[hit[0]])                     # beats BEFORE the landing note
        prem+=int(((p>0.5)&(off<end-lead-0.5)).sum())
        fp.append(float((clf.predict_proba(feats(NEG))[:,1]>0.5).mean()))
    b=np.array(before)
    print(f"  label='last {lead:.1f} beat(s)': fires {np.median(b):.3f} b BEFORE the landing note "
          f"(range {b.min():.2f}..{b.max():.2f}) | premature {prem} | FP {np.mean(fp):.1%} | missed {miss}/8")

print("How much warning does the orchestra get?  (8-fold leave-one-out)\n")
for L in (0.5,1.0,1.5,2.0,3.0): run(L)
print("\nAt 60 BPM 1 beat = 1.0 s. Landing note is the last event of the cadenza.")

# The cadenza reduced to what the orchestra actually needs: not "where is the
# beat" but "are we at the handoff". 169 position classes collapse to 2, against
# the same ~600 training events.
#
# Measured, 8-fold leave-one-out on the performer's own demonstrations:
#   binary handoff detector : 0/8 missed, 0 premature fires, 0% FP on his other
#     playing; fires ~0.29 beats before the landing note, and the orchestra's
#     next event is a further beat later -- ample MIDI scheduling headroom.
#   lead-time sweep -- precision holds at EVERY horizon, contrary to expectation:
#     label 'last 0.5b'/'1.0b' -> fires 0.29 beats before the landing note
#     label 'last 1.5b'/'2.0b' -> 1.31 beats
#     label 'last 3.0b'        -> 2.28 beats
#     all with 0 premature fires, 0% FP, 0/8 missed. Fire times cluster at three
#     values rather than varying smoothly: the detector is recognising three
#     distinct landmark chords near the end, not estimating a countdown. The
#     horizon choice is therefore "which landmark to trigger on".
#   abort signal (piano must be silent in the m.104 interlude):
#     correct fire  -> 0 notes in the following beat (8/8)
#     premature fire-> median 6, MINIMUM 3 notes (554/554)
#     zero overlap; >=2 notes vetoes 100% of premature entries, 0% false aborts.
#
# So the detector may fire eagerly and be retracted within one beat. That is the
# graceful degradation lost when position tracking was dropped.
#
# Latency, measured single-threaded on an idle machine:
#     binary handoff inference  35 us
#     feature update per chord  41 us
#     TOTAL per chord-event     76 us  (2.8 ms for the whole 10 s cadenza)
# Three orders of magnitude under any plausible budget, so run on every chord
# event -- when information actually arrives -- rather than on a fixed clock. A
# timer that fires mid-chord would classify a partially-arrived event, which is
# the exact bug that broke the anchor chain. Both costs are Python interpreter
# overhead, not arithmetic.
#
# KNOWN DEFECT: the 50 ms cluster window is not tempo-safe. At 0.7x, chords
# 60 ms apart collapse to 42 ms and merge, changing the event count (37 -> 33)
# and every downstream feature. Needs a tighter or adaptive window.
