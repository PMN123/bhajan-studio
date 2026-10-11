"use strict";

// Register the service worker: it makes the app installable and lets the page open without a network.
// Browsers only allow service workers in a secure context: https, or localhost / 127.0.0.1.
if ("serviceWorker" in navigator && window.isSecureContext) {
  try {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch((err) => {
      console.warn("Bhajan Studio: service worker not registered:", err);
    });
  } catch (err) {
    console.warn("Bhajan Studio: service worker not registered:", err);
  }
}
