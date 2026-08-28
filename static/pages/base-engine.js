// ── Global i18n engine ──
    window.__SUPPORTED_LANGS=["en","zh","ja","ko","es","ar","de","ru","pt","fr"];
    window.__LOCALE_MAP={en:"en",zh:"zh-CN",ja:"ja-JP",ko:"ko-KR",es:"es-ES",ar:"ar",de:"de-DE",ru:"ru-RU",pt:"pt-BR",fr:"fr-FR"};
    window.__LANG_LABELS={"en":"English","zh":"简体中文","ja":"日本語","ko":"한국어","es":"Español","ar":"العربية","de":"Deutsch","ru":"Русский","pt":"Português","fr":"Français"};
        window.__afterI18n=null;
    window.t=function(k,v){var I=window.__I18N__||{};var T=I[window.__LANG]||I.en||{};var s=T[k]||k;if(v){for(var p in v)s=s.split("{"+p+"}").join(v[p])}return s};
    window.applyI18n=function(){
      var L=window.__LANG||"en";
      document.documentElement.lang=window.__LOCALE_MAP[L]||"en";
      document.documentElement.dir=L==="ar"?"rtl":"ltr";
      document.querySelectorAll("[data-i18n]").forEach(function(el){try{el.textContent=window.t(el.getAttribute("data-i18n"))}catch(err){}});
      document.querySelectorAll("[data-i18n-placeholder]").forEach(function(el){try{el.setAttribute("placeholder",window.t(el.getAttribute("data-i18n-placeholder")))}catch(err){}});
      if(typeof window.__afterI18n==="function")window.__afterI18n();
    };
    // Language switcher
    (function(){
      if(window.__LANG_INIT)return;window.__LANG_INIT=true;
      var lb=document.getElementById("langButton"),lm=document.getElementById("langMenu");
      if(!lb||!lm)return;
      lb.textContent=window.__LANG_LABELS[window.__LANG]||"English";
      lb.addEventListener("click",function(){var h=lm.classList.toggle("hidden");lb.setAttribute("aria-expanded",h?"false":"true")});
      lm.innerHTML=window.__SUPPORTED_LANGS.map(function(c){return'<button class="lang-option'+(c===window.__LANG?" active":"")+'" type="button" role="option" aria-selected="'+(c===window.__LANG)+'" data-lang="'+c+'">'+window.__LANG_LABELS[c]+"</button>"}).join("");
      lm.addEventListener("click",function(e){var o=e.target.closest("[data-lang]");if(!o)return;var l=window.__SUPPORTED_LANGS.indexOf(o.dataset.lang)>=0?o.dataset.lang:"en";window.__LANG=l;document.cookie="flashshare_lang="+l+";Path=/;Max-Age=31536000;SameSite=Lax";if(typeof window.__afterI18n==="function"){lb.textContent=window.__LANG_LABELS[l]||"English";lm.classList.add("hidden");lb.setAttribute("aria-expanded","false");lm.querySelectorAll(".lang-option").forEach(function(b){b.classList.toggle("active",b.dataset.lang===l);b.setAttribute("aria-selected",b.dataset.lang===l)});window.applyI18n()}else{location.reload()}});
      document.addEventListener("click",function(e){if(e.target===lb||lm.contains(e.target))return;lm.classList.add("hidden");lb.setAttribute("aria-expanded","false")});
    })();
