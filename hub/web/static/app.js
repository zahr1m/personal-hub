// Тема, вход через мини-приложение Telegram, Bearer для HTMX. Без фреймворков.
(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp;
  var KEY = "hub_token";
  var root = document.documentElement;
  var mq = window.matchMedia("(prefers-color-scheme: dark)");

  function store(op, k, v) {
    try { return op === "get" ? sessionStorage.getItem(k) : sessionStorage.setItem(k, v); }
    catch (e) { return null; }
  }

  function applyTheme() {
    var dark = tg && tg.initData ? tg.colorScheme === "dark" : mq.matches;
    root.setAttribute("data-theme", dark ? "hub-dark" : "hub-light");
  }
  applyTheme();
  mq.addEventListener("change", applyTheme);
  if (tg) { tg.onEvent("themeChanged", applyTheme); }

  // initData -> сессия; токен живёт в sessionStorage и уходит заголовком Bearer
  function tmaLogin() {
    if (!tg || !tg.initData) { return Promise.resolve(false); }
    return fetch("/auth/tma", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ init_data: tg.initData }),
    }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { return false; }
        store("set", KEY, data.token);
        return true;
      }).catch(function () { return false; });
  }

  // не чаще одного «войти и перезагрузить» за 30 с: защита от петли
  // sessionStorage недоступен (бросает) - не перезагружаем вовсе, иначе петля
  function reloadGuardOpen() {
    try {
      var last = Number(sessionStorage.getItem("hub_reauth_at")) || 0;
      if (Date.now() - last < 30000) { return false; }
      sessionStorage.setItem("hub_reauth_at", String(Date.now()));
      return true;
    } catch (e) { return false; }
  }

  function loginAndReload() {
    if (!reloadGuardOpen()) { return; }
    // не вышло войти - перезагрузка (GET без сессии) покажет подсказку входа
    tmaLogin().then(function () { window.location.reload(); });
  }

  document.addEventListener("htmx:configRequest", function (e) {
    var token = store("get", KEY);
    var sameOrigin = new URL(e.detail.path, window.location.href).origin === window.location.origin;
    if (token && sameOrigin) { e.detail.headers.Authorization = "Bearer " + token; }
  });

  document.addEventListener("reauth", loginAndReload);

  // форма с data-reset очищается после успешной отправки; тост гаснет сам
  document.addEventListener("htmx:afterRequest", function (e) {
    var el = e.detail.elt;
    if (e.detail.successful && el && el.hasAttribute && el.hasAttribute("data-reset")) { el.reset(); }
  });
  function clearToastLater(el) { setTimeout(function () { el.replaceChildren(); }, 6000); }
  document.addEventListener("htmx:oobAfterSwap", function (e) {
    var el = e.target;
    if (el && el.id === "toast") { clearToastLater(el); }
  });
  // ошибка ответа (404 «Нет доступа» и т. п.) - текст в тост, только как textContent
  document.addEventListener("htmx:responseError", function (e) {
    var el = document.getElementById("toast");
    if (!el) { return; }
    var box = document.createElement("div");
    box.className = "alert alert-error";
    box.setAttribute("role", "alert");
    box.textContent = String(e.detail.xhr.responseText || "Ошибка").trim().slice(0, 200);
    el.replaceChildren(box);
    clearToastLater(el);
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (tg) { tg.ready(); tg.expand(); }
    if (tg && tg.initData) {
      if (document.body.hasAttribute("data-login")) {
        loginAndReload(); // страница «Войдите»: токен в хранилище не важен
      } else if (!store("get", KEY)) {
        tmaLogin(); // cookie уже есть, токен нужен только для Bearer в HTMX
      }
    }
    var main = document.getElementById("main");
    var back = main && main.getAttribute("data-back");
    if (tg && tg.BackButton) {
      if (back) {
        tg.BackButton.onClick(function () { window.location.href = back; });
        tg.BackButton.show();
      } else {
        tg.BackButton.hide();
      }
    }
  });
})();
