/* WebRTC mirror upgrade — progressive: try a real video track over the
   existing mirror; on ANY failure keep the JPEG frames (never worse). */
(function () {
  const IMG_ID = "mirror-img";
  const VID_ID = "mirror-video";
  function el(id) { return document.getElementById(id); }
  let pc = null;

  async function tryWebRTC() {
    if (pc) return;
    if (!window.RTCPeerConnection) return;
    try {
      const offer = await pc0();
      const resp = await fetch("/api/webrtc/offer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ sdp: offer.sdp }),
      });
      if (!resp.ok) return;               // 503 (no aiortc) → JPEG path
      const data = await resp.json();
      if (!data.sdp) return;
      await pc.setRemoteDescription({ type: "answer", sdp: data.sdp });
    } catch (_) {
      teardown();                          // silent — JPEG keeps flowing
    }
  }

  async function pc0() {
    pc = new RTCPeerConnection({ iceServers: [] });
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.ontrack = (ev) => {
      const v = el(VID_ID), img = el(IMG_ID);
      if (!v) return;
      v.srcObject = ev.streams[0] || new MediaStream([ev.track]);
      v.style.display = "block";
      if (img) img.style.display = "none"; // supersede JPEG when live
      v.play && v.play().catch(() => {});
    };
    pc.onconnectionstatechange = () => {
      if (["failed", "closed", "disconnected"].includes(pc.connectionState))
        fallback();
    };
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    return offer;
  }

  function fallback() {
    teardown();
    const v = el(VID_ID), img = el(IMG_ID);
    if (v) v.style.display = "none";
    if (img) img.style.display = "";
  }

  function teardown() {
    if (pc) { try { pc.close(); } catch (_) {} pc = null; }
  }

  // Hook: the existing doMirror() toggles — observe mirror-view visibility.
  window.JARVIS_TRY_WEBRTC = tryWebRTC;
  window.JARVIS_WEBRTC_FALLBACK = fallback;
  document.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest && e.target.closest("#mirror-btn");
    if (!btn) return;
    setTimeout(() => {
      const view = el("mirror-view");
      if (view && view.style.display !== "none") tryWebRTC();
      else fallback();
    }, 120);
  });
})();
