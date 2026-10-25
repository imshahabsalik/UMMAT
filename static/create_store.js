(function () {
  const rows = document.getElementById('productRows');
  const tpl = document.getElementById('productTemplate');
  const addBtn = document.getElementById('addProduct');
  const note = document.getElementById('limitNote');
  const form = document.getElementById('storeForm');
  const submitBtn = document.getElementById('submitBtn');
  const clientError = document.getElementById('clientError');
  const MAX = window.MAX_PRODUCTS || 20;
  const MAX_BYTES = 5 * 1024 * 1024;
  const password = form.elements.account_password;
  const passwordConfirm = form.elements.account_password_confirm;

  function checkPasswordMatch() {
    if (!passwordConfirm) return;
    passwordConfirm.setCustomValidity(passwordConfirm.value && passwordConfirm.value !== password.value
      ? 'Passwords do not match.' : '');
  }
  if (password && passwordConfirm) {
    password.addEventListener('input', checkPasswordMatch);
    passwordConfirm.addEventListener('input', checkPasswordMatch);
  }

  function refresh() {
    const all = rows.querySelectorAll('.product-row');
    all.forEach((r, i) => { r.querySelector('.row-no').textContent = i + 1; });
    const full = all.length >= MAX;
    addBtn.disabled = full;
    note.hidden = !full;
  }

  // Add Another Product: appends a NEW blank row; existing rows are untouched.
  addBtn.addEventListener('click', () => {
    if (rows.querySelectorAll('.product-row').length >= MAX) return;
    rows.appendChild(tpl.content.cloneNode(true));
    refresh();
    const last = rows.lastElementChild;
    last.scrollIntoView({ behavior: 'smooth', block: 'center' });
    last.querySelector('input').focus({ preventScroll: true });
  });

  rows.addEventListener('click', (e) => {
    const btn = e.target.closest('.remove-row');
    if (!btn) return;
    if (rows.querySelectorAll('.product-row').length > 1) {
      btn.closest('.product-row').remove();
      refresh();
    }
  });

  // Friendly image size/type checks (the server fully decodes and re-validates).
  function validateImage(input, preview) {
    const file = input.files[0];
    if (preview) preview.hidden = true;
    if (!file) return;
    if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
      alert('Please choose a JPG, PNG or WebP image.');
      input.value = '';
      return;
    }
    if (file.size > MAX_BYTES) {
      alert('That image is larger than 5 MB. Please choose a smaller one.');
      input.value = '';
      return;
    }
    if (preview) {
      preview.src = URL.createObjectURL(file);
      preview.hidden = false;
    }
  }

  rows.addEventListener('change', (e) => {
    if (!e.target.classList.contains('photo-input')) return;
    const input = e.target;
    const preview = input.closest('.product-row').querySelector('.photo-preview');
    validateImage(input, preview);
  });
  const logoInput = form.querySelector('input[name="store_logo"]');
  if (logoInput) logoInput.addEventListener('change', () => validateImage(logoInput));

  form.addEventListener('submit', (e) => {
    checkPasswordMatch();
    clientError.hidden = true;
    const bad = form.querySelector(':invalid');
    if (!form.checkValidity() && bad) {
      e.preventDefault();
      clientError.textContent = 'Please fill in the highlighted required fields.';
      clientError.hidden = false;
      bad.reportValidity();
      return;
    }
    submitBtn.disabled = true;               // prevents accidental double submit
    submitBtn.textContent = 'Creating your store…';
  });
  window.addEventListener('pageshow', () => {  // back button after an error
    submitBtn.disabled = false;
    submitBtn.textContent = 'Create My Store →';
  });

  refresh();
})();
