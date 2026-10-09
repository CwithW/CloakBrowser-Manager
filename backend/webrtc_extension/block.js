(() => {
  if (!__BLOCK_WEBRTC__) return;
  for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection"]) {
    Object.defineProperty(window, name, {
      value: undefined, writable: false, configurable: false,
    });
  }
})();
