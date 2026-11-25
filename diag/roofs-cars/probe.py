import sys, os, zlib, gzip, io, math, numpy as np, nbtlib, pickle
W="/home/claude/world-r5c5/world/Arnis World 1/region"
def read_region(path, want=None):
    data=open(path,"rb").read()
    for i in range(1024):
        if want is not None and i not in want: continue
        off=int.from_bytes(data[i*4:i*4+3],"big"); cnt=data[i*4+3]
        if off==0 or cnt==0: continue
        p=off*4096; ln=int.from_bytes(data[p:p+4],"big"); comp=data[p+4]; raw=data[p+5:p+4+ln]
        raw=zlib.decompress(raw) if comp==2 else gzip.decompress(raw) if comp==1 else raw
        yield nbtlib.File.parse(io.BytesIO(raw))
def unpack(longs,bits,n=4096):
    arr=np.array(longs,dtype=np.int64).view(np.uint64); per=64//bits
    sh=np.arange(per,dtype=np.uint64)*np.uint64(bits)
    return ((arr[:,None]>>sh[None,:])&np.uint64((1<<bits)-1)).reshape(-1)[:n].astype(np.int32)
NAMES=[]; NID={}
def nid(s):
    if s not in NID: NID[s]=len(NAMES); NAMES.append(s)
    return NID[s]
nid("air")
def box(x0,z0,x1,z1,y0=-64,y1=320):
    """returns ids [y,z,x] and NAMES list (name[props])"""
    out=np.zeros((y1-y0,z1-z0,x1-x0),dtype=np.int32)
    for rx in range(x0//512,(x1-1)//512+1):
        for rz in range(z0//512,(z1-1)//512+1):
            want=set()
            for cx in range(max(x0,rx*512)//16,(min(x1,rx*512+512)-1)//16+1):
                for cz in range(max(z0,rz*512)//16,(min(z1,rz*512+512)-1)//16+1):
                    want.add((cz-rz*32)*32+(cx-rx*32))
            for ch in read_region(f"{W}/r.{rx}.{rz}.mca",want):
                root=ch.get("") if "" in ch else ch
                cx,cz=int(root["xPos"]),int(root["zPos"])
                for s in root.get("sections",[]):
                    bs=s.get("block_states")
                    if bs is None: continue
                    pal=[str(q["Name"]).split(":")[-1]+("["+",".join(f"{k}={v}" for k,v in sorted(q["Properties"].items()))+"]" if q.get("Properties") else "") for q in bs["palette"]]
                    ids=np.array([nid(p) for p in pal])
                    v=ids[unpack(bs["data"],max(4,math.ceil(math.log2(len(pal)))))] if "data" in bs else np.full(4096,ids[0])
                    v=v.reshape(16,16,16); sy=int(s["Y"])*16
                    ya,yb=max(y0,sy),min(y1,sy+16)
                    if ya>=yb: continue
                    xa,xb=max(x0,cx*16),min(x1,cx*16+16); za,zb=max(z0,cz*16),min(z1,cz*16+16)
                    if xa>=xb or za>=zb: continue
                    out[ya-y0:yb-y0,za-z0:zb-z0,xa-x0:xb-x0]=v[ya-sy:yb-sy,za-cz*16:zb-cz*16,xa-cx*16:xb-cx*16]
    return out
import struct as _st
def savepng(path, a, scale=6):
    a=np.asarray(a,dtype=np.uint8)
    if a.ndim==2: a=np.stack([a]*3,-1)
    a=np.repeat(np.repeat(a,scale,0),scale,1); h,w,_=a.shape
    raw=b"".join(b"\0"+a[i].tobytes() for i in range(h))
    ch=lambda t,d: _st.pack(">I",len(d))+t+d+_st.pack(">I",zlib.crc32(t+d)&0xffffffff)
    open(path,"wb").write(b"\x89PNG\r\n\x1a\n"+ch(b"IHDR",_st.pack(">IIBBBBB",w,h,8,2,0,0,0))+ch(b"IDAT",zlib.compress(raw,6))+ch(b"IEND",b""))
