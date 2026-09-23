// Shared "Add to Home Screen" logic, included on the app, login and register pages (each has an
// #install-slot element the button gets inserted into). Independent of app.js's state/routing.
//
// Chrome (Android/desktop, over HTTPS) can genuinely install with one tap via beforeinstallprompt - so that's
// what's offered there. Safari on iOS has never implemented that API and Apple has given no sign it will
// (confirmed against current docs, not assumed), so there's no way to automate it - the button instead shows
// the exact manual steps. Android browsers that don't fire the event (e.g. over the plain-HTTP home-Wi-Fi
// mode, where Chrome requires a secure context) get the same kind of manual fallback, so the button is never
// just silently missing on a phone that could use it.
(function () {
  "use strict";

  function isStandalone() {
    return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  }
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const isAndroid = /android/i.test(navigator.userAgent);

  if ("serviceWorker" in navigator) {
    // Fails silently on plain HTTP off localhost (e.g. the LAN/home-Wi-Fi mode) - service workers require a
    // secure context there, and that's expected, not an error worth surfacing.
    navigator.serviceWorker.register("/static/sw.js").catch(() => {});
  }

  function modal(title, steps) {
    const overlay = document.createElement("div");
    overlay.className = "install-overlay";
    overlay.innerHTML = `<div class="card install-modal" role="dialog" aria-modal="true" aria-label="${title}">
      <h2 style="font-size:16px;margin-bottom:10px">${title}</h2>
      <ol>${steps.map((s) => `<li>${s}</li>`).join("")}</ol>
      <button class="btn primary" type="button" style="width:100%;margin-top:8px">Got it</button>
    </div>`;
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.querySelector("button").addEventListener("click", () => overlay.remove());
    document.addEventListener("keydown", function esc(e) { if (e.key === "Escape") { overlay.remove(); document.removeEventListener("keydown", esc); } });
    document.body.appendChild(overlay);
  }

  const iosSteps = [
    'Tap the <b>Share</b> button (the square with an arrow) in Safari’s toolbar.',
    'Scroll down and tap <b>Add to Home Screen</b>.',
    'Tap <b>Add</b> in the top right.',
  ];
  const androidFallbackSteps = [
    'Tap the <b>⋮</b> menu in the top right of Chrome.',
    'Tap <b>Add to Home screen</b> (or <b>Install app</b>).',
    'Tap <b>Add</b> / <b>Install</b> to confirm.',
  ];

  function makeButton(label, onClick) {
    const slot = document.getElementById("install-slot");
    if (!slot) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn";
    btn.textContent = label;
    btn.addEventListener("click", onClick);
    slot.innerHTML = "";
    slot.appendChild(btn);
  }

  if (isStandalone()) return;   // already installed - nothing to offer

  let deferredPrompt = null;
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferredPrompt = e;
    makeButton("Install app", async () => {
      const slot = document.getElementById("install-slot");
      if (slot) slot.innerHTML = "";
      deferredPrompt.prompt();
      await deferredPrompt.userChoice;
      deferredPrompt = null;
    });
  });
  window.addEventListener("appinstalled", () => {
    const slot = document.getElementById("install-slot");
    if (slot) slot.innerHTML = "";
  });

  if (isIOS) {
    makeButton("Add to Home Screen", () => modal("Add to Home Screen", iosSteps));
  } else if (isAndroid) {
    // give Chrome a moment to fire beforeinstallprompt (it may already be installable) before falling back
    setTimeout(() => {
      if (!deferredPrompt && !isStandalone()) makeButton("Add to Home Screen", () => modal("Add to Home Screen", androidFallbackSteps));
    }, 1500);
  }
})();
