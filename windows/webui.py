"""
Web UI for OP3T Webcam (pywebview + WebView2).

A native window renders the HTML/CSS/JS below; a small Python `Api` bridges the controls to the
existing Pipeline. Everything the UI needs is passed in via `ctx` so this file never imports the
main module back (no circular import, and it bundles cleanly under PyInstaller).

run(ctx) expects:
  host, port, Pipeline, RESOLUTIONS, FPS_OPTS, ROTATIONS, DECODERS (dict),
  load_config(), save_config(cfg), adb_forward(port), shutdown_phone(port), start_mjpeg(pipe)
Returns False if pywebview isn't importable so the caller can fall back to tkinter.

LAYOUT RULE (2026-08-28 overhaul): controls are grouped by HOW OFTEN YOU TOUCH THEM, not by what
kind of widget they are. Anything you might reach for mid-call is visible; anything you set once
lives in a collapsed Setup block. And the widget type MEANS something now:
    switch    persistent on/off state    Auto-frame, Flash
    segments  pick exactly one           Sensor zoom off/auto/on
    checkbox  independent toggle         Mirror, Flip, Denoise
    button    does something once        Refocus, Start/Stop
Before this every one of those was the same rounded pill and you had to click one to find out.
"""

import base64
import struct
import sys
import threading
import time

import numpy as np


def _bmp_data_url(w, h, rgb):
    """Pack a small RGB frame into a 24-bit BMP data: URL. FALLBACK ONLY — used when cv2 is missing
    so the MJPEG path cannot run. (BMP = headers + raw BGR, bottom-up, rows padded to 4 bytes.)"""
    arr = np.frombuffer(rgb, np.uint8).reshape(h, w, 3)[::-1, :, ::-1]   # bottom-up + RGB->BGR
    row = w * 3
    pad = (-row) % 4
    rows = arr.reshape(h, row)
    if pad:
        rows = np.hstack([rows, np.zeros((h, pad), np.uint8)])
    pixels = rows.tobytes()
    hdr = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    return "data:image/bmp;base64," + base64.b64encode(hdr + dib + pixels).decode()


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><style>
:root{
  --bg:#07080d; --surf:#10121a; --line:rgba(255,255,255,.09);
  --txt:#e9ebf3; --sub:#98a0b8; --dim:#69708a;
  --acc:#6ea8fe; --ok:#4ade80; --warn:#f5c15f; --dang:#f4587a;
}
*{box-sizing:border-box;margin:0;font-family:'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif;
  -webkit-user-select:none;cursor:default}
html,body{min-height:100%}
html{overflow-x:hidden;overflow-y:auto}
body{background:var(--bg);color:var(--txt);padding:14px;font-size:13px;-webkit-font-smoothing:antialiased}
.wrap{display:grid;grid-template-columns:minmax(330px,368px) 1fr;gap:14px;align-items:start}
.col{display:flex;flex-direction:column;gap:10px;min-width:0}
.col.right{position:sticky;top:0}
@media (max-width:900px){.wrap{grid-template-columns:1fr}.col.right{position:static}}

.head{display:flex;align-items:baseline;justify-content:space-between;padding:0 2px 2px}
h1{font-size:15px;font-weight:600;letter-spacing:-.01em}
.head .sub{font-size:11px;color:var(--sub)}
.statusbar{display:flex;align-items:center;gap:10px;background:var(--surf);border:1px solid var(--line);
  border-radius:10px;padding:9px 12px;font-size:12px}
.sd{width:7px;height:7px;border-radius:50%;background:var(--dim);flex:none;transition:.2s}
.statusbar.streaming .sd{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.statusbar.connecting .sd{background:var(--acc);box-shadow:0 0 8px var(--acc)}
.statusbar.error .sd{background:var(--dang);box-shadow:0 0 8px var(--dang)}
.statusbar b{font-weight:500;text-transform:capitalize}
.statusbar .nums{margin-left:auto;color:var(--sub);font-variant-numeric:tabular-nums;font-size:11.5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.card{background:var(--surf);border:1px solid var(--line);border-radius:12px;padding:12px 13px}
.ttl{font-size:10.5px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--sub);
  margin-bottom:11px}
.ttl .lo{text-transform:none;letter-spacing:0;font-weight:400}
.hr{height:1px;background:var(--line);margin:11px -13px}

.sw{display:flex;align-items:center;gap:10px;cursor:pointer;padding:2px 0}
.sw input{position:absolute;opacity:0;width:0;height:0}
.track{width:36px;height:20px;border-radius:10px;background:#2a2f42;border:1px solid var(--line);
  position:relative;flex:none;transition:background .16s}
.track::after{content:'';position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;
  background:#9aa3bd;transition:transform .16s,background .16s}
.sw input:checked + .track{background:rgba(110,168,254,.35);border-color:var(--acc)}
.sw input:checked + .track::after{transform:translateX(16px);background:var(--acc)}
.sw input:focus-visible + .track{outline:2px solid var(--acc);outline-offset:2px}
.sw .nm{font-size:13px}
.sw .st{margin-left:auto;font-size:11.5px;color:var(--sub);font-variant-numeric:tabular-nums}
.sw .st.live{color:var(--ok)}
.sw .st.warn{color:var(--warn)}

.cb{display:flex;align-items:center;gap:9px;cursor:pointer;padding:3px 0;font-size:13px}
.cb input{position:absolute;opacity:0;width:0;height:0}
.box{width:16px;height:16px;border-radius:4px;border:1.5px solid #3a4059;background:#181c28;flex:none;
  position:relative;transition:.14s}
.cb input:checked + .box{background:var(--acc);border-color:var(--acc)}
.cb input:checked + .box::after{content:'';position:absolute;left:4.5px;top:1.5px;width:4px;height:8px;
  border:solid #07080d;border-width:0 2px 2px 0;transform:rotate(42deg)}
.cb input:focus-visible + .box{outline:2px solid var(--acc);outline-offset:2px}

.seg{display:grid;grid-template-columns:repeat(3,1fr);background:#181c28;border:1px solid var(--line);
  border-radius:9px;padding:2px;gap:2px}
.seg label{text-align:center;padding:6px 4px;font-size:12.5px;border-radius:7px;cursor:pointer;
  color:var(--sub);transition:.14s}
.seg label:hover{color:var(--txt)}
.seg label.sel{background:rgba(110,168,254,.20);color:var(--txt);box-shadow:inset 0 0 0 1px rgba(110,168,254,.55)}

.sl{padding:7px 0 3px}
.sl .top{display:flex;align-items:baseline;gap:8px;margin-bottom:6px}
.sl .nm{font-size:12.5px}
.sl .hint{font-size:11px;color:var(--sub);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sl .val{margin-left:auto;font-size:12px;font-variant-numeric:tabular-nums;background:#1b2030;
  border:1px solid var(--line);border-radius:6px;padding:1px 7px;min-width:54px;text-align:center;flex:none}
input[type=range]{-webkit-appearance:none;width:100%;height:4px;border-radius:3px;background:#2a2f42;
  cursor:pointer;display:block}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;
  background:var(--txt);border:none;box-shadow:0 1px 4px rgba(0,0,0,.6);cursor:pointer}
input[type=range]:focus-visible{outline:2px solid var(--acc);outline-offset:4px}
.ticks{display:flex;justify-content:space-between;margin-top:4px}
.ticks span{font-size:10px;color:var(--dim);font-variant-numeric:tabular-nums}
.why{font-size:11px;color:var(--sub);margin-top:5px;display:flex;gap:6px;align-items:center}
.why .lock{color:var(--dim)}

.read{background:#141824;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-top:9px;
  font-size:11.5px;color:var(--sub);line-height:1.55}
.read b{color:var(--txt);font-weight:500;font-variant-numeric:tabular-nums}
.bar{height:3px;border-radius:2px;background:#2a2f42;margin-top:6px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc);width:0;transition:width .25s}

details{background:var(--surf);border:1px solid var(--line);border-radius:12px}
summary{list-style:none;cursor:pointer;padding:11px 13px;font-size:10.5px;font-weight:600;
  letter-spacing:.09em;text-transform:uppercase;color:var(--sub);display:flex;align-items:center;gap:7px}
summary::-webkit-details-marker{display:none}
summary .car{transition:transform .18s;font-size:9px;color:var(--dim)}
details[open] summary .car{transform:rotate(90deg)}
.setup{padding:0 13px 13px}
.frow{display:grid;grid-template-columns:104px 1fr;gap:9px 10px;align-items:center;margin-bottom:9px}
.frow>span{font-size:12.5px;color:var(--sub)}
select{appearance:none;width:100%;padding:7px 10px;border-radius:8px;color:var(--txt);font-size:12.5px;
  background:#181c28 url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M1 1l4 4 4-4' stroke='%2398a0b8' stroke-width='1.5' fill='none'/></svg>") no-repeat right 10px center;
  border:1px solid var(--line);cursor:pointer}
select:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
option{background:#12141d;color:var(--txt)}
.tag{display:inline-flex;font-size:10px;color:var(--warn);background:rgba(245,193,95,.10);
  border:1px solid rgba(245,193,95,.28);border-radius:5px;padding:1px 5px;margin-left:6px;vertical-align:middle}
.note{color:var(--dim);font-size:11px;line-height:1.5;margin-top:8px}

.btnrow{display:flex;gap:9px}
button{font:inherit;border:1px solid var(--line);background:#1b2030;color:var(--txt);border-radius:10px;
  padding:11px 14px;cursor:pointer;font-size:13px;transition:.14s}
button:hover{background:#222840}
button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.pri{flex:1;background:var(--acc);color:#06070c;border-color:var(--acc);font-weight:600}
.pri:hover{background:#87b7ff}
.pri.run{background:var(--dang);border-color:var(--dang);color:#fff}
.pri.run:hover{background:#ff6f8d}
.mini{padding:6px 11px;font-size:12px;border-radius:8px}
.foot{font-size:11px;color:var(--dim);text-align:center;padding:2px}

.pv{background:var(--surf);border:1px solid var(--line);border-radius:12px;padding:10px}
.pvhead{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px;gap:10px}
.pvhead .ttl{margin:0}
.pvhead .hint{font-size:11px;color:var(--dim);text-align:right}
#pvwrap{position:relative;line-height:0;border-radius:9px;overflow:hidden;background:#000;touch-action:none}
#pv{width:100%;height:auto;display:block;background:#000;-webkit-user-drag:none}
#cbox{position:absolute;border:1.5px solid var(--acc);border-radius:2px;
  box-shadow:0 0 0 3000px rgba(5,6,12,.55);cursor:grab;display:none;box-sizing:border-box}
#cbox.on{display:block}
#cbox:active{cursor:grabbing}
#cbox.lost{border-color:var(--warn)}
#cbox.lost #chandle{display:none}
#chandle{position:absolute;right:-6px;bottom:-6px;width:12px;height:12px;border-radius:3px;
  background:var(--acc);border:2px solid #07080d;cursor:nwse-resize}
.pvstat{display:flex;gap:14px;margin-top:9px;font-size:11px;color:var(--dim);font-variant-numeric:tabular-nums}
.banner{background:rgba(110,168,254,.10);border:1px solid rgba(110,168,254,.30);border-radius:8px;
  padding:7px 10px;font-size:11.5px;color:var(--txt);display:none;margin-top:9px}
.banner.on{display:block}
.pvoff{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;
  color:var(--sub);font-size:12.5px;min-height:240px;gap:6px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>

<div class="wrap">
 <div class="col left">
  <div class="head">
    <h1>OP3T Webcam</h1><span class="sub">OnePlus 3T &#8594; OBS Virtual Camera</span>
  </div>

  <div class="statusbar" id="st">
    <span class="sd"></span><b id="ststr">idle</b><span class="nums" id="meta"></span>
  </div>
  <div id="jserr" style="display:none;background:rgba(244,88,122,.12);border:1px solid var(--dang);
    border-radius:10px;padding:9px 12px;font-size:11.5px;color:var(--txt);
    white-space:pre-wrap;word-break:break-word"></div>

  <!-- ============ FRAMING: anything you might touch mid-call ============ -->
  <div class="card">
    <div class="ttl">Framing</div>

    <label class="sw">
      <input type="checkbox" id="af"><span class="track"></span>
      <span class="nm">Auto-frame</span><span class="st" id="afst">off</span>
    </label>

    <!-- ONE slider. Auto-frame decides what it drives and the label always says which; it used to
         silently redefine itself from "Zoom" to "Frame" with no warning at all. -->
    <div class="sl" id="zsl">
      <div class="top">
        <span class="nm" id="znm">Zoom</span>
        <span class="hint" id="zhint">digital crop from the 1080p frame</span>
        <span class="val" id="zval">1.0&times;</span>
      </div>
      <input type="range" id="z" min="1" max="4" step="0.1" value="1">
      <div class="ticks"><span id="tk0">1.0&times;</span><span id="tk1">4.0&times;</span></div>
      <div class="why" id="zwhy"><span class="lock">&#9679;</span> or drag the box on the preview</div>
    </div>

    <div class="hr"></div>

    <div class="ttl" style="margin-bottom:8px">Sensor zoom <span class="lo">&middot; phone-side</span></div>
    <div class="seg" id="seg">
      <label data-m="off" id="szoff">Off</label>
      <label data-m="auto" id="szauto">Auto</label>
      <label data-m="on" id="szon">On</label>
    </div>

    <div class="read"><span id="szline"></span>
      <div class="bar" id="szbar" style="display:none"><i id="szfill"></i></div>
    </div>

    <!-- Its own slider, and only in "On": in "Auto" the controller owns the ratio, so a slider there
         would be a control that fights back. "Auto" gets the readout above instead. -->
    <div class="sl" id="szsl" style="display:none">
      <div class="top">
        <span class="nm">Sensor zoom</span><span class="hint">the phone crops its own sensor</span>
        <span class="val" id="szval">1.0&times;</span>
      </div>
      <input type="range" id="szx" min="1" max="2.4" step="0.1" value="1">
      <div class="ticks"><span>1.0&times;</span><span>2.4&times; lossless limit</span></div>
    </div>
  </div>

  <!-- ============ IMAGE ============ -->
  <div class="card">
    <div class="ttl">Image</div>
    <div class="sl">
      <div class="top"><span class="nm">Exposure</span><span class="val" id="evval">0</span></div>
      <input type="range" id="ev" min="-6" max="6" step="1" value="0">
      <div class="ticks"><span>&minus;6</span><span>0</span><span>+6</span></div>
    </div>
    <div class="sl">
      <div class="top"><span class="nm">Focus</span><span class="hint" id="fhint">auto</span>
        <span class="val" id="fval">Auto</span></div>
      <input type="range" id="focus" min="0" max="1" step="0.01" value="0">
      <!-- FOCUSDIST v sets v * minimum-focus diopters: just right of auto is far, the right end is
           the closest the lens goes. The old "auto / near / far" ticks had it backwards. -->
      <div class="ticks"><span>auto &middot; far</span><span>near</span></div>
      <div class="why"><button class="mini" id="rfbtn">Refocus once</button></div>
    </div>
    <div class="hr"></div>
    <label class="sw">
      <input type="checkbox" id="fl"><span class="track"></span>
      <span class="nm">Flash</span><span class="st" id="flst">off</span>
    </label>
  </div>

  <!-- ============ SETUP: set once ============ -->
  <details id="setup">
    <summary><span class="car">&#9654;</span>Setup &middot; set once</summary>
    <div class="setup">
      <div class="frow">
        <span>Frame rate <span class="tag">restarts</span></span><select id="fps"></select>
        <span>Decoder <span class="tag">restarts</span></span><select id="dec"></select>
        <span>Orientation</span><select id="rot"></select>
        <span>Quality</span><select id="bitrate"></select>
      </div>
      <div class="hr" style="margin:11px 0"></div>
      <label class="cb"><input type="checkbox" id="fh"><span class="box"></span>Mirror horizontally</label>
      <label class="cb"><input type="checkbox" id="fv"><span class="box"></span>Flip vertically</label>
      <label class="cb"><input type="checkbox" id="dn"><span class="box"></span>Noise reduction
        <span style="color:var(--sub)">&nbsp;(on the phone)</span></label>
      <div class="note" id="capnote"></div>
      <div class="hr" style="margin:11px 0"></div>
      <label class="cb"><input type="checkbox" id="ac"><span class="box"></span>Start when an app turns
        the camera on</label>
      <label class="cb"><input type="checkbox" id="sw"><span class="box"></span>Start with Windows, in
        the tray</label>
    </div>
  </details>

  <div class="btnrow">
    <button class="pri" id="go">Start</button>
    <button id="pvbtn">Preview</button>
  </div>
  <div class="foot">Pick &ldquo;OBS Virtual Camera&rdquo; in your meeting app.</div>
 </div>

 <div class="col right">
  <div class="pv" id="pvcard" style="display:none">
    <div class="pvhead"><div class="ttl">Preview</div>
      <span class="hint">drag box to pan &middot; corner or scroll to zoom &middot; dbl-click reset</span></div>
    <div id="pvwrap"><img id="pv" draggable="false"><div id="cbox"><div id="chandle"></div></div></div>
    <div class="pvstat"><span id="pvres">640&times;360 &middot; 30 fps</span><span id="pvmode">MJPEG</span></div>
    <div class="banner" id="ban">Auto-frame turned off &mdash; you took the wheel.</div>
  </div>
  <div class="card pvoff" id="pvoff">Preview is off.<br>
    <span style="color:var(--dim);font-size:11px">Turn it on to frame the shot.
      Off costs a little less work on the frame thread.</span></div>
 </div>
</div>

<script>
const $=i=>document.getElementById(i);
let api=null, running=false, afOn=false, szMode='off', PV=null, pvOn=false, booted=false;
let acT=0, swT=0;   /* last local click on the two startup switches: poll() must not undo it mid-call */
let RES='1920x1080';

/* ---- bridge hygiene -------------------------------------------------------------------------
   Every pywebview call spawns a Python thread and holds the GIL, competing with the thread that
   owes the vcam a frame every 16.67 ms. A range input fires oninput CONTINUOUSLY while dragging,
   and the old code called both a setter AND api.save() (which writes JSON to disk) on every one of
   those events. Live values are throttled now; saving waits until you stop moving.              */
function throttle(fn,ms){ let last=0,t=null;
  return (...a)=>{ const now=performance.now();
    if(now-last>=ms){ last=now; fn(...a); }
    else { clearTimeout(t); t=setTimeout(()=>{ last=performance.now(); fn(...a); }, ms-(now-last)); } }; }
let saveT=null;
function saveSoon(){ clearTimeout(saveT); saveT=setTimeout(()=>{ if(api) api.save(snap()); },600); }

/* ---- the morphing slider --------------------------------------------------------------------
   Auto-frame ON  -> framing tightness (the controller owns the crop; this sets how big you sit)
   Auto-frame OFF -> plain digital zoom (the PC crop you drive yourself)
   Sensor zoom is deliberately NOT this control — it has its own slider below.                   */
const MODES={
  tight:{nm:'Framing tightness',hint:'how much of the frame your face fills',t0:'wide',t1:'tight'},
  zoom :{nm:'Zoom',hint:'digital crop from the 1080p frame',t0:'1.0×',t1:'4.0×'}
};
let store={tight:1.5,zoom:1.0};
function drawSlider(){
  const key=afOn?'tight':'zoom', m=MODES[key];
  $('znm').textContent=m.nm; $('zhint').textContent=m.hint;
  $('tk0').textContent=m.t0; $('tk1').textContent=m.t1;
  $('z').value=store[key];
  $('zval').textContent=store[key].toFixed(1)+'×';
  $('zwhy').innerHTML = afOn ? '<span class="lock">applied crop</span> <b id="zapp">&mdash;</b>'
                             : '<span class="lock">&#9679;</span> or drag the box on the preview';
}
function setAf(on,label){
  afOn=on; $('af').checked=on;
  $('afst').textContent = label || (on?'tracking':'off');
  $('afst').classList.toggle('live',on && !label);
  $('afst').classList.toggle('warn',!!label);
}

/* A throw anywhere in boot() leaves EVERY control unwired — the window looks alive and does
   nothing, with the exception swallowed by pywebview's rejected promise. Report it, and say so on
   screen, rather than failing silently. */
window.onerror=(m,src,ln,col)=>{ window.__lastErr=m+' @'+ln+':'+col; report('window.onerror: '+window.__lastErr); };
window.addEventListener('unhandledrejection',e=>report('unhandled rejection: '+(e.reason&&e.reason.message||e.reason)));
function report(msg){
  try{ if(api&&api.report_js_error) api.report_js_error(String(msg)); }catch(e){}
  const b=document.getElementById('jserr');
  if(b){ b.style.display='block'; b.textContent='UI error: '+msg; }
}

async function boot(){
  if(booted) return; booted=true;
  api=window.pywebview.api;
  try{ await bootInner(); report.ok=1; api.report_js_error('boot ok'); }
  catch(e){ report('boot failed: '+(e&&e.stack||e)); }
}

async function bootInner(){
  const o=await api.options(), c=o.config;
  RES=(o.resolutions&&o.resolutions[0])||RES;
  PV=o.preview_url||null;
  fill($('fps'),o.fps,c.fps); fill($('rot'),o.rotations,c.rotation);
  fill($('dec'),o.decoders,c.decoder); fill($('bitrate'),o.bitrates,c.bitrate);
  capNote();

  $('fh').checked=!!c.flip_h; $('fv').checked=!!c.flip_v; $('dn').checked=!!c.denoise;
  $('ac').checked=c.autocam!==false; $('sw').checked=!!o.autostart;
  store.zoom=Number(c.zoom)||1; store.tight=Number(c.tightness)||1.5;
  setAf(!!c.autoframe);
  view.z=store.zoom; view.cx=(c.pan&&c.pan[0])||0.5; view.cy=(c.pan&&c.pan[1])||0.5;
  drawSlider();
  $('ev').value=c.ev; $('evval').textContent=(c.ev>0?'+':'')+c.ev;
  $('focus').value=c.focus||0; showFocus(c.focus||0);
  szMode=c.sensor_zoom||'off'; $('szx').value=c.sensor_zoom_x||1; drawSensor();

  /* live controls: send throttled, save debounced */
  const sendZ=throttle(v=>{ if(afOn) api.set_tightness(v); else { view.z=v; applyView(); } },50);
  $('z').oninput=e=>{ const key=afOn?'tight':'zoom', v=Number(e.target.value);
    store[key]=v; $('zval').textContent=v.toFixed(1)+'×'; sendZ(v); saveSoon(); };
  const sendEv=throttle(v=>api.set_ev(v),50);
  $('ev').oninput=e=>{ const v=Number(e.target.value);
    $('evval').textContent=(v>0?'+':'')+v; sendEv(v); saveSoon(); };
  const sendF=throttle(v=>api.set_focus(v),50);
  $('focus').oninput=e=>{ const v=Number(e.target.value); showFocus(v); sendF(v); saveSoon(); };
  const sendSz=throttle(v=>api.set_sensor_zoom(szMode,v),80);
  $('szx').oninput=e=>{ const v=Number(e.target.value);
    $('szval').textContent=v.toFixed(1)+'×'; sendSz(v); saveSoon(); };

  $('af').onchange=e=>{ setAf(e.target.checked); $('ban').classList.remove('on');
    api.set_autoframe(afOn,view.z,view.cx,view.cy);
    if(afOn) api.set_tightness(store.tight);
    drawSlider(); saveSoon(); };
  $('fl').onchange=e=>{ $('flst').textContent=e.target.checked?'on':'off';
    api.set_flash(e.target.checked); };
  $('rfbtn').onclick=()=>{ api.refocus(); $('focus').value=0; showFocus(0);
    api.set_focus(0); saveSoon(); };

  document.querySelectorAll('#seg label').forEach(l=>l.onclick=()=>setSensor(l.dataset.m));

  $('fps').onchange=()=>{ capNote(); restartChange(); };
  $('dec').onchange=restartChange;
  $('rot').onchange=()=>{ setXform(); saveSoon(); };
  $('bitrate').onchange=()=>{ api.set_bitrate(Number($('bitrate').value)); saveSoon(); };
  $('fh').onchange=()=>{ setXform(); saveSoon(); };
  $('fv').onchange=()=>{ setXform(); saveSoon(); };
  $('dn').onchange=()=>{ api.set_denoise($('dn').checked); saveSoon(); };
  $('ac').onchange=()=>{ acT=Date.now(); api.set_autocam($('ac').checked); saveSoon(); };
  $('sw').onchange=async()=>{ swT=Date.now(); const on=await api.set_autostart($('sw').checked);
    if(typeof on==='boolean') $('sw').checked=on; };   /* the registry has the last word */
  $('go').onclick=toggle; $('pvbtn').onclick=togglePreview;

  /* crop box: scroll = zoom, drag box = pan, drag corner = zoom, dbl-click = reset */
  $('pvwrap').addEventListener('wheel',pvWheel,{passive:false});
  $('cbox').addEventListener('mousedown',e=>{ e.preventDefault(); e.stopPropagation();
    bdrag={x:e.clientX,y:e.clientY}; });
  $('chandle').addEventListener('mousedown',e=>{ e.preventDefault(); e.stopPropagation(); hdrag=true; });
  $('pvwrap').addEventListener('dblclick',()=>{ view={z:1,cx:0.5,cy:0.5}; store.zoom=1;
    applyView(); drawSlider(); saveSoon(); });
  window.addEventListener('mousemove',onBoxMove);
  window.addEventListener('mouseup',()=>{ if(bdrag||hdrag){ bdrag=null; hdrag=false; saveSoon(); } });
  applyView();
  /* ONE poll for the whole UI. It carries no image any more — the preview has its own MJPEG socket
     — so 10 Hz costs a couple of hundred bytes a tick instead of a 324 KB base64 string. */
  setInterval(poll,100);
}

function fill(sel,arr,val){ sel.innerHTML='';
  arr.forEach(v=>{ const obj=(v!==null&&typeof v==='object');
    const o=document.createElement('option');
    o.value=obj?v.value:v; o.textContent=obj?v.label:v;
    if(String(o.value)===String(val)) o.selected=true;
    sel.appendChild(o); }); }
function capNote(){ $('capnote').textContent = $('fps').value==='30'
  ? 'Output is always 1080p. At 30 fps the phone captures 4K and downscales on its own GPU — best detail, no extra PC cost.'
  : 'Output is always 1080p. At 60 fps the phone captures 1080p — smoothest motion.'; }
function showFocus(v){ $('fval').textContent = v<=0?'Auto':Math.round(v*100)+'%';
  $('fhint').textContent = v<=0?'auto':'locked'; }
function snap(){ return {resolution:RES,fps:$('fps').value,rotation:$('rot').value,decoder:$('dec').value,
  flip_h:$('fh').checked,flip_v:$('fv').checked,denoise:$('dn').checked,
  zoom:store.zoom,tightness:store.tight,ev:Number($('ev').value),bitrate:Number($('bitrate').value),
  focus:Number($('focus').value),autoframe:afOn,sensor_zoom:szMode,
  sensor_zoom_x:Number($('szx').value),autocam:$('ac').checked}; }
function setXform(){ api.set_transform($('rot').value,$('fh').checked,$('fv').checked); }
function restartChange(){ if(api) api.save(snap()); if(running) api.restart(snap()); }

/* ---- sensor zoom ----------------------------------------------------------------------------- */
function drawSensor(){
  ['off','auto','on'].forEach(m=>$('sz'+m).classList.toggle('sel',szMode===m));
  $('szsl').style.display = szMode==='on' ? '' : 'none';
  $('szval').textContent = Number($('szx').value).toFixed(1)+'×';
  if(szMode==='off'){ $('szbar').style.display='none';
    $('szline').innerHTML='Off — the phone sends its full field of view; all zoom happens on the PC.'; }
}
function setSensor(m){ szMode=m; drawSensor(); api.set_sensor_zoom(m,Number($('szx').value)); saveSoon(); }

/* ---- crop box -------------------------------------------------------------------------------- */
let view={z:1,cx:0.5,cy:0.5}, bdrag=null, hdrag=false;
function clampView(){ const m=0.5/view.z;
  view.cx=Math.min(Math.max(view.cx,m),1-m); view.cy=Math.min(Math.max(view.cy,m),1-m); }
function drawBox(){ const b=$('cbox');
  if(view.z<=1.02){ b.classList.remove('on'); return; }
  b.classList.add('on');
  const s=100/view.z; b.style.width=s+'%'; b.style.height=s+'%';
  b.style.left=((view.cx-0.5/view.z)*100)+'%'; b.style.top=((view.cy-0.5/view.z)*100)+'%'; }
function applyView(){ view.z=Math.min(4,Math.max(1,Math.round(view.z*10)/10)); clampView(); drawBox();
  store.zoom=view.z;
  if(!afOn){ $('z').value=view.z; $('zval').textContent=view.z.toFixed(1)+'×'; }
  if(!api) return;
  /* Touching a manual control hands control back — but SAY SO instead of silently unchecking. */
  if(afOn){ setAf(false); $('ban').classList.add('on'); drawSlider(); }
  api.set_view(view.z,view.cx,view.cy); }
function pvWheel(e){ e.preventDefault(); view.z+=(e.deltaY<0?0.1:-0.1); applyView(); saveSoon(); }
function onBoxMove(e){ const r=$('pv').getBoundingClientRect(); if(!r.width) return;
  if(bdrag){ view.cx+=(e.clientX-bdrag.x)/r.width; view.cy+=(e.clientY-bdrag.y)/r.height;
    bdrag={x:e.clientX,y:e.clientY}; applyView(); }
  else if(hdrag){ const half=Math.max(0.06,Math.abs((e.clientX-r.left)/r.width-view.cx));
    view.z=0.5/half; applyView(); } }

/* ---- start / preview -------------------------------------------------------------------------- */
async function toggle(){
  if(running){ await api.stop(); running=false;
    $('go').textContent='Start'; $('go').classList.remove('run'); }
  else { await api.start(snap()); running=true;
    $('go').textContent='Stop'; $('go').classList.add('run'); } }
function togglePreview(){ pvOn=!pvOn; api.preview(pvOn);
  $('pvcard').style.display=pvOn?'block':'none'; $('pvoff').style.display=pvOn?'none':'flex';
  $('pvbtn').textContent=pvOn?'Preview: On':'Preview';
  if(pvOn){ if(PV){ $('pv').src=PV+'?t='+Date.now(); $('pvmode').textContent='MJPEG'; }
            else { $('pvmode').textContent='bridge fallback'; $('pvres').textContent='384×216 · 8 fps';
                   if(!bridgeTimer) bridgeTimer=setInterval(pollPreview,120); }
            drawBox(); }
  else { $('pv').removeAttribute('src');
         if(bridgeTimer){ clearInterval(bridgeTimer); bridgeTimer=null; } } }
/* If the MJPEG socket cannot be reached (WebView2 policy, port refused), drop to the old bridge poll
   rather than leaving a broken image sitting there. */
let bridgeTimer=null;
$('pv').onerror=()=>{ if(!pvOn||!PV) return;
  PV=null; api.use_bridge_preview();   /* the send thread has to go back to building PPM/RGB */
  $('pvmode').textContent='bridge fallback'; $('pvres').textContent='384×216 · 8 fps';
  if(!bridgeTimer) bridgeTimer=setInterval(pollPreview,120); };
async function pollPreview(){ if(!pvOn||!api||PV) return;
  try{ const d=await api.get_preview(); if(d&&d.img) $('pv').src=d.img; }catch(e){} }

async function poll(){
  if(!api) return;
  try{
    const s=await api.status();
    $('st').className='statusbar '+s.state; $('ststr').textContent=s.state;
    let meta = s.state==='streaming'
      ? s.fps.toFixed(1)+' fps · '+s.frames+' frames · '+s.dropped+' dropped' : s.msg;
    if(s.state==='idle' && !meta && s.autocam)
      meta = s.cam_users ? 'an app turned the camera on — starting' : 'starts when an app turns the camera on';
    $('meta').textContent = meta;
    /* The stream now starts and stops without this button (auto-start, the tray menu), so the
       button follows the truth instead of its own memory of the last click. */
    if(typeof s.running==='boolean' && s.running!==running){ running=s.running;
      $('go').textContent=running?'Stop':'Start'; $('go').classList.toggle('run',running); }
    if(typeof s.autocam==='boolean' && Date.now()-acT>1500) $('ac').checked=s.autocam;
    if(typeof s.autostart==='boolean' && Date.now()-swT>1500) $('sw').checked=s.autostart;

    /* the crop box must draw the APPLIED view or it lies about where the output points */
    if(s.auto){
      if(!afOn){ setAf(true); drawSlider(); }
      setAf(true, s.lost?'no face':null);
      view.z=s.z; view.cx=s.cx; view.cy=s.cy; clampView(); drawBox();
      $('cbox').classList.toggle('lost',!!s.lost);
      const za=$('zapp'); if(za) za.textContent=s.z.toFixed(2)+'×';
    } else { $('cbox').classList.remove('lost'); }

    /* Sensor readout: the APPLIED ratio, in every mode that touches the phone, always visible.
       Applied is the truth — a crop-region change lands up to 8 frames after it is asked for. */
    if(szMode==='auto'){
      $('szbar').style.display='';
      $('szfill').style.width=Math.min(100,Math.max(0,(s.sz_obs-1)/1.4*100))+'%';
      /* Auto only ever zooms IN while Auto-frame drives it; on the manual slider it just hands back
         (SensorZoom.update, `tracking`). The readout has to say so or it promises a takeover. */
      let t = s.sz_obs>1.03
        ? (s.auto
            ? 'Auto · phone at <b>'+s.sz_obs.toFixed(2)+'×</b> — the PC crop was pinned and you were still small in frame.'
            : 'Auto · phone held at <b>'+s.sz_obs.toFixed(2)+'×</b>. Auto-frame is off, so it only hands back — zoom out to release it.')
        : (s.auto
            ? 'Auto · phone at <b>1.00×</b> — PC crop <b>'+s.z.toFixed(2)+'×</b> of <b>'
              +s.zmax.toFixed(2)+'×</b> max. The phone takes over only when the PC crop runs out.'
            : 'Auto · phone at <b>1.00×</b>. It zooms in only while Auto-frame is tracking.');
      if(Math.abs(s.sz_req-s.sz_obs)>0.05)
        t+=' <span style="color:var(--warn)">→ '+s.sz_req.toFixed(2)+'× landing</span>';
      $('szline').innerHTML=t;
    } else if(szMode==='on'){
      $('szbar').style.display='none';
      $('szline').innerHTML='On · asked for <b>'+s.sz_req.toFixed(2)+'×</b>, applied <b>'
        +s.sz_obs.toFixed(2)+'×</b>. Magnification moved to the sensor is pan range the PC gives up.';
    }
  }catch(e){}
}

/* pywebview injects its api then fires 'pywebviewready'.
   MEASURED 2026-08-28: on this machine the bridge can take NINE SECONDS to appear. The old
   bootstrap gave up after 1 s (20 x 50 ms) and relied on the ready event alone after that — and if
   that event fires before `.api` is populated, tryBoot() returns false and nothing ever retries.
   The window then renders perfectly with not one handler attached, which is precisely the "buttons
   do nothing" failure. So: keep retrying for 30 s, and SAY SO if the bridge never turns up. */
(function(){
  /* Ready means POPULATED, not present: pywebview creates window.pywebview.api as an empty object
     first (api.js), crawls the Python side, and only then fills it in one go (finish.js). Booting on
     the empty object threw "api.options is not a function" and, booted already, never retried —
     MEASURED 2026-10-05, whenever startup was busy (auto-start waking the phone). */
  function tryBoot(){ if(window.pywebview && window.pywebview.api
                         && typeof window.pywebview.api.options==='function'){ boot(); return true; }
                      return false; }
  window.addEventListener('pywebviewready', tryBoot);
  if(tryBoot()) return;
  let tries=0;
  const tid=setInterval(()=>{
    if(tryBoot()){ clearInterval(tid); return; }
    if(++tries>=300){ clearInterval(tid);
      const b=document.getElementById('jserr');
      if(b){ b.style.display='block';
             b.textContent='UI error: the Python bridge never appeared after 30 s — controls are dead.'; } }
  },100);
})();
</script>
</body></html>"""


class Api:
    def __init__(self, ctx):
        self.ctx = ctx
        self.pipe = ctx["Pipeline"]()
        self.pipe.preview_fmt = "jpeg"       # the web UI pulls MJPEG; see Pipeline.preview_fmt
        self.host, self.port = ctx["host"], ctx["port"]
        self.cfg = ctx["load_config"]()
        self.preview_url = None              # filled in by run() once the MJPEG server is up
        self.pipe.zoom = float(self.cfg["zoom"])
        self.pipe.pan = list(self.cfg["pan"])
        self.pipe.denoise = bool(self.cfg["denoise"])
        self.pipe.ev = int(self.cfg["ev"])
        self.pipe.bitrate = int(self.cfg["bitrate"])
        self.set_tightness(self.cfg.get("tightness", 1.5))
        if self.cfg.get("autoframe"):
            self.pipe.af.arm(float(self.cfg["zoom"]), *self.cfg["pan"])
        self.pipe.set_sensor_zoom(self.cfg.get("sensor_zoom", "off"),
                                  self.cfg.get("sensor_zoom_x", 1.0))
        self.running = False
        # Auto-start (op3t_webcam.VcamHost / AutoCam). Built here, STARTED by run(): an Api must stay
        # inert, because test_webui constructs one with no camera, no window and a minimal ctx.
        self.autocam = ctx["AutoCam"]() if "AutoCam" in ctx else None
        if self.autocam is not None:
            self.autocam.enabled = bool(self.cfg.get("autocam", True))
        self.vcam = None
        self.cam_users = 0
        self.tray = None
        self._win = None
        self._quitting = False
        self._quit = threading.Event()
        try:
            self._autostart = bool(ctx.get("get_autostart", lambda: "")())
        except Exception:
            self._autostart = False

    def options(self):
        c = self.ctx
        return {"resolutions": c["RESOLUTIONS"], "fps": c["FPS_OPTS"], "rotations": c["ROTATIONS"],
                "decoders": list(c["DECODERS"].keys()),
                # Quality used to be a bare 4..20 slider with no unit on it. Discrete choices, named.
                "bitrates": [{"value": v, "label": lbl} for v, lbl in
                             ((8, "8 Mbps — lighter"), (12, "12 Mbps — default"),
                              (16, "16 Mbps"), (20, "20 Mbps — sharpest"))],
                "preview_url": self.preview_url, "config": self.cfg, "autostart": self._autostart}

    def _args(self, cfg):
        w, h = map(int, cfg["resolution"].split("x"))
        return (self.host, self.port, w, h, int(cfg["fps"]), int(cfg["rotation"]), cfg["decoder"],
                bool(cfg["flip_h"]), bool(cfg["flip_v"]), bool(cfg["denoise"]))

    def save(self, cfg):
        cfg = dict(cfg)
        # Only capture the LIVE pan when the user owns it. While auto-framing is driving, this would
        # persist wherever their face happened to be and the app would restart framed on that spot.
        if not self.pipe.af.enabled:
            cfg["pan"] = list(self.pipe.pan)
        else:
            cfg["pan"] = list(self.cfg.get("pan", [0.5, 0.5]))
        self.ctx["save_config"](cfg)
        self.cfg = cfg

    def set_autoframe(self, on, z=1.0, cx=0.5, cy=0.5):
        """Arming from the CURRENT framing means engaging never snaps — it eases in from wherever
        the manual crop box was left."""
        if on:
            self.pipe.af.arm(float(z), float(cx), float(cy))
        else:
            self.pipe.af.disengage(keep_current=True)
            z2, cx2, cy2 = self.pipe.auto_view
            self.pipe.zoom, self.pipe.pan = z2, [cx2, cy2]   # hand the framing back, no jump

    def start(self, cfg):
        """Start (button or tray): a stream the user owns, so auto-start never stops it."""
        if self.autocam is not None:
            self.autocam.user_start()
        self._begin(cfg)

    def _begin(self, cfg):
        self.save(cfg)
        self.pipe.zoom = float(cfg["zoom"])
        self.pipe.ev = int(cfg["ev"])
        self.pipe.bitrate = int(cfg["bitrate"])
        self.running = True
        self.pipe.start(*self._args(cfg))

    def restart(self, cfg):
        self.save(cfg)
        if self.running:
            self.pipe.start(*self._args(cfg))

    def stop(self):
        """Stop (button or tray). Respected even while an app still has the camera on: auto-start
        then waits for that app to let go before it will start the stream again."""
        if self.autocam is not None:
            self.autocam.user_stop()
        self._end()

    def _end(self):
        self.running = False
        self.pipe.stop()

    def set_autocam(self, on):
        """Start and stop with the apps that use the camera. Off = only Start starts the stream, and
        the virtual camera exists only while streaming, exactly as before auto-start."""
        on = bool(on)
        self.save(dict(self.cfg, autocam=on))
        if self.autocam is not None:
            self.autocam.enabled = on
        if self.vcam is not None:
            if on:
                self.vcam.idle(*self._spec())
            else:
                self.vcam.no_idle()
        return on

    def set_autostart(self, on):
        """Start with Windows, straight into the tray: a per-user Run entry, so no admin prompt."""
        try:
            self._autostart = bool(self.ctx["set_autostart"](bool(on)))
        except Exception as e:
            self.ctx["log_js_error"](f"set_autostart({on}): {e!r}")
        return self._autostart

    # ---- background: camera host, auto-start, tray, window (Python-side only, never JS) ----------

    def _spec(self):
        w, h = map(int, self.cfg["resolution"].split("x"))
        return w, h, int(self.cfg["fps"])

    def _start_background(self, win):
        """The parts that need a real camera or window. run() calls this; tests never do."""
        self._win = win
        if "VcamHost" not in self.ctx:
            return
        self.vcam = self.ctx["VcamHost"]()
        self.pipe.vcam = self.vcam
        if self.cfg.get("autocam", True):
            self.vcam.idle(*self._spec())
        if self.autocam is not None:
            threading.Thread(target=self._autocam_loop, daemon=True).start()

    def _autocam_loop(self):
        """Four ticks a second; each is one NtQueryObject on a handle we already hold."""
        while not self._quit.wait(0.25):
            try:
                users = self.vcam.consumers()
                self.cam_users = users
                t = self.pipe.thread
                act = self.autocam.step(users, time.monotonic(), bool(t and t.is_alive()))
                if act == "start":
                    self._auto_start()
                elif act == "stop":
                    self._auto_stop()
            except Exception as e:
                self.ctx["log_js_error"](f"autocam: {e!r}")

    def _auto_start(self):
        self.ctx["log_js_error"]("autocam: an app turned the camera on -> start")
        try:
            self.ctx["adb_forward"](self.port)     # the last auto-stop parked the phone: wake it
        except Exception:
            pass
        self._begin(dict(self.cfg))
        if self.tray is not None:
            self.tray.notify("Camera on", "An app turned the camera on. Streaming from your phone.")

    def _auto_stop(self):
        self.ctx["log_js_error"]("autocam: every app let go of the camera -> stop")
        self._end()
        try:
            self.ctx.get("park_phone", lambda p: None)(self.port)   # sleep it; keep adb up
        except Exception:
            pass

    def _show_window(self):
        """Always opens maximised; from the tray, a second launch, or a failed tray at login."""
        if self._win is None:
            return
        try:
            self._win.show()
            self._win.maximize()
        except Exception as e:
            self.ctx["log_js_error"](f"show window: {e!r}")

    def _quit_app(self):
        """Tray > Quit: the only way out now that closing the window hides it."""
        self._quitting = True
        if self.tray is not None:
            self.tray.dispose()
        if self._win is not None:
            self._win.destroy()

    def _shutdown(self):
        self._quit.set()
        self._end()
        if self.vcam is not None:
            self.vcam.close()

    def set_tightness(self, slider):
        import op3t_webcam as m
        self.pipe.af.tightness = min(m.AF_TIGHT_MAX,
                                     max(m.AF_TIGHT_MIN, float(slider) * m.AF_FRAC_PER_X))

    def set_sensor_zoom(self, mode, manual=1.0):
        self.pipe.set_sensor_zoom(str(mode), manual)

    def set_transform(self, rot, h, v):
        self.pipe.set_transform(int(rot), bool(h), bool(v))

    def set_denoise(self, on):
        self.pipe.set_denoise(bool(on))

    def set_flash(self, on):
        self.pipe.set_torch(bool(on))

    def set_ev(self, steps):
        self.pipe.set_ev(int(steps))

    def set_bitrate(self, mbps):
        self.pipe.set_bitrate(int(mbps))

    def set_focus(self, f):
        self.pipe.set_focus(float(f))

    def refocus(self):
        self.pipe.send_control("FOCUS")

    def report_js_error(self, msg):
        """The panel's only way to be heard. See log_js_error: a JS throw during boot leaves every
        control unwired and pywebview swallows it, so the window looks fine and does nothing."""
        try:
            self.ctx["log_js_error"](str(msg)[:2000])
        except Exception:
            pass

    def status(self):
        """ONE poll for the whole UI, 10 Hz. It used to be two (status at 500 ms, preview at 250 ms)
        and the preview one carried a ~324 KB base64 string; the image has its own socket now, so
        this is a couple of hundred bytes and can afford to be quick enough for the crop box."""
        import op3t_webcam as m
        p = self.pipe
        z, cx, cy = p.auto_view
        msg = p.msg or (self.vcam.error if self.vcam is not None and not self.running else "")
        return {"state": p.state, "fps": p.fps, "frames": p.frames, "dropped": p.dropped, "msg": msg,
                "sz_req": round(p.sz.req, 2), "sz_obs": round(p.sz.obs, 2),
                "auto": p.af.enabled, "z": round(z, 3), "cx": round(cx, 4), "cy": round(cy, 4),
                "zmax": m.AF_ZOOM_MAX,
                "lost": p.af.enabled and (time.monotonic() - p.face_seen) > 1.2,
                "running": self.running, "cam_users": self.cam_users,
                "autocam": bool(self.cfg.get("autocam", True)), "autostart": self._autostart}

    def preview(self, on):
        self.pipe.preview_on = bool(on)

    def use_bridge_preview(self):
        """The <img> could not reach the MJPEG socket. Put the send thread back on the numpy/PPM
        path — otherwise it keeps building JPEGs nobody collects and _preview_rgb stays None, so the
        fallback would show a black rectangle for ever."""
        self.pipe.preview_fmt = "ppm"

    def get_preview(self):
        """Bridge fallback for a box with no cv2, or if the MJPEG socket cannot be reached."""
        pr = self.pipe._preview_rgb
        return {"img": _bmp_data_url(*pr) if pr else None}

    def set_view(self, z, cx, cy):
        # absolute crop-box state from the UI: zoom factor + crop centre (0..1). PC-side crop.
        z = float(z)
        if z <= 1.02:
            cx, cy = 0.5, 0.5
        self.pipe.set_zoom(z, float(cx), float(cy))


class Tray:
    """Notification-area icon, made with pythonnet on the window's own WinForms thread — the runtime
    pywebview already loads, so it adds no dependency (pystray, the usual pick, is LGPL-3.0).

    Left-click opens the window; the menu holds the session, the two startup switches and Quit.
    Closing the window only hides it here: the app has to keep running to notice an app turning the
    camera on. Windows logging off still closes it for real."""

    COLORS = {"streaming": (74, 222, 128), "connecting": (110, 168, 254), "error": (244, 88, 122)}
    IDLE = (152, 160, 184)
    # pywebview builds the JS bridge by recursing into every public attribute of the js_api object.
    # Api.tray is one, and crawling the live WinForms form under it stalled the bridge: the panel booted
    # with "api.options is not a function" (MEASURED 2026-10-04). This is pywebview's own opt-out.
    _serializable = False

    def __init__(self, api, form):
        self.api, self.form = api, form
        self.icon = self.timer = None
        self._icons = {}
        self._told = False

    def build(self):
        """Runs on the GUI thread (form.Invoke)."""
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System.Drawing import Font, FontStyle
        from System.Windows.Forms import (ContextMenuStrip, NotifyIcon, Timer, ToolStripMenuItem,
                                          ToolStripSeparator)
        api = self.api
        self.m_open = ToolStripMenuItem("Open OP3T Webcam")
        self.m_open.Font = Font(self.m_open.Font, FontStyle.Bold)
        self.m_open.Click += lambda s, e: api._show_window()
        self.m_go = ToolStripMenuItem("Start camera")
        self.m_go.Click += lambda s, e: self._toggle()
        self.m_auto = ToolStripMenuItem("Start when an app turns the camera on")
        self.m_auto.CheckOnClick = True
        self.m_auto.Click += lambda s, e: api.set_autocam(self.m_auto.Checked)
        self.m_boot = ToolStripMenuItem("Start with Windows")
        self.m_boot.CheckOnClick = True
        self.m_boot.Click += lambda s, e: api.set_autostart(self.m_boot.Checked)
        self.m_quit = ToolStripMenuItem("Quit")
        # off the GUI thread: destroy() marshals back onto it and would wait on itself
        self.m_quit.Click += lambda s, e: threading.Thread(target=api._quit_app, daemon=True).start()
        menu = ContextMenuStrip()
        for item in (self.m_open, self.m_go, ToolStripSeparator(), self.m_auto, self.m_boot,
                     ToolStripSeparator(), self.m_quit):
            menu.Items.Add(item)
        menu.Opening += lambda s, e: self._refresh_menu()
        self.icon = NotifyIcon()
        self.icon.ContextMenuStrip = menu
        self.icon.MouseClick += self._on_click
        self._tick(None, None)
        self.icon.Visible = True
        self.timer = Timer()
        self.timer.Interval = 1000
        self.timer.Tick += self._tick
        self.timer.Start()
        self.form.FormClosing += self._on_closing

    def notify(self, title, text):
        """A Windows notification from any thread."""
        if self.icon is None:
            return
        from System import Action
        from System.Windows.Forms import ToolTipIcon

        def show():
            if self.icon is not None:
                self.icon.ShowBalloonTip(4000, title, text, ToolTipIcon.Info)
        try:
            self.form.BeginInvoke(Action(show))
        except Exception:
            pass

    def dispose(self):
        """Take the icon down now, or it lingers in the tray until the mouse passes over it."""
        if self.icon is None:
            return
        from System import Action

        def gone():
            self.timer.Stop()
            self.icon.Visible = False
            self.icon.Dispose()
            self.icon = None
        try:
            if self.form.InvokeRequired:
                self.form.Invoke(Action(gone))
            else:
                gone()
        except Exception:
            pass

    def _toggle(self):
        api = self.api
        go = api.stop if api.running else (lambda: api.start(dict(api.cfg)))
        threading.Thread(target=go, daemon=True).start()    # stop() joins a thread: not on the GUI

    def _refresh_menu(self):
        self.m_go.Text = "Stop camera" if self.api.running else "Start camera"
        self.m_auto.Checked = bool(self.api.cfg.get("autocam", True))
        self.m_boot.Checked = self.api._autostart

    def _on_click(self, sender, e):
        from System.Windows.Forms import MouseButtons
        if e.Button == MouseButtons.Left:
            self.api._show_window()

    def _on_closing(self, sender, args):
        from System.Windows.Forms import CloseReason
        if self.api._quitting or args.CloseReason != CloseReason.UserClosing:
            return                                   # Quit, or Windows logging off: really close
        args.Cancel = True
        sender.Hide()
        if not self._told:
            self._told = True
            self.notify("Still running in the tray",
                        "OP3T Webcam keeps waiting for apps to turn the camera on. "
                        "Right-click its icon to quit.")

    def _tick(self, sender, e):
        p, api = self.api.pipe, self.api
        if p.state == "streaming":
            text = f"OP3T Webcam: streaming, {p.fps:.0f} fps"
        elif p.state == "connecting":
            text = "OP3T Webcam: connecting to the phone"
        elif p.state == "error":
            text = "OP3T Webcam: error, open for details"
        elif api.cfg.get("autocam", True):
            text = "OP3T Webcam: waiting for an app"
        else:
            text = "OP3T Webcam: stopped"
        self.icon.Icon = self._icon(p.state)
        self.icon.Text = text[:63]                   # NotifyIcon throws on anything longer

    def _icon(self, state):
        """A video-camera glyph in the state's colour, drawn once per state: no icon file to ship."""
        key = state if state in self.COLORS else "idle"
        if key in self._icons:
            return self._icons[key]
        from System import Array
        from System.Drawing import Bitmap, Color, Graphics, Icon, Point, SolidBrush
        from System.Drawing.Drawing2D import SmoothingMode
        r, g, b = self.COLORS.get(key, self.IDLE)
        bmp = Bitmap(32, 32)
        gr = Graphics.FromImage(bmp)
        gr.SmoothingMode = SmoothingMode.AntiAlias
        gr.Clear(Color.Transparent)
        fill = SolidBrush(Color.FromArgb(255, r, g, b))
        gr.FillRectangle(fill, 1, 8, 21, 16)                                   # body
        gr.FillPolygon(fill, Array[Point]([Point(22, 13), Point(31, 8), Point(31, 24), Point(22, 19)]))
        gr.FillEllipse(SolidBrush(Color.FromArgb(255, 7, 8, 13)), 6, 11, 10, 10)   # lens
        gr.Dispose()
        self._icons[key] = Icon.FromHandle(bmp.GetHicon())
        return self._icons[key]


def _listen_for_show(api, event):
    """A second launch signals this event (op3t_webcam._single_instance) instead of starting a copy."""
    import ctypes
    from ctypes import wintypes as w
    k32 = ctypes.WinDLL("kernel32")
    k32.WaitForSingleObject.restype = w.DWORD
    k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    while not api._quit.is_set():
        if k32.WaitForSingleObject(event, 500) == 0:          # WAIT_OBJECT_0
            api._show_window()


def run(ctx):
    try:
        import webview
    except Exception:
        return False
    tray_mode = bool(ctx.get("tray"))
    if getattr(sys, "frozen", False):
        # A moved exe leaves a login entry pointing at nothing: re-point it at this one.
        try:
            if ctx["get_autostart"]() not in ("", ctx["autostart_command"]()):
                ctx["set_autostart"](True)
        except Exception:
            pass
    api = Api(ctx)
    if not tray_mode and not api.cfg.get("autocam", True):
        # Wake the phone ahead of a manual Start. With auto-start the phone is woken when an app
        # turns the camera on instead: waking it here would leave its screen lit for nobody.
        ctx["adb_forward"](ctx["port"])
    srv = None
    try:
        srv, mport = ctx["start_mjpeg"](api.pipe)
        api.preview_url = f"http://127.0.0.1:{mport}/preview.mjpg"
    except Exception:
        api.preview_url = None          # the JS drops to the bridge poll on an <img> error
        api.pipe.preview_fmt = "ppm"    # ...which needs the numpy path building frames again
    # Controls left / preview right. Opens maximised; with --tray (Start with Windows) it starts hidden
    # and only the tray icon shows. 1280x720 is just what Restore Down goes back to.
    win = webview.create_window("OP3T Webcam", html=HTML, js_api=api,
                                width=1280, height=720, min_size=(900, 560),
                                resizable=True, background_color="#07080d",
                                maximized=True, hidden=tray_mode)
    api._start_background(win)
    if ctx.get("show_event"):
        threading.Thread(target=_listen_for_show, args=(api, ctx["show_event"]), daemon=True).start()

    def after_start():
        # webview.start's func: its own thread, once the GUI loop is up and the form exists.
        for _ in range(200):
            if getattr(win, "native", None) is not None:
                break
            time.sleep(0.05)
        form = getattr(win, "native", None)
        try:
            from System import Action
            tray = Tray(api, form)
            form.Invoke(Action(tray.build))
            api.tray = tray
            ctx["log_js_error"]("tray: ready")
        except Exception:
            import traceback
            ctx["log_js_error"]("tray failed: " + traceback.format_exc()[-1500:])
            if tray_mode:
                api._show_window()       # no tray to come back from: never leave it invisible

    def on_loaded():
        # One-shot health probe. A panel whose script fails to parse, or whose boot() never fires
        # because the bridge was not injected in time, looks EXACTLY like a working window with dead
        # controls — and reports nothing, because the reporter died with everything else.
        try:
            js = ("JSON.stringify({api:!!(window.pywebview&&window.pywebview.api),"
                  "boot:typeof boot,booted:(typeof booted!=='undefined')&&booted,"
                  "err:(typeof window.__lastErr!=='undefined')?window.__lastErr:null})")
            ctx["log_js_error"]("page loaded: " + str(win.evaluate_js(js)))
        except Exception as e:
            ctx["log_js_error"]("load probe failed: " + repr(e))

        def late():
            # The one that matters: did boot() finish and actually attach handlers? `loaded` fires
            # before boot's await returns, so the interesting state only exists a moment later.
            try:
                js2 = ("JSON.stringify({booted:booted,go:typeof (document.getElementById('go')||{}).onclick,"
                       "af:typeof (document.getElementById('af')||{}).onchange,"
                       "banner:(document.getElementById('jserr')||{}).textContent||'',"
                       "err:window.__lastErr||null})")
                ctx["log_js_error"]("late probe: " + str(win.evaluate_js(js2)))
            except Exception as e:
                ctx["log_js_error"]("late probe failed: " + repr(e))
        threading.Timer(5.0, late).start()

    def on_closed():
        api.save(api.cfg)
        api.stop()
    win.events.loaded += on_loaded
    win.events.closed += on_closed
    webview.start(after_start)
    api._shutdown()                     # the auto-start watcher, the stream, the virtual camera
    api.pipe._mjpeg_stop.set()          # release the MJPEG writer loops before shutting the server
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
    # start() returns only once the window is really gone. Closing the APP (not pressing Stop) is
    # what puts the phone to sleep and drops the adb server — see shutdown_phone for why the adb
    # server matters to PyInstaller's temp-dir cleanup. Done here rather than in on_closed so it
    # still runs if the closed event never fires.
    try:
        ctx["shutdown_phone"](ctx["port"])
    except Exception:
        pass
    return True
