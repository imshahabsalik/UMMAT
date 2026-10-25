(function () {
  const dlg = document.getElementById('quoteDialog');
  if (!dlg) return;
  const form = document.getElementById('quoteForm');
  const status = document.getElementById('quoteStatus');
  const sendBtn = document.getElementById('quoteSend');

  function clearErrors() {
    form.querySelectorAll('.field-error').forEach(s => { s.textContent = ''; });
    status.hidden = true;
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.quote-btn');
    if (!btn) return;
    form.reset();
    clearErrors();
    sendBtn.disabled = false;
    sendBtn.textContent = 'Send enquiry';
    document.getElementById('q_store').value = btn.dataset.store;
    document.getElementById('q_product').value = btn.dataset.product ?? '';
    document.getElementById('q_product_name').value = btn.dataset.productName || '';
    document.getElementById('quoteFor').textContent = btn.dataset.label || '';
    dlg.showModal();
  });

  document.getElementById('quoteCancel').addEventListener('click', () => dlg.close());

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors();
    const fd = Object.fromEntries(new FormData(form).entries());
    fd.product_index = fd.product_index === '' ? null : Number(fd.product_index);
    sendBtn.disabled = true;
    sendBtn.textContent = 'Sending…';
    try {
      const res = await fetch('/api/enquiry', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': form.elements._csrf_token.value
        },
        body: JSON.stringify(fd)
      });
      const data = await res.json();
      if (!res.ok) {
        for (const [k, msg] of Object.entries(data.fields || {})) {
          const el = form.querySelector('[data-for="' + k + '"]');
          if (el) el.textContent = msg;
        }
        status.className = 'alert error';
        status.textContent = data.error || 'Could not send your enquiry.';
        status.hidden = false;
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send enquiry';
        return;
      }
      status.className = 'alert success';
      status.textContent = 'Your enquiry has been recorded.';
      status.hidden = false;
      sendBtn.textContent = 'Sent ✓';
    } catch (err) {
      status.className = 'alert error';
      status.textContent = 'Network problem. Please check your connection and try again.';
      status.hidden = false;
      sendBtn.disabled = false;
      sendBtn.textContent = 'Send enquiry';
    }
  });
})();
