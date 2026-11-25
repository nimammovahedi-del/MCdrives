import re, glob, os, collections
from schem import load
src=open("/home/claude/irvine/arnis/src/structures/schematic.rs").read()
body=src[src.index("fn map_structure_block"):src.index("fn parse_state")]
arm={}
for m in re.finditer(r'((?:"[a-z_]+"\s*\|\s*)*"[a-z_]+")\s*=>\s*([A-Z_0-9]+)', body):
    for k in re.findall(r'"([a-z_]+)"', m.group(1)): arm[k]=m.group(2)
bd=open("/home/claude/irvine/arnis/src/block_definitions.rs").read()
ids={}
for a,b,c in re.findall(r"^\s*(\d+)(?:\.\.=(\d+))? => \"([a-z_]+)\",", bd, re.M):
    for i in range(int(a),int(b or a)+1): ids[i]=c
const={a:ids[int(b)] for a,b in re.findall(r'pub const ([A-Z_0-9]+): Block = Block::new\((\d+)\)', bd)}
out={}
for p in sorted(glob.glob("/home/claude/irvine/arnis/assets/structures/car_*.schem")):
    W,H,L,a,inv=load(p)
    vox=[]
    for (y,z,x),v in __import__("numpy").ndenumerate(a):
        n=inv[int(v)].replace("minecraft:",""); base=n.split("[")[0]
        if base not in arm: continue
        vox.append((x,y,z,const[arm[base]], n))
    miny=min(v[1] for v in vox)
    out[os.path.basename(p)[4:-6]]=(W,L,[(x,y-miny,z,nm,full) for x,y,z,nm,full in vox])
    print(os.path.basename(p), W,L, len(vox), collections.Counter(v[3] for v in vox).most_common())
import pickle; pickle.dump(out,open("cars.pkl","wb"))
