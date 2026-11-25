"""Bundle per-tile .bin files into base64 JSON files the artifact host can serve."""
import json, os, base64, sys
d = sys.argv[1]; m = json.load(open(f'{d}/meta.json')); out = bytearray()
for t in m['tiles']:
    b = open(f"{d}/t_{t[0]}_{t[1]}.bin", 'rb').read(); t.append(len(out)); t.append(len(b)); out += b; os.remove(f"{d}/t_{t[0]}_{t[1]}.bin")
json.dump(m, open(f'{d}/meta.json', 'w'))
json.dump({'base64': base64.b64encode(bytes(out)).decode()}, open(f'{d}/world.json', 'w'))
json.dump({'base64': base64.b64encode(open(f'{d}/height.bin', 'rb').read()).decode()}, open(f'{d}/height.json', 'w')); os.remove(f'{d}/height.bin')
