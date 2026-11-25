from probe import *
import collections
x0,z0=432-60,2057-60
b=box(x0,z0,x0+120,z0+120)
np.save("s26.npy",b); pickle.dump(NAMES,open("s26names.pkl","wb"))
ys=np.nonzero(b.any(axis=(1,2)))[0]; print("y range",ys.min()-64,ys.max()-64)
# top block per column
top=np.where(b.any(0), b.shape[0]-1-np.argmax(b[::-1]!=0,axis=0),-1)
tn=b[top, np.arange(120)[:,None], np.arange(120)[None,:]]
c=collections.Counter(NAMES[i] for i in tn.ravel()); print(c.most_common(40))
