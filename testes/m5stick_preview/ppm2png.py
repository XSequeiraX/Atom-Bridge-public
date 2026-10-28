# Junta os shot_*.ppm de uma pasta numa folha de contacto PNG (ampliada 2x): python3 ppm2png.py <pasta> <saida.png>
import glob, struct, sys, zlib
pasta, saida = sys.argv[1], sys.argv[2]
def ler(fn):
    d = open(fn, 'rb').read(); p = d.split(b'\n', 3); w, h = map(int, p[1].split()); return w, h, p[3]
fich = sorted(glob.glob(pasta + '/shot_*.ppm'))
imgs = [ler(f) for f in fich]; cols, esc, pad = 3, 2, 8
W = cols * (240 * esc + pad) + pad; rows = (len(imgs) + cols - 1) // cols; H = rows * (135 * esc + pad) + pad
buf = bytearray(b'\x30\x30\x30' * (W * H))
for n, (w, h, p) in enumerate(imgs):
    ox = pad + (n % cols) * (240 * esc + pad); oy = pad + (n // cols) * (135 * esc + pad)
    for y in range(h):
        for sy in range(esc):
            row = bytearray()
            for x in range(w): row += p[(y * w + x) * 3:(y * w + x) * 3 + 3] * esc
            o = ((oy + y * esc + sy) * W + ox) * 3; buf[o:o + len(row)] = row
raw = b''.join(b'\x00' + bytes(buf[y * W * 3:(y + 1) * W * 3]) for y in range(H))
def ch(t, c): return struct.pack('>I', len(c)) + t + c + struct.pack('>I', zlib.crc32(t + c) & 0xffffffff)
open(saida, 'wb').write(b'\x89PNG\r\n\x1a\n' + ch(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0)) + ch(b'IDAT', zlib.compress(raw)) + ch(b'IEND', b''))
print(saida, len(imgs), 'paginas')
