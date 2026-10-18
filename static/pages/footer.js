// Header credit chip sync — called by app.js after a metered tool call
window.__syncCredits = function (n) {
  var el = document.getElementById("headerCredits");
  if (el && typeof n === "number") el.textContent = String(n);
};
// Top-bar interaction wiring (user dropdown, history modal open/close,
// mobile drawer) lives in base-engine.js — the site-wide single source.
// History modal + data loading (shared across all pages)
(function(){
var _ts=window.__I18N__ ? (window.__I18N__[window.__LANG]||{}) : {};
function _tx(k,v){var I=window.__I18N__||{};var _t=I[window.__LANG]||_ts||{};var s=(_t[k]||k);if(v){for(var p in v)s=s.split("{"+p+"}").join(v[p])}return s}
function _sz(n){if(!n)return"0 B";var u=["B","KB","MB","GB"],i=0,x=n;while(x>=1024&&i<u.length-1){x/=1024;i++}return x.toFixed(i?1:0)+" "+u[i]}
function _dt(ts){if(!ts)return"-";try{return new Date(ts*1000).toLocaleString((window.__LOCALE_MAP||{})[window.__LANG]||"en",{hour12:false})}catch(e){return"-"}}
function _eh(s){return String(s||"").replace(/[&<>"']/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
var _ic=window.__ICONS__||{};
function _si(n){var p=_ic[n]||_ic.info||"";return'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+p+"</svg>"}
var _et={jpg:"image",jpeg:"image",png:"image",gif:"image",webp:"image",bmp:"image",ico:"image",tiff:"image",svg:"image",pdf:"pdf",doc:"doc",docx:"doc",xls:"sheet",xlsx:"sheet",ppt:"slides",pptx:"slides",zip:"archive",rar:"archive","7z":"archive",txt:"text",csv:"text",md:"text",json:"text",xml:"text",html:"text",css:"text",js:"text",py:"text",go:"text"};
function _fi(n){var m=(n||"").toLowerCase().match(/\.(\w+)$/);if(!m)return"";var tp=_et[m[1]]||"file";var p=_ic[tp]||_ic.doc||_ic.text||"";return'<span class="fmt-badge fmt-'+tp+'" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'+p+"</svg></span>"}
function _dl(x){var t=x.allowed_downloads;if(Number.isFinite(x.download_count))return _tx("downloadsDetail",{used:x.download_count,total:t});if(Number.isFinite(x.downloads_remaining))return _tx("downloadsDetail",{used:Math.max(0,t-x.downloads_remaining),total:t});return _tx("downloadsUnknown",{total:t})}

var _uploads=[];var _histOffset=0;var _histLoading=false;var _histGen=0;
async function _del(pid,btn){var ok=await(window.confirmAction?window.confirmAction({title:_tx("delete"),message:_tx("confirmDelete"),confirmLabel:_tx("delete"),cancelLabel:_tx("cancel")}):Promise.resolve(window.confirm(_tx("confirmDelete"))));if(!ok)return;btn.disabled=true;fetch("/api/uploads/"+pid,{method:"DELETE"}).then(function(r){return r.json()}).then(function(d){if(d.ok){var it=btn.closest(".item");if(it)it.remove();var l=document.getElementById("historyList");var hc=document.getElementById("historyCount");var n=l?l.querySelectorAll(".item").length:0;if(hc)hc.textContent=n?String(n):"";if(l&&!n)l.setAttribute("data-empty",_tx("noHistory"))}else{btn.disabled=false}}).catch(function(){btn.disabled=false})}

function _qr(url){var m=document.getElementById("homeQrModal");if(!m){m=document.createElement("div");m.id="homeQrModal";m.className="modal hidden";m.setAttribute("role","dialog");m.setAttribute("aria-modal","true");m.innerHTML='<div class="modal-panel qr-modal-panel"><button class="modal-close" type="button" aria-label="Close">'+_si("close")+'</button><img alt="QR" width="200" height="200" class="qr-code"></div>';document.body.appendChild(m);var cb=m.querySelector(".modal-close");cb.addEventListener("click",function(){m.classList.add("hidden")});m.addEventListener("click",function(e){if(e.target===m)m.classList.add("hidden")})}var im=m.querySelector("img");im.classList.add("qr-loading");im.onload=function(){im.classList.remove("qr-loading","qr-error")};im.onerror=function(){im.classList.remove("qr-loading");im.classList.add("qr-error")};im.src=url;m.classList.remove("hidden")}

function _pwa(){return window.isStandalonePWA?window.isStandalonePWA():(window.matchMedia&&window.matchMedia("(display-mode: standalone)").matches||window.navigator.standalone===true)}

function _renderItem(x,i){
var s=x.url_download;
return'<div class="item"><a class="item-link" href="'+_eh(s)+'"><span class="item-main"><span class="item-title">'+_fi(x.file_name)+'<span class="item-name">'+_eh(x.file_name)+'</span></span><span class="item-sub">'+_tx("itemMeta",{size:_sz(x.size_bytes),days:x.expiry_days,date:_dt(x.expire_at)})+" · "+_dl(x)+'</span></span></a><div class="item-actions"><button class="item-tool" type="button" data-dl="'+i+'" aria-label="'+_tx("downloadFile")+'" title="'+_tx("downloadFile")+'">'+_si("download")+'</button><button class="item-tool" type="button" data-cp="'+i+'" aria-label="'+_tx("copy")+'" title="'+_tx("copy")+'">'+_si("copy")+'</button><button class="item-tool" type="button" data-sh="'+i+'" aria-label="'+_tx("share_via_system")+'" title="'+_tx("share_via_system")+'">'+_si("share")+'</button><button class="item-tool" type="button" data-qr="'+i+'" aria-label="'+_tx("qrCode")+'" title="'+_tx("qrCode")+'">'+_si("qr")+'</button><button class="item-tool danger-action" type="button" data-rm="'+i+'" aria-label="'+_tx("delete")+'" title="'+_tx("delete")+'">'+_si("trash")+'</button></div></div>'
}
function _bindActions(l){
l.querySelectorAll("[data-dl]").forEach(function(b){b.addEventListener("click",async function(e){e.preventDefault();e.stopPropagation();if(window._dlLock)return;window._dlLock=true;var u=_uploads[+b.dataset.dl];var dl=u.url_download+"?download=1";var hm=document.getElementById("historyModal");if(_pwa()){if(hm)hm.dataset.locked="1";await(window.pwaDownload?pwaDownload(dl):Promise.resolve());if(hm)delete hm.dataset.locked}else window.location.href=dl;setTimeout(function(){window._dlLock=false},1000)})});
l.querySelectorAll("[data-cp]").forEach(function(b){b.addEventListener("click",function(){var u=_uploads[+b.dataset.cp];b.style.color="var(--accent)";setTimeout(function(){b.style.color=""},1400);window.copyFeedback(b);navigator.clipboard.writeText(u.url_download).catch(function(){})})});
l.querySelectorAll("[data-sh]").forEach(function(b){b.addEventListener("click",function(){var u=_uploads[+b.dataset.sh];window.shareLink&&window.shareLink(u.url_download,u.file_name,b)})});
l.querySelectorAll("[data-qr]").forEach(function(b){b.addEventListener("click",function(){var u=_uploads[+b.dataset.qr];_qr(u.url_download+"/qr.svg")})});
l.querySelectorAll("[data-rm]").forEach(function(b){b.addEventListener("click",function(){var u=_uploads[+b.dataset.rm];_del(u.public_id,b)})})
}
window._loadHistory=function(){
var l=document.getElementById("historyList");if(!l)return;
var hc=document.getElementById("historyCount");
var gen=++_histGen;
l.innerHTML='<div class="upload-spinner"></div>';l.classList.add("loading");l.setAttribute("data-empty","");
_uploads=[];_histOffset=0;
fetch("/api/uploads?offset=0").then(function(r){return r.json()}).then(function(d){
if(gen!==_histGen)return;
l.classList.remove("loading");
if(!d||!d.ok||!d.uploads||!d.uploads.length){_uploads=[];l.innerHTML="";l.setAttribute("data-empty",_tx("noHistory"));if(hc)hc.textContent="";return}
_uploads=d.uploads;_histOffset=d.uploads.length;if(hc)hc.textContent=String(d.uploads.length);
l.innerHTML=d.uploads.map(function(x,i){return _renderItem(x,i)}).join("");
_bindActions(l);
if(d.has_more)_appendSentinel(l,gen)
})["catch"](function(){if(gen!==_histGen)return;l.classList.remove("loading");l.innerHTML="";l.setAttribute("data-empty",_tx("noHistory"))})
};
function _appendSentinel(l,gen){
var s=document.createElement("div");s.className="hist-sentinel";s.style.height="1px";l.appendChild(s);
var ob=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting&&!_histLoading){_histLoading=true;ob.disconnect();var off=_histOffset;fetch("/api/uploads?offset="+off).then(function(r){return r.json()}).then(function(d){if(gen!==_histGen){_histLoading=false;return}if(d&&d.ok&&d.uploads&&d.uploads.length){d.uploads.forEach(function(x){_uploads.push(x)});var html=d.uploads.map(function(x,i){return _renderItem(x,_uploads.length-d.uploads.length+i)}).join("");var nd=document.createElement("div");nd.innerHTML=html;_bindActions(nd);l.removeChild(s);while(nd.firstChild)l.appendChild(nd.firstChild);_histOffset=_uploads.length;if(d.has_more)_appendSentinel(l,gen)}_histLoading=false})["catch"](function(){_histLoading=false})}})},{root:l,rootMargin:"40px"});ob.observe(s)
}
})();
