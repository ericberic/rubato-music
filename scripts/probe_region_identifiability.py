import json, numpy as np
from scipy.ndimage import gaussian_filter1d

p=json.load(open('data/scores/chopin_op11_movement_2/derived/cadenza_demos.machine.json'))
notes=p['notes']
passes=[[notes[0]]]
for a,b in zip(notes,notes[1:]):
    if b['beat']-a['beat']>3.0: passes.append([])
    passes[-1].append(b)
passes=passes[1:]                      # drop the wrong-beat first pass

STEP=0.1; LEN=10.4
Q=np.arange(0.0, LEN+STEP, STEP); N=len(Q)
KERNEL=0.35        # beats; emission smoothing
EPS=0.12           # uniform floor mixed into every emission
TEMPOS=np.array([0.6,0.75,0.9,1.0,1.1,1.3,1.6])

def emission_model(train):
    """P(pitch class | position) from demonstrations. (N,12)"""
    E=np.full((N,12), 1e-9)
    for ps in train:
        o=ps[0]['beat']
        for nt in ps:
            d=nt['beat']-o
            w=np.exp(-0.5*((Q-d)/KERNEL)**2)
            E[:, nt['pitch']%12]+=w
    E/=E.sum(1, keepdims=True)
    return (1-EPS)*E + EPS/12.0

def decode(E, test, tempo_scale=1.0):
    """Forward-backward over position x tempo. Uniform start prior."""
    o=test[0]['beat']
    times=np.array([(nt['beat']-o)*tempo_scale for nt in test])
    pcs=[nt['pitch']%12 for nt in test]
    M=len(TEMPOS)
    alpha=np.full((M,N), 1.0/(M*N))
    alphas=[]
    for k,(t,pc) in enumerate(zip(times,pcs)):
        if k>0:
            dt=t-times[k-1]
            nxt=np.zeros_like(alpha)
            for j,r in enumerate(TEMPOS):
                shift=dt*r/STEP
                lo=int(np.floor(shift)); frac=shift-lo
                a=alpha[j]
                s=np.zeros(N)
                if lo<N:
                    s[lo:]+= (1-frac)*a[:N-lo]
                if lo+1<N:
                    s[lo+1:]+= frac*a[:N-lo-1]
                sig=max(0.4, 0.18*dt*r/STEP)
                nxt[j]=gaussian_filter1d(s, sig, mode='constant')
            # slow tempo drift
            nxt=0.9*nxt+0.05*np.roll(nxt,1,axis=0)+0.05*np.roll(nxt,-1,axis=0)
            alpha=nxt
        alpha=alpha*E[:,pc][None,:]
        tot=alpha.sum()
        alpha = alpha/tot if tot>1e-300 else np.full((M,N),1.0/(M*N))
        alphas.append(alpha.copy())
    # backward
    beta=np.ones((M,N)); posts=[None]*len(times)
    for k in range(len(times)-1,-1,-1):
        g=alphas[k]*beta; g/=g.sum()
        posts[k]=g.sum(0)
        if k>0:
            b=beta*E[:,pcs[k]][None,:]
            dt=times[k]-times[k-1]
            nb=np.zeros_like(b)
            for j,r in enumerate(TEMPOS):
                shift=dt*r/STEP
                lo=int(np.floor(shift)); frac=shift-lo
                s=np.zeros(N)
                if lo<N: s[:N-lo]+=(1-frac)*b[j][lo:]
                if lo+1<N: s[:N-lo-1]+=frac*b[j][lo+1:]
                sig=max(0.4,0.18*dt*r/STEP)
                nb[j]=gaussian_filter1d(s,sig,mode='constant')
            beta=nb/max(nb.max(),1e-300)
    return times, np.array(posts)

print(f"{len(passes)} passes  |  grid {N} positions x {len(TEMPOS)} tempos")
print("\nLeave-one-out, PITCH-ONLY evidence, uniform start prior, full lookahead:\n")
allerr=[]
for h in range(len(passes)):
    E=emission_model([ps for i,ps in enumerate(passes) if i!=h])
    times,posts=decode(E, passes[h])
    est=posts@Q
    err=np.abs(est-times)
    allerr.append(err)
    late=err[len(err)//4:]     # after a few notes of burn-in
    print(f"  pass {h}: median err {np.median(err):5.3f} beats | "
          f"after burn-in {np.median(late):5.3f} | p90 {np.percentile(late,90):5.3f} | "
          f"max {late.max():5.3f}")
a=np.concatenate([e[len(e)//4:] for e in allerr])
print(f"\n  ALL, post burn-in: median {np.median(a):.3f}  p90 {np.percentile(a,90):.3f}  "
      f"frac within 0.5 beat {np.mean(a<0.5):.1%}  within 1.0 {np.mean(a<1.0):.1%}")
print(f"  chance baseline (uniform guess over {LEN} beats): {LEN/3:.2f} beats mean error")

# Offline probe: is the information in the recordings at all, regardless of compute?
# Answers, on the Chopin mvt II cadenza demos (see docs/LOG.md):
#   Q2 where in the region -- YES. Leave-one-out, pitch evidence only, uniform
#      start prior, full lookahead: median 0.02-0.05 beats. Controls: uniform
#      emissions 0.55, shuffled pitch 0.74-1.84, wide 30-beat grid 0.008-0.052.
#   Q1/Q3 entry & exit -- YES same-performer (held-out demo p=1.00 vs Eric's own
#      non-cadenza playing max p=0.141, zero FPs), NO cross-performer.
