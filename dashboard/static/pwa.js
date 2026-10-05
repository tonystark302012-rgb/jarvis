/* JARVIS PWA bootstrap: register SW + subscribe to web push once. */
(function () {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  const SUB_KEY = "jarvis_push_sub";
  async function boot() {
    try {
      const reg = await navigator.serviceWorker.register("/sw.js");
      if (Notification.permission === "denied") return;
      const existing = await reg.pushManager.getSubscription();
      if (existing) { sync(existing); return; }
      if (Notification.permission !== "granted") {
        // do not nag on load — wait for a user gesture on the bell if any
        const bell = document.querySelector("[data-jarvis-push]");
        if (bell) bell.addEventListener("click", () => subscribe(reg));
        return;
      }
      await subscribe(reg);
    } catch (_) { /* offline/dev — PWA is progressive, never blocks */ }
  }
  async function subscribe(reg) {
    try {
      const keyResp = await fetch("/api/push/public-key");
      if (!keyResp.ok) return;
      const { key } = await keyResp.json();
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
      });
      sync(sub);
    } catch (_) {}
  }
  function sync(sub) {
    try {
      localStorage.setItem(SUB_KEY, JSON.stringify(sub));
      fetch("/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sub),
      });
    } catch (_) {}
  }
  function urlBase64ToUint8Array(base64String) {
    const pad = "=".repeat((4 - (base64String.length % 4)) % 4);
    const b64 = (base64String + pad).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(b64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
