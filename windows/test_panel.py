#!/usr/bin/env python3
"""Actually RUN the panel's JavaScript. Run: python test_panel.py   (exits 1 on failure)

WHY. test_webui.py checks statically that every `api.X(` in the HTML exists on Api, and that every
element id the JS reaches exists in the markup. Both passed while the shipped panel was completely
dead: nothing in a static check notices that boot() throws on its third line and therefore never
attaches a single event handler. The whole UI is one function's success away from being inert, and
pywebview swallows the error silently.

So this drives the real script in node against a stub DOM and a stub bridge whose method names come
from the REAL Api class. It asserts boot() completes, handlers land on the controls, and the
controls actually do something when poked.

Skips (does not fail) if node is unavailable.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import op3t_webcam as m
import webui

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  " + detail) if detail else ""))
    if not ok:
        fails.append(name)


node = shutil.which("node")
if not node:
    print("SKIP  node not found — cannot execute the panel JS")
    sys.exit(0)

script = webui.HTML.split("<script>")[1].split("</script>")[0]
# Proof that this suite actually discriminates: PANEL_MUTATE=1 shortens the boot retry window back
# to something like the 1 s one that shipped broken. Every assertion below MUST fail then. Without
# this the suite could be green for the wrong reason.
if os.environ.get("PANEL_MUTATE"):
    script = script.replace("tries>=300", "tries>=5")
    print("(mutation: boot retry window shortened — this run is EXPECTED to fail)")
ids = sorted(set(re.findall(r'\bid="([a-zA-Z0-9_]+)"', webui.HTML)))
api_methods = sorted(n for n in dir(webui.Api) if not n.startswith("_"))
cfg = dict(m.DEFAULTS)

HARNESS = r"""
// ---- stub DOM -------------------------------------------------------------------------------
const REAL_IDS = __IDS__, API_METHODS = __API__, CFG = __CFG__;
const log = [];
class CL {
  constructor(el){ this.el=el; this.s=new Set(); }
  add(c){ this.s.add(c); } remove(c){ this.s.delete(c); }
  contains(c){ return this.s.has(c); }
  toggle(c,on){ if(on===undefined) on=!this.s.has(c); on?this.s.add(c):this.s.delete(c); }
}
class El {
  constructor(id){ this.id=id; this.classList=new CL(this); this.style={}; this.dataset={};
    this.textContent=''; this.innerHTML=''; this.value='0'; this.checked=false;
    this.min='0'; this.max='1'; this.step='1'; this.children=[]; this._listeners={}; }
  appendChild(c){ this.children.push(c); return c; }
  addEventListener(t,f){ (this._listeners[t]=this._listeners[t]||[]).push(f); }
  removeAttribute(){} setAttribute(){}
  getBoundingClientRect(){ return {width:640,height:360,left:0,top:0}; }
  get className(){ return [...this.classList.s].join(' '); }
  set className(v){ this.classList.s=new Set(String(v).split(/\s+/).filter(Boolean)); }
}
const NODES = {};
for (const id of REAL_IDS) NODES[id] = new El(id);
const segLabels = ['off','auto','on'].map(m=>{ const e=new El('seg-'+m); e.dataset.m=m; return e; });

global.document = {
  getElementById: id => NODES[id] || null,      // unknown id -> null, exactly like the browser
  createElement: () => new El('opt'),
  querySelectorAll: sel => sel.includes('#seg') ? segLabels : [],
  addEventListener: () => {},
};
global.window = { addEventListener: (t,f)=>{ if(t==='pywebviewready') window._ready=f; } };
global.performance = { now: () => Date.now() };
const realSetTimeout = setTimeout, realSetInterval = setInterval, realClearInterval = clearInterval;
// setInterval stays REAL: the boot bootstrap retries on it while waiting for the bridge, and that
// retry loop is the thing under test. setTimeout is stubbed dead so debounced saves never fire.
global.setInterval = (f,ms) => { log.push('interval:'+ms); return realSetInterval(f,ms); };
global.clearInterval = (h) => realClearInterval(h);
global.setTimeout = (f,ms) => { return 1; };       // never fires: saves must be debounced, not eager
global.clearTimeout = () => {};

// ---- stub bridge: ONLY the methods the real Api actually has --------------------------------
const calls = [];
const apiStub = {};    // NOT `api` — the panel declares its own `api` in the same scope
for (const name of API_METHODS) {
  apiStub[name] = async (...a) => {
    calls.push(name);
    if (name === 'options') return {resolutions:['1920x1080'], fps:['30','60'],
      rotations:['0','90','180','270'], decoders:['NVIDIA (cuvid)','CPU'],
      bitrates:[{value:12,label:'12 Mbps'}], preview_url:'http://127.0.0.1:1/preview.mjpg',
      config: CFG};
    if (name === 'status') return {state:'streaming',fps:30,frames:10,dropped:0,msg:'',
      sz_req:1.0, sz_obs:1.0, auto:false, z:1, cx:0.5, cy:0.5, zmax:2.0, lost:false};
    if (name === 'get_preview') return {img:null};
    return null;
  };
}
// THE BRIDGE ARRIVES LATE, ON PURPOSE. pywebview took NINE SECONDS to inject its api on the dev
// machine (measured 2026-08-28); a bootstrap that gives up after 1 s leaves every control unwired
// and reports nothing. 2.5 s here is far longer than the old 1 s window and short enough to test.
// ...AND IT ARRIVES EMPTY FIRST, like the real thing: pywebview's api.js creates `pywebview.api` as {}
// and finish.js fills it in one go later (_createApi). A bootstrap that boots on the empty object
// throws "api.options is not a function" and never retries (MEASURED 2026-10-05 under a busy start).
realSetTimeout(() => { global.window.pywebview = { api: {} }; }, 1000);
realSetTimeout(() => { Object.assign(global.window.pywebview.api, apiStub); }, 2500);

// ---- run the panel --------------------------------------------------------------------------
__SCRIPT__

// ---- assertions -----------------------------------------------------------------------------
const results = [];
const t = (name, ok, detail) => results.push({name, ok: !!ok, detail: detail || ''});

(async () => {
  // Wait for the panel to notice the late bridge and finish booting. The whole point: a bootstrap
  // that stops retrying never gets here, and every assertion below fails.
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline && !calls.includes('options')) {
    await new Promise(r => realSetTimeout(r, 100));
  }
  await new Promise(r => realSetTimeout(r, 100));   // let boot's awaits settle

  t('boot survives a bridge that appears late', calls.includes('options'));
  t('boot() called api.options()', calls.includes('options'));
  t('Start button has a click handler', typeof NODES.go.onclick === 'function');
  t('Preview button has a click handler', typeof NODES.pvbtn.onclick === 'function');
  t('Auto-frame has a change handler', typeof NODES.af.onchange === 'function');
  t('main slider has an input handler', typeof NODES.z.oninput === 'function');
  t('sensor segments have click handlers', segLabels.every(l => typeof l.onclick === 'function'));
  t('setup dropdowns are wired', typeof NODES.fps.onchange === 'function'
      && typeof NODES.dec.onchange === 'function');
  t('status polling was scheduled', log.some(l => l.startsWith('interval:')));

  // behaviour, not just presence: the slider must change its own name with auto-frame
  if (typeof NODES.af.onchange === 'function') {
    NODES.af.checked = true;
    await NODES.af.onchange({target: NODES.af});
    t('auto-frame ON renames the slider', NODES.znm.textContent === 'Framing tightness',
      NODES.znm.textContent);
    t('auto-frame ON reports tracking', NODES.afst.textContent === 'tracking', NODES.afst.textContent);
    NODES.af.checked = false;
    await NODES.af.onchange({target: NODES.af});
    t('auto-frame OFF renames it back to Zoom', NODES.znm.textContent === 'Zoom',
      NODES.znm.textContent);
  }
  // the sensor slider appears only in "On", and the readout is never empty
  if (segLabels[2].onclick) {
    segLabels[1].onclick();                       // auto
    t('sensor slider hidden in auto', NODES.szsl.style.display === 'none', NODES.szsl.style.display);
    segLabels[2].onclick();                       // on
    t('sensor slider shown in on', NODES.szsl.style.display === '', NODES.szsl.style.display);
    segLabels[0].onclick();                       // off
    t('sensor readout explains off mode', /Off/.test(NODES.szline.innerHTML), NODES.szline.innerHTML);
  }
  // dragging a slider must not write config on every event
  if (typeof NODES.z.oninput === 'function') {
    const before = calls.filter(c => c === 'save').length;
    NODES.z.value = '2.0';
    for (let i = 0; i < 30; i++) NODES.z.oninput({target: NODES.z});
    t('30 slider events cause no immediate save',
      calls.filter(c => c === 'save').length === before,
      'saves=' + (calls.filter(c => c === 'save').length - before));
  }
  console.log('__RESULTS__' + JSON.stringify(results));
  process.exit(0);                    // the panel's own poll interval would keep node alive
})().catch(e => {
  console.log('__RESULTS__' + JSON.stringify([{name:'panel script ran without throwing',
    ok:false, detail:String(e && e.stack || e).split('\n').slice(0,3).join(' | ')}]));
  process.exit(0);
});
"""

harness = (HARNESS
           .replace("__IDS__", json.dumps(ids))
           .replace("__API__", json.dumps(api_methods))
           .replace("__CFG__", json.dumps(cfg))
           .replace("__SCRIPT__", script))

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "panel_harness.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write(harness)
    proc = subprocess.run([node, path], capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")

out = proc.stdout or ""
if "__RESULTS__" not in out:
    # node refused to even parse the panel script — that IS the failure, and the message says where
    err = (proc.stderr or "").strip().splitlines()
    check("panel JS parses and runs", False, " | ".join(err[:6]) or f"exit {proc.returncode}")
else:
    check("panel JS parses and runs", True)
    for r in json.loads(out.split("__RESULTS__")[1].splitlines()[0]):
        check(r["name"], r["ok"], r.get("detail", ""))

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
