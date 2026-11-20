"""X window focus helpers for WeChat Qt dialogs (no WM auto-focus on :99)."""
import ctypes as c, time
X = c.CDLL("libX11.so.6")
X.XOpenDisplay.restype=c.c_void_p; X.XOpenDisplay.argtypes=[c.c_char_p]
_dpy=None
def dpy():
    global _dpy
    if _dpy is None:
        _dpy=X.XOpenDisplay(None)
        if not _dpy: raise RuntimeError("x_display_unavailable")
    return _dpy
def root():
    X.XDefaultRootWindow.restype=c.c_ulong; X.XDefaultRootWindow.argtypes=[c.c_void_p]
    return X.XDefaultRootWindow(dpy())
def windows():
    d=dpy(); r=c.c_ulong(); p=c.c_ulong(); kids=c.POINTER(c.c_ulong)(); n=c.c_uint()
    X.XQueryTree.argtypes=[c.c_void_p,c.c_ulong,c.POINTER(c.c_ulong),c.POINTER(c.c_ulong),c.POINTER(c.POINTER(c.c_ulong)),c.POINTER(c.c_uint)]
    X.XQueryTree(d,root(),c.byref(r),c.byref(p),c.byref(kids),c.byref(n))
    X.XGetGeometry.argtypes=[c.c_void_p,c.c_ulong,c.POINTER(c.c_ulong),c.POINTER(c.c_int),c.POINTER(c.c_int),c.POINTER(c.c_uint),c.POINTER(c.c_uint),c.POINTER(c.c_uint),c.POINTER(c.c_uint)]
    X.XTranslateCoordinates.argtypes=[c.c_void_p,c.c_ulong,c.c_ulong,c.c_int,c.c_int,c.POINTER(c.c_int),c.POINTER(c.c_int),c.POINTER(c.c_ulong)]
    out={}
    for i in range(n.value):
        w=kids[i]
        rr=c.c_ulong(); xx=c.c_int(); yy=c.c_int(); ww=c.c_uint(); hh=c.c_uint(); bw=c.c_uint(); dp=c.c_uint()
        X.XGetGeometry(d,w,c.byref(rr),c.byref(xx),c.byref(yy),c.byref(ww),c.byref(hh),c.byref(bw),c.byref(dp))
        tx=c.c_int(); ty=c.c_int(); ch=c.c_ulong()
        X.XTranslateCoordinates(d,w,root(),0,0,c.byref(tx),c.byref(ty),c.byref(ch))
        out[w]=(tx.value,ty.value,ww.value,hh.value)
    return out
def focus(w):
    d=dpy()
    X.XRaiseWindow.argtypes=[c.c_void_p,c.c_ulong]; X.XRaiseWindow(d,w)
    X.XSetInputFocus.argtypes=[c.c_void_p,c.c_ulong,c.c_int,c.c_ulong]
    X.XSetInputFocus(d,w,1,0)
    X.XFlush.argtypes=[c.c_void_p]; X.XFlush(d)
def find_new(before, min_w=150, min_h=100):
    after=windows()
    return [(w,g) for w,g in after.items() if w not in before and g[2]>=min_w and g[3]>=min_h]
def main_window():
    X.XFetchName.argtypes=[c.c_void_p,c.c_ulong,c.POINTER(c.c_char_p)]
    for w,g in windows().items():
        nm=c.c_char_p()
        X.XFetchName(dpy(),w,c.byref(nm))
        if nm.value and nm.value.decode()=="Weixin" and g[2]>500:
            return w
    return None