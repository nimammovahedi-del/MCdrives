from probe import *
import pickle, collections, sys
cars=pickle.load(open(os.path.join(os.path.dirname(__file__),"cars.pkl"),"rb"))
x0,z0,x1,z1=[int(v) for v in sys.argv[1:5]]
b=box(x0,z0,x1,z1,-64,112)
base=[n.split("[")[0] for n in NAMES]
bid={}
for i,n in enumerate(base): bid.setdefault(n,[]).append(i)
bn=np.array([sorted(set(base)).index(n) for n in base]); U=sorted(set(base)); B=bn[b]
def rot(x,z,w,l,k): return [(x,z),(l-1-z,x),(w-1-x,l-1-z),(z,w-1-x)][k&3]
hits=[]
for name,(W,L,vox) in cars.items():
    for k in range(4):
        ax,az=rot(W//2,L//2,W,L,k)
        off=[(y, rot(x,z,W,L,k)[1]-az, rot(x,z,W,L,k)[0]-ax, U.index(nm) if nm in U else -1) for x,y,z,nm,_ in vox]
        # candidates: positions of first voxel's name
        cnt=collections.Counter(o[3] for o in off)
        y0,dz0,dx0,n0=min(off,key=lambda o: (o[3]<0, (B==o[3]).sum() if o[3]>=0 else 1e9))
        cy,cz,cx=np.nonzero(B==n0)
        by,bz,bx=cy-y0,cz-dz0,cx-dx0
        sc=np.zeros(len(by),int)
        for y,dz,dx,n in off:
            yy,zz,xx=by+y,bz+dz,bx+dx
            ok=(yy>=0)&(yy<B.shape[0])&(zz>=0)&(zz<B.shape[1])&(xx>=0)&(xx<B.shape[2])
            m=np.zeros(len(by),bool); m[ok]=B[yy[ok],zz[ok],xx[ok]]==n; sc+=m
        good=sc>=0.5*len(off)
        for i in np.nonzero(good)[0]: hits.append((name,k,int(bx[i])+x0,int(by[i])-64,int(bz[i])+z0,int(sc[i]),len(off)))
print(len(hits)); 
fr=collections.Counter(round(h[5]/h[6],1) for h in hits); print(sorted(fr.items()))
for h in hits[:15]: print(h)
pickle.dump(hits,open("hits.pkl","wb"))
