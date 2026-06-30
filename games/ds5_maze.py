#!/usr/bin/python3
# ds5_maze.py — walk a maze felt only through the DualSense, over Bluetooth.
#
#   Lightbar:     off = open ahead,  RED = wall ahead (blocked)
#   Player LEDs:  centre = you;  left bits = wall on your left;  right bits = wall right
#   Left stick:   left / right = turn 90deg,   up = step forward
#   Sound:        a "step" blip when you walk, an "oompf" thud when you hit a wall
#
# Optional debug view: a tiny web server on http://localhost:8119 renders the maze
# in 3D (three.js) with your position; the "Expert" button hides it (LEDs+sound only).
#
# Uses the speaker (Opus / 0x36) for sound, the 0x31 output report for the LEDs,
# and reads the left stick straight from /dev/hidraw. Stops the audio service while
# it runs (sole sender) and restarts it on exit.
#
#   ./ds5_maze.py [--size N] [--flip] [--no-web]

import os, sys, time, glob, zlib, struct, math, threading, random, select, curses
import base64, hashlib, socket

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "reverse_engineered"))
import ps5bt_membrane as D

# --- maze (recursive backtracker); open_dirs[y][x] = set of open dirs N=0 E=1 S=2 W=3 -
DX = [0, 1, 0, -1]; DY = [-1, 0, 1, 0]; OPP = [2, 3, 0, 1]
NAMES = "NESW"

def gen_maze(w, h):
    openn = [[set() for _ in range(w)] for _ in range(h)]
    seen = [[False] * w for _ in range(h)]
    # deterministic-ish but varied: simple LCG seeded by time passed in via stamp
    rnd = random.Random(os.getpid() ^ int(time.monotonic() * 1000) & 0xffffff)
    stack = [(0, 0)]; seen[0][0] = True
    while stack:
        x, y = stack[-1]
        nb = []
        for d in range(4):
            nx, ny = x + DX[d], y + DY[d]
            if 0 <= nx < w and 0 <= ny < h and not seen[ny][nx]:
                nb.append((d, nx, ny))
        if not nb:
            stack.pop(); continue
        d, nx, ny = rnd.choice(nb)
        openn[y][x].add(d); openn[ny][nx].add(OPP[d])
        seen[ny][nx] = True; stack.append((nx, ny))
    return openn

# --- LEDs -------------------------------------------------------------------------
def crc(b): return zlib.crc32(bytes([0xA2]) + b) & 0xFFFFFFFF

def led_report(seq, r, g, b, player):
    p = bytearray(78)
    p[0] = 0x31; p[1] = (seq << 4) & 0xF0; p[2] = 0x10
    p[4] = 0x04 | 0x10
    p[45] = 0x02; p[46] = player & 0x1F
    p[47] = r & 0xFF; p[48] = g & 0xFF; p[49] = b & 0xFF
    struct.pack_into("<I", p, 74, crc(bytes(p[:74])))
    return bytes(p)

# --- audio blips (pre-encoded Opus frames) ----------------------------------------
def make_blip(enc, freq, nframes, amp=11000, glide=1.0):
    frames = []; ph = 0.0; env = 1.0; f = freq
    for _ in range(nframes):
        pcm = bytearray()
        for _i in range(480):
            v = int(amp * env * math.sin(ph)); ph += 2 * math.pi * f / 48000
            pcm += struct.pack("<hh", v, v)
            env *= 0.9994; f *= glide
        frames.append(enc.encode(bytes(pcm), 480))
    return frames

ECHO_LOCK = threading.Lock()

# Echolokation mit 2D-Wellenphysik: ein Schnipp + Wand-Echos in alle 4 relativen
# Richtungen. Zwei Effekte aus der 2D-Akustik:
#  - Ausbreitung ~ 1/sqrt(r): Schall faellt in 2D langsamer ab als in 3D (1/r).
#  - Huygens versagt in geraden Dimensionen: jede Wellenfront schmiert mit
#    ~1/sqrt(t-t0) nach -> scharfe Ankunft + nachklingender Schwanz pro Reflexion.
# Schall ist skalar (keine Polarisation); L/R ist nur binaurale Hoerhilfe.
def make_echo(enc, maze, px, py, pdir, size):
    SR = 48000.0
    def raydist(d):
        x, y, n = px, py, 0
        while d in maze[y][x]:
            nx, ny = x + DX[d], y + DY[d]
            if not (0 <= nx < size and 0 <= ny < size): break
            x, y, n = nx, ny, n + 1
        return n                                   # offene Zellen bis zur Wand
    rel  = {'ahead': pdir, 'right': (pdir+1) % 4, 'back': (pdir+2) % 4, 'left': (pdir+3) % 4}
    pan  = {'ahead': 0.0, 'back': 0.0, 'left': -1.0, 'right': 1.0}
    gain = {'ahead': 1.0, 'right': 0.85, 'left': 0.85, 'back': 0.5}
    PER_CELL = 0.045                               # s pro Zelle (Hin+Rueck, ueberhoeht)
    echoes = [((raydist(d) + 1) * PER_CELL, gain[k] / math.sqrt(1.0 + raydist(d)), pan[k])
              for k, d in rel.items()]             # 1/sqrt(r): 2D-Ausbreitung
    WAKE = 0.14                                    # Laenge des 2D-Nachschmier-Schwanzes
    total = 0.05 + max(e[0] for e in echoes) + WAKE + 0.05
    N = int(total * SR); L = [0.0]*N; R = [0.0]*N
    rnd = random.Random(px*131 + py*17 + pdir)
    def emit(off, amp, panv, bright):
        lg = 1.0 - max(0.0, panv); rg = 1.0 + min(0.0, panv)
        tl = int(0.0025 * SR)                      # scharfe Wellenfront (Ankunft = Entfernung)
        for i in range(tl):
            j = off + i
            if j >= N: break
            e = (1 - i / tl) * amp
            v = e * (0.6 * math.sin(2*math.pi*2600*bright*i/SR) + 0.4*(rnd.random()*2-1))
            L[j] += v*lg; R[j] += v*rg
        wl = int(WAKE * SR)                        # 2D-Huygens-Schwanz: ~1/sqrt(t), gedeckelt
        for i in range(wl):
            j = off + i
            if j >= N: break
            t = i / SR
            shape = math.sqrt(0.004 / (t + 0.004))  # Peak 1 bei t=0, dann 1/sqrt(t)
            e = amp * 0.5 * shape * math.exp(-t / 0.06)
            nz = (rnd.random()*2-1) * e
            L[j] += nz*lg; R[j] += nz*rg
    emit(0, 0.75, 0.0, 1.0)                        # eigener Schnipp (hell, mittig) + 2D-Schwanz
    for delay, amp, panv in echoes:
        emit(int(delay * SR), amp * 0.7, panv, 0.5)  # gedaempfte Wand-Echos
    frames = []; pos = 0
    while pos < N:
        pcm = bytearray()
        for i in range(480):
            j = pos+i; l = L[j] if j < N else 0.0; r = R[j] if j < N else 0.0
            pcm += struct.pack('<hh', int(max(-1, min(1, l))*16000), int(max(-1, min(1, r))*16000))
        frames.append(enc.encode(bytes(pcm), 480)); pos += 480
    return frames

# --- shared state -----------------------------------------------------------------
class G:
    rgb = (0, 0, 0); mask = 0x04
    sound = []            # queue of opus frames to play
    silence = None        # one silence frame (trailing tail)
    lock = threading.Lock()
    maze = None; px = 0; py = 0; pdir = 0; stamp = 0
    stop = False

def push_sound(frames):
    with G.lock:
        # Blip + kurzer Stille-Ausklang; danach Funkstille (kein Dauerbrummen)
        G.sound = list(frames) + ([G.silence, G.silence] if G.silence else [])

# --- sender: 0x36 audio (sound/silence) + periodic 0x31 LED ------------------------
def sender(path, enc):
    fd = os.open(path, os.O_WRONLY)
    os.write(fd, D.build_speaker_setup())
    seq = cnt = 0; per = D.PERIOD; nt = time.monotonic(); n = 0
    while not G.stop:
        with G.lock:
            op = G.sound.pop(0) if G.sound else None   # None = nichts spielen
            r, g, b, m = (*G.rgb, G.mask)
        try:
            if op is not None:                         # Speaker NUR bei Sound treiben
                os.write(fd, D.build_0x36(seq, cnt, op))
                cnt = (cnt + 1) & 0xFF
            if n % 6 == 0:                             # LEDs unabhaengig ~15x/s
                os.write(fd, led_report(seq, r, g, b, m))
        except OSError:
            break
        seq = (seq + 1) & 0x0F; n += 1
        nt += per; sl = nt - time.monotonic()
        if sl > 0: time.sleep(sl)
        else: nt = time.monotonic()
    try: os.write(fd, led_report(0, 0, 0, 0, 0))
    except OSError: pass
    os.close(fd)

# --- left-stick reader (LX=[2], LY=[3]) -------------------------------------------
class Stick:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.lx = 128; self.ly = 128; self.btn = 0    # btn = buttons0 ([9]): Dreieck=0x80
    def run(self):
        while not G.stop:
            r, _, _ = select.select([self.fd], [], [], 0.03)
            if not r: continue
            try:
                while True:
                    b = os.read(self.fd, 128)
                    if not b or b[0] != 0x31: continue
                    if len(b) > 5 and b[3] == 0xd4: continue
                    if len(b) > 9:
                        self.lx, self.ly, self.btn = b[2], b[3], b[9]
            except BlockingIOError: pass
        os.close(self.fd)

# --- tiny web server: three.js maze view + WS state push (best effort) ------------
PAGE = """<!doctype html><html><head><meta charset=utf-8><title>DS5 Maze</title>
<style>body{margin:0;background:#d9d9d9;font-family:system-ui}#hud{position:fixed;top:8px;left:8px;
color:#222}button{font-size:14px;padding:6px 10px}</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script></head>
<body><div id=hud>DS5 Maze (debug) &nbsp;<button id=ex>Expert: View aus</button></div>
<script>
let sc=new THREE.Scene();sc.background=new THREE.Color(0xd9d9d9);
let cam=new THREE.PerspectiveCamera(55,innerWidth/innerHeight,0.1,200);
let rn=new THREE.WebGLRenderer({antialias:true});rn.setSize(innerWidth,innerHeight);
document.body.appendChild(rn.domElement);
addEventListener('resize',()=>{cam.aspect=innerWidth/innerHeight;cam.updateProjectionMatrix();rn.setSize(innerWidth,innerHeight);});
sc.add(new THREE.AmbientLight(0xffffff,0.7));let dl=new THREE.DirectionalLight(0xffffff,0.6);dl.position.set(5,10,7);sc.add(dl);
let walls=new THREE.Group(),player=null,W=1,H=1;sc.add(walls);
let DX=[0,1,0,-1],DZ=[-1,0,1,0];
function build(m){while(walls.children.length)walls.remove(walls.children[0]);
 H=m.length;W=m[0].length;
 let fl=new THREE.Mesh(new THREE.PlaneGeometry(W,H),new THREE.MeshStandardMaterial({color:0xeeeeee}));
 fl.rotation.x=-Math.PI/2;fl.position.set(W/2-0.5,0,H/2-0.5);sc.add(fl);
 let wm=new THREE.MeshStandardMaterial({color:0x3a7ca5});
 function wall(x,z,horiz){let g=new THREE.BoxGeometry(horiz?1:0.1,0.9,horiz?0.1:1);
  let me=new THREE.Mesh(g,wm);me.position.set(x,0.45,z);walls.add(me);}
 for(let y=0;y<H;y++)for(let x=0;x<W;x++){let o=m[y][x];
  if(!(o&1))wall(x,y-0.5,true); if(!(o&8))wall(x-0.5,y,false);
  if(y==H-1&&!(o&4))wall(x,y+0.5,true); if(x==W-1&&!(o&2))wall(x+0.5,y,false);} }
function setPlayer(x,z,dir){if(!player){player=new THREE.Mesh(new THREE.ConeGeometry(0.25,0.6,12),
 new THREE.MeshStandardMaterial({color:0xd98a2b}));sc.add(player);}
 player.position.set(x,0.4,z);player.rotation.y=-dir*Math.PI/2;
 cam.position.set(W/2-0.5,Math.max(W,H)*1.15,H+1.5);cam.lookAt(W/2-0.5,0,H/2-0.5);}
let view=true;document.getElementById('ex').onclick=()=>{view=!view;
 rn.domElement.style.display=view?'':'none';document.getElementById('ex').textContent=view?'Expert: View aus':'View an';};
function loop(){requestAnimationFrame(loop);if(view)rn.render(sc,cam);}loop();
let st=-1;function conn(){let ws=new WebSocket('ws://'+location.host+'/ws');
 ws.onmessage=e=>{let m=JSON.parse(e.data);if(m.stamp!=st){st=m.stamp;build(m.maze);}setPlayer(m.px,m.py,m.pdir);};
 ws.onclose=()=>setTimeout(conn,1000);}conn();
</script></body></html>"""

def ws_send(c, msg):
    d = msg.encode(); n = len(d)
    if n < 126: h = bytes([0x81, n])
    else: h = bytes([0x81, 126, (n >> 8) & 0xFF, n & 0xFF])
    try: c.sendall(h + d)
    except OSError: pass

def web_server(port):
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port)); srv.listen(8); srv.settimeout(0.5)
    except OSError:
        return
    clients = []
    last_push = 0.0
    while not G.stop:
        try:
            c, _ = srv.accept()
            req = c.recv(2048)
            if b"Sec-WebSocket-Key" in req:
                key = b""
                for line in req.split(b"\r\n"):
                    if line.lower().startswith(b"sec-websocket-key"):
                        key = line.split(b":")[1].strip()
                acc = base64.b64encode(hashlib.sha1(
                    key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + acc + b"\r\n\r\n")
                c.setblocking(False); clients.append(c)
            else:
                body = PAGE.encode()
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                          b"Content-Length: " + str(len(body)).encode() +
                          b"\r\nConnection: close\r\n\r\n" + body)
                c.close()
        except socket.timeout:
            pass
        except OSError:
            pass
        now = time.monotonic()
        if clients and now - last_push > 0.08:
            last_push = now
            import json
            with G.lock:
                msg = json.dumps({"stamp": G.stamp, "maze": G.maze,
                                  "px": G.px, "py": G.py, "pdir": G.pdir})
            for c in clients[:]:
                try: ws_send(c, msg)
                except OSError:
                    clients.remove(c)
    srv.close()

# --- game -------------------------------------------------------------------------
def main(scr):
    size = 7; flip = "--flip" in sys.argv; web = "--no-web" not in sys.argv
    if "--size" in sys.argv:
        size = max(3, min(16, int(sys.argv[sys.argv.index("--size") + 1])))
    path = D.find_dualsense_hidraw()
    if not path:
        scr.addstr(0, 0, "DualSense (BT) nicht gefunden."); scr.getch(); return

    os.system("systemctl --user stop ds5-membrane-sink.service 2>/dev/null")
    os.system("pkill -f ds5_membrane_sink 2>/dev/null")
    time.sleep(0.4)

    enc = D.OpusEncoder()
    G.silence = enc.encode(bytes(480 * 2 * 2), 480)
    step_snd = make_blip(enc, 520, 4, amp=9000)
    oompf_snd = make_blip(enc, 150, 8, amp=13000, glide=0.99955)
    turn_snd = make_blip(enc, 380, 2, amp=6000)

    maze = gen_maze(size, size)
    G.maze = [[sum(1 << d for d in maze[y][x]) for x in range(size)] for y in range(size)]
    G.px, G.py, G.pdir, G.stamp = 0, 0, 1, 1

    st = Stick(path)
    threading.Thread(target=st.run, daemon=True).start()
    threading.Thread(target=sender, args=(path, enc), daemon=True).start()
    if web:
        threading.Thread(target=web_server, args=(8119,), daemon=True).start()

    curses.curs_set(0); scr.nodelay(True); scr.timeout(20)
    DZ = 60; CN = 128; settled = True

    def open_dir(x, y, d):
        return d in maze[y][x]

    def render_leds():
        ahead = open_dir(G.px, G.py, G.pdir)
        left = open_dir(G.px, G.py, (G.pdir + 3) % 4)
        right = open_dir(G.px, G.py, (G.pdir + 1) % 4)
        m = 0x04                                   # Mitte = du
        if not left:  m |= 0x03 if not flip else 0x18
        if not right: m |= 0x18 if not flip else 0x03
        G.mask = m
        G.rgb = (0, 0, 0) if ahead else (255, 0, 0)
        return ahead, left, right

    def step():
        if open_dir(G.px, G.py, G.pdir):
            G.px += DX[G.pdir]; G.py += DY[G.pdir]
            push_sound(step_snd); return True
        push_sound(oompf_snd); return False

    def fire_echo():                               # Echolokation (im Thread, mit Lock)
        try: push_sound(make_echo(enc, maze, G.px, G.py, G.pdir, size))
        finally: ECHO_LOCK.release()

    tri_prev = False
    while not G.stop:
        ahead, left, right = render_leds()
        # Stick -> Aktionen (flankengetriggert: erst zurueck zur Mitte)
        lx, ly = st.lx, st.ly
        act = None
        if settled:
            if ly < CN - DZ: act = "fwd"
            elif lx < CN - DZ: act = "left"
            elif lx > CN + DZ: act = "right"
        if abs(lx - CN) < 30 and abs(ly - CN) < 30: settled = True
        if act:
            settled = False
            if act == "fwd": step()
            elif act == "left": G.pdir = (G.pdir + 3) % 4; push_sound(turn_snd)
            elif act == "right": G.pdir = (G.pdir + 1) % 4; push_sound(turn_snd)

        ch = scr.getch()
        if ch in (ord('q'), 27): break
        # Tastatur-Fallback
        elif ch == curses.KEY_UP: step()
        elif ch == curses.KEY_LEFT: G.pdir = (G.pdir + 3) % 4; push_sound(turn_snd)
        elif ch == curses.KEY_RIGHT: G.pdir = (G.pdir + 1) % 4; push_sound(turn_snd)

        # Dreieck (oder [t]) = Schnipp/Echolokation
        tri = bool(st.btn & 0x80)
        if (tri and not tri_prev) or ch == ord('t'):
            if ECHO_LOCK.acquire(blocking=False):
                threading.Thread(target=fire_echo, daemon=True).start()
        tri_prev = tri

        if G.px == size - 1 and G.py == size - 1:
            scr.erase(); scr.addstr(1, 2, "ZIEL erreicht! Neues Maze: [n]   [q] quit", curses.A_BOLD)
            scr.refresh()
            G.rgb = (0, 255, 0)
            while True:
                k = scr.getch()
                if k in (ord('q'), 27): G.stop = True; break
                if k == ord('n'):
                    maze = gen_maze(size, size)
                    G.maze = [[sum(1 << d for d in maze[y][x]) for x in range(size)] for y in range(size)]
                    G.px, G.py, G.pdir = 0, 0, 1; G.stamp += 1; break
                time.sleep(0.03)
            continue

        # Terminal-Top-Down-Debug (randsicher)
        def sput(y, x, s, a=0):
            my, mx = scr.getmaxyx()
            if 0 <= y < my and x < mx:
                try: scr.addstr(y, x, s[:mx - x - 1], a)
                except curses.error: pass
        scr.erase()
        sput(0, 2, "DS5 MAZE  Stick: vor/l/r  Dreieck=Schnipp(Echo)  [t]Echo [q]quit  Web:localhost:8119")
        arrow = "^>v<"[G.pdir]
        for y in range(size):
            top = ""; mid = ""
            for x in range(size):
                o = G.maze[y][x]                    # Bitmaske (N=1,E=2,S=4,W=8)
                top += "+" + ("  " if (o & 1) else "--")
                cell = (" " + arrow + " ") if (x == G.px and y == G.py) else "   "
                cell = (" Z ") if (x == size - 1 and y == size - 1 and not (x == G.px and y == G.py)) else cell
                mid += ("  " if (o & 8) else "| ") + cell[1]
            sput(2 + y * 2, 2, top + "+")
            sput(3 + y * 2, 2, mid + "|")
        sput(3 + size * 2, 2,
             "voraus: %s   links: %s   rechts: %s   (btn=0x%02X)" %
             ("frei" if ahead else "WAND", "frei" if left else "Wand",
              "frei" if right else "Wand", st.btn))
        scr.refresh()
        time.sleep(0.015)

    G.stop = True; time.sleep(0.15)


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    finally:
        G.stop = True
        os.system("systemctl --user start ds5-membrane-sink.service 2>/dev/null")
        print("Audio-Service wieder gestartet.")
