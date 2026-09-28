(() => {
  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/service-worker.js', {scope: '/'}).catch(() => {});
  }
  let installPrompt;
  const install = document.querySelector('[data-install]');
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault(); installPrompt = event; if (install) install.hidden = false;
  });
  install?.addEventListener('click', async () => {
    if (!installPrompt) return;
    await installPrompt.prompt(); installPrompt = null; install.hidden = true;
  });
  window.addEventListener('appinstalled', () => { if (install) install.hidden = true; });
  // Offline writes are never queued; refresh server state after connectivity returns.
  document.addEventListener('submit', event => {
    if (!navigator.onLine && event.target.method.toLowerCase() === 'post') {
      event.preventDefault(); event.stopImmediatePropagation();
      const status = document.querySelector('#offline');
      if (status) { status.hidden = false; status.textContent = 'Reconnect before saving. No changes have been sent.'; status.scrollIntoView(); }
    }
  }, true);
  window.addEventListener('online', () => {
    const status = document.querySelector('#connection-restored');
    if (status) status.hidden = false;
  });
})();
