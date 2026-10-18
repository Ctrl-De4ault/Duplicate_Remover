const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const api=()=>window.pywebview.api;
const S={folders:[],protect:[],rest:{},extra:[],raw:null,shown:0,action:'recycle'};
const KEEP=['keep_newest','keep_oldest','keep_shortest_path'];
const T={scan:'Scanning folders…',partial:'Comparing files…',full:'Verifying matches…',execute:'Cleaning up…'};
const fmt=b=>{const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++}return b.toFixed(i?1:0)+' '+u[i]};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const show=id=>$$('.view').forEach(v=>v.classList.toggle('active',v.id===id));
function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('on');clearTimeout(toast.t);toast.t=setTimeout(()=>t.classList.remove('on'),4000)}

function chips(id,key){const l=S[key];$(id).innerHTML=l.map((p,i)=>`<span class="chip">${esc(p)}<b data-i="${i}">×</b></span>`).join('')||'<span class="hint">None yet</span>';
 $$(id+' b').forEach(b=>b.onclick=()=>{l.splice(+b.dataset.i,1);render()})}
function render(){chips('#folders','folders');chips('#protect','protect')}

function build(){const exts=$('#exts').value.split(/[ ,]+/).filter(Boolean);
 const p={...S.rest,name:$('#pname').value.trim()||'Untitled',folders:S.folders,
  rules:[...S.protect.map(folder=>({kind:'protect_folder',folder})),...S.extra,{kind:$('#keep').value}],action:$('#action').value};
 if(exts.length)p.extensions=exts;
 if(S.raw&&S.raw.preview_confirmed)p.preview_confirmed=S.raw.preview_confirmed;
 return p}
function load(p){const {name,folders,rules=[],action,extensions,preview_confirmed,...rest}=p;
 const k=rules.find(r=>KEEP.includes(r.kind));
 Object.assign(S,{raw:p,rest,folders:[...folders],protect:rules.filter(r=>r.kind==='protect_folder').map(r=>r.folder),extra:rules.filter(r=>r!==k&&r.kind!=='protect_folder')});
 $('#keep').value=k?k.kind:'keep_oldest';$('#action').value=action||'recycle';
 $('#exts').value=(extensions||[]).join(', ');$('#pname').value=name;$('#quick').hidden=!preview_confirmed;render();show('home')}

async function plist(){const r=await api().profiles();
 $('#plist').innerHTML=r.profiles.map((p,i)=>`<div class="pitem" data-i="${i}"><span>${esc(p.name)}</span>${p.preview_confirmed?'<em>ready</em>':''}<b data-d="${i}">×</b></div>`).join('')||'<p class="hint" style="padding:0 10px">None saved yet</p>';
 $$('.pitem').forEach(el=>el.onclick=async e=>{const p=r.profiles[+el.dataset.i];
  if(e.target.dataset.d!==undefined){if(confirm(`Delete profile “${p.name}”?`)){await api().delete_profile(p.name);plist()}}else load(p)})}

function prog(s){$('#stitle').textContent=T[s.stage]||'Working…';
 $('#stext').textContent=s.total?`${s.done} of ${s.total}`:`${s.done} files found`;
 $('#sbar').style.width=s.total?(100*s.done/s.total)+'%':'30%';$('#sbar').classList.toggle('ind',!s.total)}
async function go(kind,call){show('scanning');$('#cancel').hidden=kind!=='run';prog({stage:'scan',done:0,total:0});
 const r=await call;if(r.error){toast(r.error);show('home');return}
 const t=setInterval(async()=>{const s=await api().status();prog(s);if(s.busy)return;clearInterval(t);
  if(s.error){toast(s.error);show('home')}else kind==='scan'?review():finish()},250)}

const gHtml=g=>`<div class="group${g.skipped?' off':''}" data-g="${g.id}"><div class="gh"><b>${esc(g.files[0].name)}</b><span>${g.files.length} copies · ${fmt(g.size)} each</span><label><input type="checkbox" ${g.skipped?'checked':''}> Skip</label></div>${g.files.map(f=>`<div class="file ${f.keep?'keep':''}" data-f="${f.id}"><span class="pill ${f.protected&&!f.keep?'prot':''}">${f.keep?'Keep':f.protected?'Protected':'Remove'}</span><div class="fn"><b>${esc(f.name)}</b><small title="${esc(f.path)}">${esc(f.folder)}</small></div><small>${f.date}</small></div>`).join('')}</div>`;
function sum(s){const c=$('#clean');$('#rtitle').textContent=s.groups?`${s.groups} duplicate groups`:'No duplicates found';
 $('#rsub').textContent=s.groups?`${s.files} files can be removed · ${fmt(s.bytes)} recoverable`:'Your folders are clean.';
 c.disabled=!s.files;c.textContent=`Clean up ${s.files} files`;c.classList.toggle('danger',S.action==='delete')}
async function more(){const r=await api().results(S.shown,50);S.shown+=r.groups.length;
 $('#groups').insertAdjacentHTML('beforeend',r.groups.map(gHtml).join(''));sum(r.summary);$('#more').hidden=S.shown>=r.total}
async function review(){S.shown=0;$('#groups').innerHTML='';await more();show('review')}
$('#groups').onclick=async e=>{const el=e.target.closest('.group');if(!el)return;const id=+el.dataset.g;
 if(e.target.matches('input')){const r=await api().skip_group(id,e.target.checked);el.classList.toggle('off',e.target.checked);return sum(r.summary)}
 const f=e.target.closest('.file');if(f){const r=await api().set_keep(id,+f.dataset.f);el.outerHTML=gHtml(r.group);sum(r.summary)}};

async function finish(){const r=await api().run_result();show('done');
 $('#dtitle').textContent=r.cancelled?'Stopped early':`Recovered ${fmt(r.bytes)}`;
 $('#dtext').textContent=`${r.done} files handled`+(r.skipped?` · ${r.skipped} skipped for safety`:'')+(r.failed?` · ${r.failed} failed`:'')+(r.note?` · ${r.note}`:'');
 $('#undo').hidden=!r.can_undo;plist()}

const pick=key=>async()=>{const f=await api().pick_folders();f.forEach(x=>S[key].includes(x)||S[key].push(x));render()};
$('#addf').onclick=pick('folders');$('#addp').onclick=pick('protect');
$('#scan').onclick=()=>{const p=build();if(!p.folders.length)return toast('Add at least one folder first.');S.action=p.action;go('scan',api().start_scan(p))};
$('#quick').onclick=()=>{const p=build();if(p.action==='delete'&&!confirm('This profile deletes files permanently. Continue?'))return;go('run',api().quick_run(p,p.action==='delete'))};
$('#clean').onclick=()=>{if(S.action==='delete'&&!confirm('Delete permanently? This cannot be undone.'))return;go('run',api().run(S.action==='delete'))};
$('#save').onclick=async()=>{const r=await api().save_profile(build());if(r.error)toast(r.error);else{toast('Profile saved');plist()}};
$('#more').onclick=more;$('#cancel').onclick=()=>api().cancel();
$('#back').onclick=$('#again').onclick=()=>show('home');
$('#undo').onclick=async()=>{const r=await api().undo_last();toast(r.error||`Restored ${r.restored} files`);$('#undo').hidden=true};
$('#newp').onclick=()=>{Object.assign(S,{folders:[],protect:[],rest:{},extra:[],raw:null});$('#pname').value=$('#exts').value='';$('#quick').hidden=true;render();show('home')};
window.addEventListener('pywebviewready',()=>{render();plist()});
