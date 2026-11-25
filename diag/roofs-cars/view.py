from probe import *
import pickle, sys

b=np.load(sys.argv[1]); N=pickle.load(open(sys.argv[2],"rb"))
import re
SKIPR=re.compile(r"short_grass|tall_grass|fern|flower|tulip|poppy|dandelion|orchid|allium|bluet|daisy|cornflower|lily|bush|sapling")
keep=np.array([0 if (n=="air" or SKIPR.search(n)) else 1 for n in N])
s=keep[b]; top=np.where(s.any(0), s.shape[0]-1-np.argmax(s[::-1]!=0,axis=0),0)
img=((top-top.min())*255//max(1,np.ptp(top))).astype(np.uint8)
savepng(sys.argv[3],img)
print(top.min()-64, top.max()-64)
