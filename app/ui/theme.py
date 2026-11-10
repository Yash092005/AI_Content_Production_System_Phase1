CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root{--bg:#070a14;--panel:rgba(255,255,255,.04);--line:rgba(255,255,255,.09);--txt:#e6e9f2;--mute:#8d93ad;
--violet:#8b5cf6;--cyan:#06b6d4;--green:#22c55e;--red:#ef4444;--amber:#f59e0b}
html,body,[class*="css"],.stApp{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif!important}
.stApp{background:radial-gradient(900px 500px at 82% -8%,rgba(139,92,246,.18),transparent 60%),
 radial-gradient(700px 420px at -5% 12%,rgba(6,182,212,.10),transparent 60%),var(--bg)}
[data-testid="stHeader"]{background:transparent}
[data-testid="stToolbar"],[data-testid="stDecoration"],footer,#MainMenu{display:none!important}
.block-container{max-width:1240px;padding-top:1rem;padding-bottom:4rem}
[data-testid="stSidebar"]{background:rgba(10,14,28,.92);border-right:1px solid var(--line)}
[data-testid="stSidebar"] .block-container{padding-top:1.4rem}

/* top navigation */
.topnav{display:flex;align-items:center;justify-content:space-between;padding:.7rem 1.1rem;margin-bottom:1.1rem;
 border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.035);backdrop-filter:blur(10px)}
.brand{display:flex;align-items:center;gap:.65rem;font-weight:800;letter-spacing:-.02em;font-size:1.05rem}
.logo-mark{width:32px;height:32px;border-radius:10px;display:grid;place-items:center;color:#fff;font-size:1rem;
 background:linear-gradient(135deg,var(--violet),var(--cyan));box-shadow:0 6px 20px rgba(139,92,246,.45)}
.navlinks{display:flex;gap:1.4rem;color:var(--mute);font-size:.86rem;font-weight:500}
.navlinks b{color:var(--txt);font-weight:600}
.pill{display:inline-flex;align-items:center;gap:.4rem;padding:.25rem .7rem;border-radius:99px;font-size:.72rem;font-weight:700;
 border:1px solid var(--line);background:var(--panel);color:var(--mute)}
.pill i{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green)}

/* hero */
.hero{position:relative;overflow:hidden;padding:2.6rem 2.4rem;border-radius:26px;margin-bottom:1.6rem;border:1px solid rgba(139,92,246,.28);
 background:radial-gradient(circle at 88% 18%,rgba(6,182,212,.28),transparent 32%),
 radial-gradient(circle at 8% 110%,rgba(139,92,246,.35),transparent 40%),linear-gradient(135deg,rgba(24,18,60,.9),rgba(10,14,30,.9));
 box-shadow:0 30px 80px rgba(0,0,0,.35)}
.hero:before{content:"";position:absolute;inset:0;background-image:linear-gradient(rgba(255,255,255,.04) 1px,transparent 1px),
 linear-gradient(90deg,rgba(255,255,255,.04) 1px,transparent 1px);background-size:38px 38px;
 mask-image:radial-gradient(circle at 70% 30%,#000,transparent 70%);-webkit-mask-image:radial-gradient(circle at 70% 30%,#000,transparent 70%)}
.hero>*{position:relative}
.eyebrow{font-size:.7rem;letter-spacing:.16em;font-weight:800;color:#b9a6ff}
.hero h1{margin:.5rem 0 .6rem;font-size:3.1rem;line-height:1.05;letter-spacing:-.05em;font-weight:800;
 background:linear-gradient(90deg,#fff,#cdbfff 55%,#7fe6f5);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{margin:0;max-width:640px;color:#b7bcd4;font-size:1.04rem;line-height:1.6}
.chips{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:1.2rem}
.chip{padding:.32rem .75rem;border-radius:99px;font-size:.76rem;font-weight:600;color:#d8d2ff;
 background:rgba(139,92,246,.14);border:1px solid rgba(139,92,246,.3)}

/* sections */
.sec{display:flex;align-items:center;gap:.7rem;margin:1.9rem 0 .8rem}
.sec .n{width:32px;height:32px;border-radius:10px;display:grid;place-items:center;font-size:.78rem;font-weight:800;color:#fff;
 background:linear-gradient(135deg,var(--violet),var(--cyan))}
.sec h3{margin:0;font-size:1.28rem;letter-spacing:-.02em;font-weight:700}
.sec small{color:var(--mute);font-weight:500;margin-left:.2rem}
.card{border:1px solid var(--line);border-radius:18px;background:var(--panel);padding:1rem 1.15rem}
.mode-card{border:1px solid var(--line);border-radius:14px;padding:.8rem .9rem;margin:.6rem 0;background:var(--panel)}
.mode-card strong{display:block;font-size:.92rem}.mode-card span{font-size:.78rem;color:var(--mute)}
.muted{color:var(--mute);font-size:.82rem}

/* inputs */
textarea,input{border-radius:14px!important}
[data-testid="stTextArea"] textarea{background:rgba(255,255,255,.035)!important;border:1px solid var(--line)!important;font-size:.98rem!important}
[data-testid="stTextArea"] textarea:focus{border-color:var(--violet)!important;box-shadow:0 0 0 3px rgba(139,92,246,.25)!important}
[data-testid="stRadio"] [role="radiogroup"]{gap:.5rem}
[data-testid="stRadio"] [role="radiogroup"] label{padding:.5rem .95rem;border:1px solid var(--line);border-radius:12px;background:var(--panel);transition:.18s}
[data-testid="stRadio"] [role="radiogroup"] label:hover{border-color:rgba(139,92,246,.5)}
[data-testid="stRadio"] [role="radiogroup"] label:has(input:checked){border-color:var(--violet);background:rgba(139,92,246,.16)}
.stButton>button,.stDownloadButton>button{border-radius:12px;font-weight:650;min-height:2.6rem;border:1px solid var(--line);transition:.18s}
.stButton>button:hover,.stDownloadButton>button:hover{border-color:var(--violet);transform:translateY(-1px)}
.stButton>button[kind="primary"]{border:0;color:#fff;font-size:1.02rem;min-height:3.1rem;
 background:linear-gradient(90deg,#7c3aed,#06b6d4);box-shadow:0 12px 34px rgba(124,58,237,.4)}
.stButton>button[kind="primary"]:hover{filter:brightness(1.1);transform:translateY(-2px)}
[data-testid="stExpander"]{border:1px solid var(--line)!important;border-radius:14px!important;background:var(--panel)}
[data-testid="stTabs"] [role="tablist"]{gap:.35rem;border-bottom:1px solid var(--line)}
[data-testid="stTabs"] button{font-weight:650;border-radius:10px 10px 0 0}

/* pipeline stepper */
.pipe{display:grid;grid-template-columns:repeat(5,1fr);gap:.8rem;margin:.2rem 0 1rem}
.ps{position:relative;display:flex;gap:.7rem;align-items:center;padding:.9rem .85rem;border-radius:16px;border:1px solid var(--line);
 background:var(--panel);transition:.25s}
.ps .dot{flex:none;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:.82rem;
 background:rgba(255,255,255,.07);color:var(--mute)}
.ps b{display:block;font-size:.95rem}.ps span{font-size:.76rem;color:var(--mute)}
.ps.active{border-color:var(--violet);background:rgba(139,92,246,.12);box-shadow:0 0 0 3px rgba(139,92,246,.12)}
.ps.active .dot{background:transparent;border:3px solid rgba(139,92,246,.25);border-top-color:var(--violet);animation:spin .9s linear infinite;color:transparent}
.ps.done{border-color:rgba(34,197,94,.35)}.ps.done .dot{background:rgba(34,197,94,.18);color:var(--green)}
.ps.error{border-color:rgba(239,68,68,.5)}.ps.error .dot{background:rgba(239,68,68,.18);color:var(--red)}
@keyframes spin{to{transform:rotate(360deg)}}

/* strategy */
.fwbar{display:flex;gap:1rem;align-items:center;padding:1rem 1.2rem;border-radius:18px;margin:.4rem 0 1rem;
 border:1px solid rgba(6,182,212,.35);background:linear-gradient(135deg,rgba(6,182,212,.12),rgba(139,92,246,.08))}
.fwbadge{flex:none;min-width:92px;text-align:center;padding:.6rem .9rem;border-radius:14px;color:#fff;
 background:linear-gradient(135deg,#7c3aed,#06b6d4);box-shadow:0 8px 24px rgba(124,58,237,.35)}
.fwbadge small{display:block;font-size:.58rem;letter-spacing:.14em;opacity:.85;font-weight:700}.fwbadge b{font-size:1.45rem;letter-spacing:-.02em}
.fwbar strong{font-size:1rem}.fwbar p{margin:.25rem 0 0;color:#b7bcd4;font-size:.9rem;line-height:1.5}
.fwsrc{margin-left:.6rem;font-size:.7rem;font-weight:700;color:#7fe6f5;letter-spacing:.04em}
.stbl{width:100%;border-collapse:collapse;font-size:.92rem}
.stbl th{width:190px;text-align:left;color:var(--mute);font-weight:600;padding:.6rem .8rem;border-bottom:1px solid var(--line);vertical-align:top}
.stbl td{padding:.6rem .8rem;border-bottom:1px solid var(--line)}
.warn-box{padding:.8rem 1rem;margin:.5rem 0;border-radius:14px;border:1px solid rgba(245,158,11,.35);background:rgba(245,158,11,.08);color:#f6d58b;font-size:.9rem}

/* result */
.verdict{display:flex;gap:1.1rem;align-items:center;padding:1.3rem 1.5rem;border-radius:20px;border:1px solid var(--line);margin:.3rem 0 1rem}
.verdict .ic{width:54px;height:54px;border-radius:16px;display:grid;place-items:center;font-size:1.6rem;flex:none}
.verdict h2{margin:0;font-size:1.5rem;letter-spacing:-.03em}.verdict p{margin:.2rem 0 0;color:#b7bcd4;font-size:.92rem}
.verdict.pass{background:linear-gradient(135deg,rgba(34,197,94,.14),rgba(34,197,94,.03));border-color:rgba(34,197,94,.35)}
.verdict.pass .ic{background:rgba(34,197,94,.2)}
.verdict.fail{background:linear-gradient(135deg,rgba(239,68,68,.15),rgba(239,68,68,.03));border-color:rgba(239,68,68,.38)}
.verdict.fail .ic{background:rgba(239,68,68,.2)}
.verdict.warn{background:linear-gradient(135deg,rgba(245,158,11,.15),rgba(245,158,11,.03));border-color:rgba(245,158,11,.38)}
.verdict.warn .ic{background:rgba(245,158,11,.2)}
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:.7rem;margin-bottom:1rem}
.stat{border:1px solid var(--line);border-radius:14px;padding:.8rem .9rem;background:var(--panel)}
.stat small{display:block;color:var(--mute);font-size:.7rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase}
.stat b{font-size:1.25rem;letter-spacing:-.02em}
.issue{display:flex;gap:.8rem;padding:.85rem 1rem;margin:.5rem 0;border-radius:14px;border:1px solid rgba(239,68,68,.28);
 border-left:4px solid var(--red);background:rgba(239,68,68,.07);font-size:.93rem;line-height:1.5}
.issue .no{flex:none;width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-size:.72rem;font-weight:800;
 background:rgba(239,68,68,.22);color:#ffb4b4}
.ok-box{padding:.9rem 1.1rem;border-radius:14px;border:1px solid rgba(34,197,94,.3);background:rgba(34,197,94,.08);color:#9be7b5}
.check{display:inline-flex;align-items:center;gap:.4rem;margin:.2rem .35rem .2rem 0;padding:.28rem .7rem;border-radius:99px;font-size:.76rem;font-weight:600;
 border:1px solid var(--line);background:var(--panel)}
.check.y{color:#8be8ac;border-color:rgba(34,197,94,.3)}.check.n{color:#ffa3a3;border-color:rgba(239,68,68,.35)}
.hist{display:flex;gap:.55rem;align-items:flex-start;font-size:.8rem;padding:.45rem 0;border-bottom:1px solid var(--line);color:#c3c8de}
.hist i{flex:none;width:8px;height:8px;margin-top:.38rem;border-radius:50%}
.footer-note{text-align:center;color:var(--mute);opacity:.6;font-size:.75rem;margin-top:2.4rem}
@media(max-width:1100px){.pipe{grid-template-columns:repeat(3,1fr)}}
@media(max-width:900px){.pipe{grid-template-columns:1fr 1fr}.stats{grid-template-columns:repeat(3,1fr)}.hero h1{font-size:2.2rem}.navlinks{display:none}}
</style>
"""


EXTRA = """
/* ===== deployment polish ===== */
[data-testid="stMain"]{scroll-behavior:smooth}
.anchor{display:block;position:relative;top:-18px;visibility:hidden;height:0}
a{color:inherit}
:focus-visible{outline:2px solid #a78bfa!important;outline-offset:2px;border-radius:8px}
.block-container{padding-top:1.2rem;max-width:1180px}

/* header */
.topnav{position:relative;gap:1rem;flex-wrap:wrap}
.topnav .brand{text-decoration:none;color:var(--txt)}
.topnav .brand svg{flex:none;filter:drop-shadow(0 6px 14px rgba(139,92,246,.45))}
.navlinks a{color:var(--mute);text-decoration:none;font-weight:550;transition:.15s}
.navlinks a:hover{color:#fff}
.spill{display:inline-flex;align-items:center;gap:.45rem;padding:.3rem .75rem;border-radius:99px;font-size:.74rem;font-weight:700;border:1px solid var(--line);background:var(--panel);color:var(--mute);white-space:nowrap}
.spill i{width:8px;height:8px;border-radius:50%;background:var(--mute)}
.spill.live{color:#8be8ac;border-color:rgba(34,197,94,.35)}.spill.live i{background:var(--green);box-shadow:0 0 10px var(--green)}
.spill.demo{color:#f6d58b;border-color:rgba(245,158,11,.4)}.spill.demo i{background:var(--amber)}
.spill.error{color:#ffa3a3;border-color:rgba(239,68,68,.45)}.spill.error i{background:var(--red)}

/* hero */
.hero2 a.btn-primary,.hero2 a.btn-ghost,.topnav a,.foot a{text-decoration:none!important}
.hero2{position:relative;overflow:hidden;display:grid;grid-template-columns:1.2fr .8fr;gap:2.2rem;align-items:center;
 padding:2.8rem 2.6rem;border-radius:28px;margin-bottom:2rem;border:1px solid rgba(139,92,246,.3);
 background:radial-gradient(circle at 92% 12%,rgba(6,182,212,.30),transparent 34%),radial-gradient(circle at 4% 115%,rgba(139,92,246,.38),transparent 42%),linear-gradient(135deg,rgba(24,18,60,.92),rgba(9,13,30,.92));
 box-shadow:0 36px 90px rgba(0,0,0,.4)}
.hero2:before{content:"";position:absolute;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.04) 1px,transparent 1px);
 background-size:40px 40px;-webkit-mask-image:radial-gradient(circle at 75% 25%,#000,transparent 70%);mask-image:radial-gradient(circle at 75% 25%,#000,transparent 70%)}
.hero2>*{position:relative}
.hero2 h1{margin:.55rem 0 .8rem;font-size:3.2rem;line-height:1.04;letter-spacing:-.05em;font-weight:800;
 background:linear-gradient(90deg,#fff,#d4c8ff 55%,#7fe6f5);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero2 p{margin:0;max-width:560px;color:#b9bfd8;font-size:1.05rem;line-height:1.65}
.cta-row{display:flex;gap:.8rem;margin-top:1.5rem;flex-wrap:wrap}
.btn-primary,.btn-ghost{display:inline-block;padding:.8rem 1.35rem;border-radius:13px;font-weight:700;font-size:.95rem;text-decoration:none;transition:.18s}
.btn-primary{color:#fff!important;background:linear-gradient(90deg,#7c3aed,#06b6d4);box-shadow:0 12px 30px rgba(124,58,237,.42)}
.btn-primary:hover{transform:translateY(-2px);filter:brightness(1.1)}
.btn-ghost{color:#dcd6ff!important;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.04)}
.btn-ghost:hover{border-color:var(--violet);background:rgba(139,92,246,.12)}
.trust{display:flex;gap:2rem;margin-top:1.7rem;flex-wrap:wrap}
.trust div{display:flex;flex-direction:column}.trust b{font-size:1.15rem;letter-spacing:-.02em}.trust span{font-size:.76rem;color:var(--mute)}
.hero-card{border-radius:20px;padding:1.2rem 1.3rem;background:rgba(8,11,24,.72);border:1px solid rgba(255,255,255,.12);
 box-shadow:0 24px 60px rgba(0,0,0,.45);backdrop-filter:blur(8px);transform:rotate(1.2deg)}
.hc-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:.9rem}
.hc-tag{font-size:.62rem;letter-spacing:.14em;font-weight:800;color:#b9a6ff}
.hc-dots{display:flex;gap:5px}.hc-dots i{width:9px;height:9px;border-radius:50%;background:rgba(255,255,255,.18)}
.hc-title{font-size:1.2rem;font-weight:750;letter-spacing:-.02em}.hc-sub{color:var(--mute);font-size:.8rem;margin:.15rem 0 .9rem}
.hc-ok{display:flex;gap:.7rem;align-items:center;padding:.7rem .8rem;border-radius:13px;background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.3)}
.hc-ok span{font-size:1.3rem}.hc-ok b{display:block;font-size:.92rem}.hc-ok small{color:#9fdcb4;font-size:.72rem}
.hc-rows{display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem;margin:.9rem 0}
.hc-rows div{padding:.55rem .65rem;border-radius:11px;border:1px solid var(--line);background:var(--panel)}
.hc-rows small{display:block;font-size:.62rem;color:var(--mute);text-transform:uppercase;letter-spacing:.06em;font-weight:700}.hc-rows b{font-size:1.05rem}
.hc-bars i{display:block;height:7px;border-radius:99px;margin:.4rem 0;background:linear-gradient(90deg,var(--violet),var(--cyan));opacity:.75}

/* section headings + how it works */
.sec-head{margin:3.2rem 0 1.2rem;text-align:center}
.sec-head h2{margin:.4rem 0 .5rem;font-size:2rem;letter-spacing:-.04em;font-weight:800}
.sec-head p{margin:0 auto;max-width:560px;color:var(--mute)}
.how-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:1rem}
.how-card{padding:1.3rem;border-radius:20px;border:1px solid var(--line);background:var(--panel);transition:.2s}
.how-card:hover{transform:translateY(-4px);border-color:rgba(139,92,246,.5);background:rgba(139,92,246,.07)}
.how-n{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:10px;font-size:.78rem;font-weight:800;color:#fff;background:linear-gradient(135deg,var(--violet),var(--cyan))}
.how-card h4{margin:.9rem 0 .35rem;font-size:1.05rem;letter-spacing:-.02em}.how-card p{margin:0;color:#aab0c8;font-size:.88rem;line-height:1.6}

/* cards around the studio */
.st-key-studio_card,.st-key-strategy_card,.st-key-run_card,.st-key-result_card,.st-key-faq_card{
 border:1px solid var(--line);border-radius:24px;background:rgba(255,255,255,.028);padding:1.4rem 1.6rem 1.5rem;
 box-shadow:0 18px 50px rgba(0,0,0,.22);margin-bottom:1.1rem}
.sec{margin-top:.2rem}

/* empty state */
.empty{text-align:center;padding:2.4rem 1.2rem;border:1px dashed rgba(255,255,255,.16);border-radius:22px;background:rgba(255,255,255,.02)}
.empty-ic{font-size:2rem}.empty h3{margin:.4rem 0 .3rem;font-size:1.35rem;letter-spacing:-.03em}.empty p{color:var(--mute);margin:0 0 1.2rem}
.empty-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:.8rem;max-width:820px;margin:0 auto}
.empty-tile{display:flex;flex-direction:column;gap:.25rem;align-items:center;padding:1rem;border-radius:16px;border:1px solid var(--line);background:var(--panel)}
.empty-tile span{font-size:1.4rem}.empty-tile small{color:var(--mute);font-size:.78rem;line-height:1.45}

/* footer */
.foot{margin-top:3rem;padding-top:2rem;border-top:1px solid var(--line)}
.foot-grid{display:grid;grid-template-columns:1.4fr 1fr 1.4fr;gap:2rem}
.foot .brand{display:flex;align-items:center;gap:.65rem;font-weight:800;text-decoration:none;color:var(--txt)}
.foot p{color:var(--mute);font-size:.85rem;line-height:1.6;max-width:330px}
.foot h5{margin:.2rem 0 .5rem!important;padding:0!important;font-size:.72rem!important;letter-spacing:.12em;color:#b9a6ff;text-transform:uppercase;line-height:1.3}
.foot .brand{margin-bottom:.2rem}
.foot-grid span{display:block;color:#aab0c8;font-size:.85rem;margin:.3rem 0}
.foot-bar{display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap;margin-top:1.6rem;padding:1rem 0 .4rem;border-top:1px solid var(--line);color:var(--mute);font-size:.78rem}

/* login */
.st-key-login_card{max-width:440px;margin:7vh auto 0;padding:2rem 2rem 1.6rem;border:1px solid var(--line);border-radius:24px;background:rgba(255,255,255,.035);box-shadow:0 30px 80px rgba(0,0,0,.4)}
.login-head{text-align:center;margin-bottom:1rem}.login-head h2{margin:.7rem 0 .3rem;font-size:1.5rem;letter-spacing:-.03em}.login-head p{margin:0;color:var(--mute);font-size:.9rem}

/* sidebar */
.sidecard{padding:.8rem .9rem;border:1px solid var(--line);border-radius:14px;background:var(--panel);margin:.4rem 0 1rem}
.sidecard b{display:block;font-size:.72rem;letter-spacing:.1em;color:var(--mute);margin-bottom:.45rem}.sidecard small{display:block;color:var(--mute);margin-top:.45rem;line-height:1.45}

@media(max-width:1000px){.hero2{grid-template-columns:1fr;padding:2rem 1.5rem}.hero-card{display:none}.hero2 h1{font-size:2.5rem}
 .how-grid{grid-template-columns:1fr 1fr}.foot-grid{grid-template-columns:1fr 1fr}}
@media(max-width:640px){.hero2 h1{font-size:2rem}.how-grid,.empty-grid,.foot-grid{grid-template-columns:1fr}.pipe{grid-template-columns:1fr 1fr}.ps{padding:.7rem .6rem;gap:.5rem}.ps .dot{width:28px;height:28px}
 .stats{grid-template-columns:1fr 1fr}.topnav{padding:.7rem .8rem}.trust{gap:1.2rem}.sec-head h2{font-size:1.5rem}
 .st-key-studio_card,.st-key-strategy_card,.st-key-run_card,.st-key-result_card,.st-key-faq_card{padding:1rem 1rem 1.1rem;border-radius:18px}
 .fwbar{flex-direction:column;align-items:flex-start}.verdict{flex-direction:column;align-items:flex-start}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
"""
EXTRA += """
/* configuration screen */
.cfgbar{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;padding:.8rem 1.1rem;margin:.2rem 0 1rem;border-radius:16px;
 border:1px solid rgba(34,197,94,.35);background:linear-gradient(135deg,rgba(34,197,94,.12),rgba(34,197,94,.03))}
.cfgbar b{font-size:1rem}.cfgbar span{color:#b7bcd4;font-size:.85rem}
.chg{padding:.35rem .6rem;margin:.25rem 0;border-radius:10px;border:1px solid var(--line);background:var(--panel);font-size:.85rem;overflow-wrap:anywhere}
.chg code{color:#b9a6ff;background:transparent}.chg span{color:var(--mute)}
.st-key-view_switch{margin:.2rem 0 1rem}
.refcard{padding:1rem 1.1rem;margin:.6rem 0;border-radius:16px;border:1px solid rgba(6,182,212,.35);background:linear-gradient(135deg,rgba(6,182,212,.09),rgba(139,92,246,.05))}
.refhead{display:flex;gap:.8rem;align-items:baseline;flex-wrap:wrap;margin-bottom:.6rem}.refhead b{font-size:1.05rem}
.refrow{display:flex;gap:.8rem;align-items:flex-start;padding:.3rem 0;border-bottom:1px solid var(--line);font-size:.9rem}
.refrow>b{flex:none;width:118px;color:var(--mute);font-weight:600}.refrow>span{flex:1}
.chip.warn{color:#f6d58b;border-color:rgba(245,158,11,.4);background:rgba(245,158,11,.1)}
.refcard details{margin:.4rem 0;font-size:.88rem}.refcard summary{cursor:pointer;color:#b9a6ff;font-weight:600}.refcard ul{margin:.3rem 0 .2rem 1.1rem}
"""
CSS = CSS.replace("</style>", EXTRA + "\n</style>")
