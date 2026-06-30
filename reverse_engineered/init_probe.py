#!/usr/bin/python3
# init_probe.py — DualSense Audio-Init-Sondierer (Root-Cause: d4-Feedback-Stream).
#
# Sendet kontinuierlich einen 0x36-Audio-Stream (Stille) und laesst ALLE Init-/
# Config-Bytes live durchschalten. Parallel wird /dev/hidraw gelesen und 1x/s die
# empfangene Rate angezeigt: GAMEPAD (echte 0x31) vs FEEDBACK (0xd4-Audio-Stream).
#
# Ziel: ein Config-Byte finden, bei dem FEEDBACK auf 0 faellt -> der Controller
# schreibt dann kein Audio mehr in den Input-Stream (kein d4-Mix).
#
# Tasten:  Pfeil hoch/runter = Feld waehlen,  links/rechts = Wert -/+ (1),
#          BILD hoch/runter = -/+ 0x10,  [r]=reset Defaults,  [SPACE]=start/stop
#          Audio,  [q]=quit.   /usr/bin/python3 (hat evdev/ctypes), uinput frei.

import os, sys, time, glob, zlib, struct, ctypes, threading, curses

# --- libopus: ein hoerbarer 440-Hz-Ton-Loop (damit man Audio-Regressionen hoert) -
import math
def opus_tone(freq=440, nframes=400):   # langer Loop -> seltener Opus-State-Knack
    for n in ("libopus.so.0", "libopus.so"):
        try: lib = ctypes.CDLL(n); break
        except OSError: lib = None
    if not lib: return [b"\x00" * 8]
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encoder_create.argtypes = [ctypes.c_int32,ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.c_int)]
    lib.opus_encode.restype = ctypes.c_int32
    lib.opus_encode.argtypes = [ctypes.c_void_p,ctypes.POINTER(ctypes.c_int16),ctypes.c_int,ctypes.POINTER(ctypes.c_ubyte),ctypes.c_int32]
    lib.opus_encoder_ctl.restype = ctypes.c_int
    lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p,ctypes.c_int,ctypes.c_int32]
    err = ctypes.c_int(0)
    enc = lib.opus_encoder_create(48000,2,2049,ctypes.byref(err))
    lib.opus_encoder_ctl(enc,4002,200*8*100)   # bitrate
    lib.opus_encoder_ctl(enc,4006,0)           # CBR -> 200 byte/frame
    lib.opus_encoder_ctl(enc,4010,0)           # complexity 0
    out=(ctypes.c_ubyte*400)()
    frames=[]; phase=0.0
    for _ in range(nframes):
        pcm=(ctypes.c_int16*(480*2))()
        for i in range(480):
            v=int(11000*math.sin(phase)); pcm[i*2]=v; pcm[i*2+1]=v
            phase += 2*math.pi*freq/48000
        n=lib.opus_encode(enc,pcm,480,out,400)
        frames.append(bytes(out[:n]))
    return frames

def crc(d): return zlib.crc32(bytes([0xA2])+d) & 0xFFFFFFFF

def feedback_params(start, strength):   # Adaptive-Trigger FEEDBACK (Widerstand)
    fv = (max(1, min(8, strength)) - 1) & 0x07     # Kraft 1..8 -> 3-Bit-Wert
    active = force = 0
    for z in range(start, 10):                     # Zonen ab 'start' aktiv
        active |= (1 << z); force |= (fv << (3 * z))
    return [active & 0xff, (active >> 8) & 0xff,
            force & 0xff, (force >> 8) & 0xff, (force >> 16) & 0xff, (force >> 24) & 0xff,
            0, 0, 0, 0]

def find_hidraw():
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            if "HID_ID=0005:0000054C:00000CE6" in open(d+"/device/uevent").read():
                return "/dev/"+os.path.basename(d)
        except OSError: pass
    return None

# --- editierbare Felder: (Name, default). Reihenfolge = Anzeige-Reihenfolge. ----
# 0x11-config = Sub-Packet [mask, route, b2, b3, b4, byte5, counter]
# 0x10-state  = Controller-State-Snapshot (63 byte), wichtige Offsets:
STATE_DEFAULT = bytearray([
 0xfd,0xe3,0,0, 0x7f,0x64, 0x00,0x09,0x00,0x10, 0,0,0,0, 0,0,0,0,0,0,0,0,
 0,0,0,0,0,0,0,0, 0,0,0,0,0,0,0,0x0a, 0x04,0,0,0,0x01, 0x00, 0,0,0xff] + [0]*16)

FIELDS = [
 # label,            kind,   default,  (kind 'cfg' -> 0x11[idx], 'snap' -> state[idx])
 ("cfg.mask  [0]",   "cfg", 0, 0xFE),
 ("cfg.route [1]",   "cfg", 1, 0x00),
 ("cfg.b2    [2]",   "cfg", 2, 0x00),
 ("cfg.b3    [3]",   "cfg", 3, 0x00),
 ("cfg.b4    [4]",   "cfg", 4, 0x00),
 ("cfg.byte5 [5]",   "cfg", 5, 0xFF),
 ("snap.valid_flag0","snap",0, 0xfd),
 ("snap.valid_flag1","snap",1, 0xe3),
 ("snap.mic_vol  [6]","snap",6,0x00),
 ("snap.audio_ctl[7]","snap",7,0x09),
 ("snap.mute_led [8]","snap",8,0x00),
 ("snap.pwr_save [9]","snap",9,0x10),
 # LED-Steuerung (brauchen valid_flag1-Bits 0x04 Lightbar / 0x10 Player -> Taste [l])
 ("snap.led_bright[42]","snap",42,0x01),
 ("snap.playerLED [43]","snap",43,0x00),   # Bitmaske der 5 weissen LEDs
 ("snap.lightbar_R[44]","snap",44,0x00),
 ("snap.lightbar_G[45]","snap",45,0x00),
 ("snap.lightbar_B[46]","snap",46,0xff),
]

class Probe:
    def __init__(self):
        self.cfg = [0xFE,0,0,0,0,0xFF,0]          # 0x11 sub-packet data
        self.state = bytearray(STATE_DEFAULT)
        self.tones = opus_tone()                    # hoerbarer 440-Hz-Loop
        self.ti = 0
        self.path = find_hidraw()
        self.running = True                         # Audio gleich AN
        self.gp = 0; self.d4 = 0                    # shared counters (last 1s)
        self.stop = threading.Event()
        self.seq = 0; self.counter = 0
        self.route = 0x13                           # 0x13 = interner Speaker, 0x16 = Klinke
        self.audio_ctl = 0x30                       # Setup-Report b[10]: Routing (0x30=Speaker)
        self.setup_dirty = False                    # -> Sender schickt build_setup() neu
        self.led_on = False                         # LED-Steuerung (eigener 0x31-Report)
        self.trig_on = False                        # Adaptive Trigger (Widerstand L2+R2)
        self.trig_str = 6                           # Trigger-Staerke 1..8

    def build_0x36(self):
        p = bytearray(398)
        p[0]=0x36; p[1]=(self.seq & 0x0F)<<4
        p[2]=0x11|0x80; p[3]=7
        for i in range(6): p[4+i]=self.cfg[i]
        p[10]=self.counter & 0xFF
        p[11]=0x10|0x80; p[12]=63; p[13:13+63]=self.state
        p[76]=0x12|0x80; p[77]=64
        p[142]=self.route|0x80; p[143]=200          # 0x13 Speaker / 0x16 Klinke
        op=self.tones[self.ti % len(self.tones)]; self.ti+=1
        p[144:144+len(op)]=op
        c=crc(bytes(p[:394])); struct.pack_into("<I",p,394,c)
        self.seq=(self.seq+1)&0x0F; self.counter=(self.counter+1)&0xFF
        return bytes(p)

    def build_setup(self):
        b=bytearray(78); b[0]=0x31; b[1]=0x10
        b[3]=0xA0; b[4]=0x80; b[8]=0x64; b[10]=self.audio_ctl; b[40]=0x02
        struct.pack_into("<I",b,74,crc(bytes(b[:74]))); return bytes(b)

    def build_out(self):
        # Ein 0x31-Output-Report: LEDs (valid_flag1) + Adaptive Trigger (valid_flag0).
        p=bytearray(78); p[0]=0x31; p[1]=(self.seq<<4)&0xF0; p[2]=0x10
        if self.led_on:
            p[4]=0x04|0x10                          # valid_flag1: Lightbar + Player
            p[45]=self.state[42]; p[46]=self.state[43]
            p[47]=self.state[44]; p[48]=self.state[45]; p[49]=self.state[46]
        p[3]=0x04|0x08                              # valid_flag0: rechter+linker Trigger
        if self.trig_on:                            # FEEDBACK-Widerstand ab Zone 2
            pr=feedback_params(2, self.trig_str)
            p[13]=0x21
            for i in range(10): p[14+i]=pr[i]       # rechter Trigger (R2)
            p[24]=0x21
            for i in range(10): p[25+i]=pr[i]       # linker Trigger (L2)
        else:
            p[13]=0x05; p[24]=0x05                  # OFF -> Trigger frei
        struct.pack_into("<I",p,74,crc(bytes(p[:74]))); return bytes(p)

    def reader(self):
        try: fd=os.open(self.path, os.O_RDONLY|os.O_NONBLOCK)
        except OSError: return
        g=d=0; t=time.monotonic()
        while not self.stop.is_set():
            try: r=os.read(fd,128)
            except BlockingIOError: time.sleep(0.001); r=None
            except OSError: break
            if r and r[0]==0x31:
                if len(r)>5 and r[3]==0xd4 and r[4]==0xff: d+=1
                else: g+=1
            now=time.monotonic()
            if now-t>=1.0:
                self.gp=g; self.d4=d; g=d=0; t=now
        os.close(fd)

    def sender(self):
        try: fd=os.open(self.path, os.O_WRONLY)
        except OSError: return
        os.write(fd, self.build_setup())
        nt=time.monotonic(); per=512/48000.0; led_t=0.0
        while not self.stop.is_set():
            if self.setup_dirty:                    # audio_control geaendert -> Setup neu
                self.setup_dirty=False
                try: os.write(fd, self.build_setup())
                except OSError: break
            if time.monotonic()-led_t > 0.20:        # LED+Trigger ~5x/s re-assert
                led_t=time.monotonic()
                try: os.write(fd, self.build_out())
                except OSError: break
            if self.running:
                try: os.write(fd, self.build_0x36())
                except OSError: break
            nt+=per; sl=nt-time.monotonic()
            if sl>0: time.sleep(sl)
            else: nt=time.monotonic()
        os.close(fd)

def main(scr):
    # WICHTIG: der Probe muss alleiniger 0x36-Sender sein, sonst verfaelscht der
    # parallele Audio-Service die d4-Messung. Daher hart stoppen.
    os.system("systemctl --user stop ds5-membrane-sink.service 2>/dev/null")
    os.system("pkill -f ds5_membrane_sink 2>/dev/null")
    time.sleep(0.5)
    pr=Probe()
    if not pr.path:
        scr.addstr(0,0,"DualSense (BT) nicht gefunden."); scr.getch(); return
    threading.Thread(target=pr.reader,daemon=True).start()
    threading.Thread(target=pr.sender,daemon=True).start()
    curses.curs_set(0); scr.nodelay(True); scr.timeout(300); scr.keypad(True)
    sel=0
    def getval(i):
        _,kind,idx,_=FIELDS[i]
        return pr.cfg[idx] if kind=="cfg" else pr.state[idx]
    def setval(i,v):
        _,kind,idx,_=FIELDS[i]; v&=0xFF
        if kind=="cfg": pr.cfg[idx]=v
        else: pr.state[idx]=v
    def put(y,x,s,attr=0):                           # randsicheres addstr
        maxy,maxx=scr.getmaxyx()
        if y>=maxy or x>=maxx: return
        try: scr.addstr(y,x,s[:maxx-x-1],attr)
        except curses.error: pass
    while True:
        scr.erase()
        put(0,0,"DualSense Audio-Init-Sondierer  (Root-Cause d4-Stream)",curses.A_BOLD)
        put(1,0,f"hidraw {pr.path}   Audio: {'AN ' if pr.running else 'aus'} [SPACE]")
        put(2,2,f"LEDs: [l] {'AN ' if pr.led_on else 'aus'}  Lightbar #"
                f"{pr.state[44]:02X}{pr.state[45]:02X}{pr.state[46]:02X}  "
                f"Player=0b{pr.state[43]&0x1f:05b}  bright={pr.state[42]}",
                curses.A_BOLD if pr.led_on else curses.A_DIM)
        col = curses.A_BOLD | (curses.A_REVERSE if pr.d4==0 and pr.running else 0)
        put(3,2,f"EMPFANG:  GAMEPAD={pr.gp:4d}/s    FEEDBACK(d4)={pr.d4:4d}/s",col)
        put(4,2,"Ziel: FEEDBACK(d4) = 0  -> kein Audio mehr im Input-Stream",curses.A_DIM)
        rname = "KLINKE (0x16)" if pr.route==0x16 else "SPEAKER (0x13)"
        put(5,2,f"AUSGABE: [j]Route={rname:13s} [a/d]audio=0x{pr.audio_ctl:02X}   "
                f"TRIGGER: [t]{'AN ' if pr.trig_on else 'aus'} [f/g]Kraft={pr.trig_str}",
            curses.A_BOLD)
        put(6,0,"Config-Bytes  [hoch/runter waehlen, links/rechts +-1, BILD +-0x10]:")
        for i,(label,_,_,dflt) in enumerate(FIELDS):
            v=getval(i); mark="-> " if i==sel else "   "
            chg="" if v==dflt else "  *"
            attr=curses.A_REVERSE if i==sel else 0
            warn="  (0xFF killt BT!)" if (label.startswith("cfg.mask") and v==0xFF) else ""
            put(7+i,2,f"{mark}{label:18s} = 0x{v:02X} ({v:3d}){chg}{warn}",attr)
        put(7+len(FIELDS)+1,2,"[j]Spk/Klinke [a/d]audio [l]LEDs [t]Trigger [f/g]Kraft [r]reset [SPACE]aud [q]quit",curses.A_DIM)
        scr.refresh()
        ch=scr.getch()
        if ch==-1: continue
        if ch in (ord('q'),27): break
        elif ch==curses.KEY_UP: sel=(sel-1)%len(FIELDS)
        elif ch==curses.KEY_DOWN: sel=(sel+1)%len(FIELDS)
        elif ch==curses.KEY_LEFT: setval(sel,getval(sel)-1)
        elif ch==curses.KEY_RIGHT: setval(sel,getval(sel)+1)
        elif ch==curses.KEY_NPAGE: setval(sel,getval(sel)-0x10)
        elif ch==curses.KEY_PPAGE: setval(sel,getval(sel)+0x10)
        elif ch==ord(' '): pr.running=not pr.running
        elif ch==ord('j'):                          # Speaker <-> Klinke umschalten
            pr.route = 0x16 if pr.route==0x13 else 0x13
        elif ch==ord('a'): pr.audio_ctl=(pr.audio_ctl-1)&0xFF; pr.setup_dirty=True
        elif ch==ord('d'): pr.audio_ctl=(pr.audio_ctl+1)&0xFF; pr.setup_dirty=True
        elif ch==ord('l'):                          # LED-Steuerung an/aus (0x31-Report)
            pr.led_on = not pr.led_on
            if pr.led_on and pr.state[44]==0 and pr.state[45]==0 and pr.state[46]==0:
                pr.state[46]=0xff                   # Default: blau, damit man sofort was sieht
        elif ch==ord('t'): pr.trig_on = not pr.trig_on        # Adaptive Trigger an/aus
        elif ch==ord('f'): pr.trig_str = max(1, pr.trig_str-1)
        elif ch==ord('g'): pr.trig_str = min(8, pr.trig_str+1)
        elif ch==ord('r'):
            pr.cfg=[0xFE,0,0,0,0,0xFF,0]; pr.state=bytearray(STATE_DEFAULT)
            pr.route=0x13; pr.audio_ctl=0x30; pr.setup_dirty=True
            pr.led_on=False; pr.trig_on=False; pr.trig_str=6
    pr.stop.set(); time.sleep(0.2)

if __name__=="__main__":
    curses.wrapper(main)
