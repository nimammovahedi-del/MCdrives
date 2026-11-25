import nbtlib, numpy as np, glob, os
def load(p):
    f=nbtlib.load(p); r=f.get("Schematic",f)
    if "Blocks" in r: r2=r["Blocks"]; pal=r2["Palette"]; data=r2["Data"]
    else: pal=r["Palette"]; data=r["BlockData"]
    W,H,L=int(r["Width"]),int(r["Height"]),int(r["Length"])
    inv={int(v):str(k) for k,v in pal.items()}
    # varint decode
    b=bytes(np.array(data,dtype=np.int8).view(np.uint8)); out=[];i=0
    while i<len(b):
        v=0;s=0
        while True:
            c=b[i];i+=1;v|=(c&127)<<s;s+=7
            if not c&128: break
        out.append(v)
    a=np.array(out).reshape(H,L,W)
    return W,H,L,a,inv
if __name__=="__main__":
    for p in sorted(glob.glob("/home/claude/irvine/arnis/assets/structures/car_*.schem")):
        W,H,L,a,inv=load(p)
        import collections
        c=collections.Counter(inv[v].replace("minecraft:","") for v in a.ravel())
        print(os.path.basename(p),W,H,L, dict(c))
