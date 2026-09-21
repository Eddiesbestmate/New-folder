/* Klasser AI - billing: balance, packages, card, history and invoices. */

const alertBox = document.getElementById('alert');
const cardDialog = document.getElementById('card-dialog');
const buyDialog = document.getElementById('buy-dialog');
const cardAlert = document.getElementById('card-alert');
const buyAlert = document.getElementById('buy-alert');

let account = null;
let packages = [];
let pendingPackage = null;

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function money(cents) {
  return `$${(cents / 100).toFixed(2)}`;
}

function when(value) {
  if (!value) return '';
  return new Date(value).toLocaleDateString(undefined,
    { day: 'numeric', month: 'short', year: 'numeric' });
}

/* --- Balance --------------------------------------------------------------- */

function renderAccount(a) {
  account = a;

  document.getElementById('available').textContent = a.available;
  document.getElementById('run-cost').textContent =
    `${a.estimated_generation_cost} cr`;
  document.getElementById('runs-left').textContent =
    a.billing_mode === 'credits'
      ? `${a.generations_remaining} run${a.generations_remaining === 1 ? '' : 's'} funded`
      : `charged after each run`;

  const detail = [`${a.balance} total`];
  if (a.reserved) detail.push(`${a.reserved} held for runs in progress`);
  detail.push(`${a.lifetime_spent} spent all time`);
  document.getElementById('balance-detail').textContent = detail.join(' | ');

  // The meter is against the low-balance threshold, not the lifetime total:
  // what matters to a school is how close they are to running out.
  const ceiling = Math.max(a.low_balance_threshold * 2, a.available, 1);
  document.getElementById('meter-fill').style.width =
    `${Math.min(100, (a.available / ceiling) * 100)}%`;

  const low = document.getElementById('low-balance');
  low.hidden = !a.low_balance || a.billing_mode !== 'credits';
  if (!low.hidden) {
    low.textContent = `Your balance is below ${a.low_balance_threshold} credits.`
      + ` That is ${a.generations_remaining} more generation`
      + `${a.generations_remaining === 1 ? '' : 's'}.`;
  }

  const banner = document.getElementById('blocked-banner');
  banner.hidden = !a.account_blocked;
  if (a.account_blocked) {
    banner.innerHTML = `<strong>This account is blocked.</strong> `
      + escapeHtml(a.blocked_reason || 'An outstanding balance must be settled.')
      + (a.outstanding_cents
        ? ` ${money(a.outstanding_cents)} is owed - see Invoices.` : '');
  }
}

/* --- Packages -------------------------------------------------------------- */

function renderPackages(data) {
  packages = data.packages;

  const best = data.packages
    .filter((p) => p.available)
    .reduce((a, b) => (a && a.price_per_credit <= b.price_per_credit ? a : b), null);

  document.getElementById('packages').innerHTML = data.packages.map((p) => `
    <div class="pack ${best && p.name === best.name ? 'best' : ''}">
      <div style="font-weight:600">${escapeHtml(p.display_name)}</div>
      <div class="price">${p.credits} cr</div>
      <div class="rate">$${p.price_dollars.toFixed(2)}
        &middot; $${p.price_per_credit.toFixed(2)}/credit</div>
      ${p.once_per_school
        ? `<div class="small muted">One per school${p.expires_days
            ? `, expires after ${p.expires_days} days` : ''}</div>` : ''}
      ${p.available
        ? `<button class="btn btn-primary" data-buy="${escapeHtml(p.name)}"
                   type="button">Buy</button>`
        : `<button class="btn" type="button" disabled>Already used</button>`}
    </div>`).join('');

  document.getElementById('simulated-note').insertAdjacentHTML('beforeend',
    ` Pay as you go is $${data.payg_rate_dollars.toFixed(2)} per credit,`
    + ' charged after each generation.');

  document.querySelectorAll('[data-buy]').forEach((btn) => {
    btn.addEventListener('click', () => openBuy(btn.dataset.buy));
  });
}

function openBuy(name) {
  pendingPackage = packages.find((p) => p.name === name);
  if (!pendingPackage) return;
  buyAlert.hidden = true;
  document.getElementById('buy-title').textContent =
    `Buy ${pendingPackage.display_name}`;
  document.getElementById('buy-detail').innerHTML =
    `<strong>${pendingPackage.credits} credits</strong> for `
    + `$${pendingPackage.price_dollars.toFixed(2)}`
    + ` ($${pendingPackage.price_per_credit.toFixed(2)} per credit).`;
  buyDialog.showModal();
}

document.getElementById('buy-cancel').addEventListener('click',
  () => buyDialog.close());

document.getElementById('buy-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!pendingPackage) return;

  const submit = document.getElementById('buy-submit');
  const spinner = document.getElementById('buy-processing');
  submit.disabled = true;
  spinner.hidden = false;
  buyAlert.hidden = true;

  try {
    // The pause is the terminal animation from BILLING.md - it makes a
    // simulated charge feel like one rather than an instant number change.
    await new Promise((r) => setTimeout(r, 1200));
    const result = await apiCall('POST', '/billing/purchase',
      { package: pendingPackage.name });
    buyDialog.close();
    showAlert(alertBox,
      `${result.credits_added} credits added. Balance is now ${result.balance}.`,
      'success');
    await reload();
  } catch (err) {
    showAlert(buyAlert, err.message);
  } finally {
    submit.disabled = false;
    spinner.hidden = true;
  }
});

/* --- Billing mode ---------------------------------------------------------- */

const MODES = [
  ['credits', 'Prepaid credits',
    'Buy credits up front. Each generation deducts what it costs.'],
  ['payg', 'Pay as you go',
    'No balance to keep topped up. Your card is charged after each generation.'],
  ['invoiced', 'Invoiced monthly',
    'Generate now, receive one invoice a month. Needs approval first.'],
];

function renderModes() {
  document.getElementById('modes').innerHTML = MODES.map(([value, label, blurb]) => `
    <label class="mode-option ${account.billing_mode === value ? 'selected' : ''}">
      <input type="radio" name="mode" value="${value}"
             ${account.billing_mode === value ? 'checked' : ''}>
      <span>
        <span style="font-weight:600">${label}</span><br>
        <span class="small muted">${blurb}</span>
      </span>
    </label>`).join('');

  document.querySelectorAll('input[name="mode"]').forEach((input) => {
    input.addEventListener('change', async () => {
      try {
        await apiCall('PUT', '/billing/mode', { billing_mode: input.value });
        showAlert(alertBox, 'Billing mode updated.', 'success');
        await reload();
      } catch (err) {
        showAlert(alertBox, err.message);
        // Put the buttons back where they were, since the change did not stick.
        renderModes();
      }
    });
  });
}

/* --- Card ------------------------------------------------------------------ */

function renderCard() {
  const box = document.getElementById('card-state');
  const remove = document.getElementById('remove-card');

  if (!account.card) {
    box.innerHTML = '<p class="small muted">No card saved.</p>';
    remove.hidden = true;
    return;
  }

  box.innerHTML = `
    <div class="card-face">**** **** **** ${escapeHtml(account.card.last_four)}</div>
    <div class="small muted">${escapeHtml(account.card.brand)}
      &middot; expires ${escapeHtml(account.card.expires)}</div>`;
  remove.hidden = false;
}

document.getElementById('add-card').addEventListener('click', () => {
  cardAlert.hidden = true;
  cardDialog.showModal();
});

document.getElementById('card-cancel').addEventListener('click',
  () => cardDialog.close());

/* Format as the user types, the way a real terminal does. */
document.getElementById('card-number').addEventListener('input', (e) => {
  const digits = e.target.value.replace(/\D/g, '').slice(0, 16);
  e.target.value = digits.replace(/(.{4})/g, '$1 ').trim();
});

document.getElementById('card-expiry').addEventListener('input', (e) => {
  const digits = e.target.value.replace(/\D/g, '').slice(0, 4);
  e.target.value = digits.length > 2
    ? `${digits.slice(0, 2)}/${digits.slice(2)}` : digits;
});

document.getElementById('card-cvv').addEventListener('input', (e) => {
  e.target.value = e.target.value.replace(/\D/g, '').slice(0, 4);
});

document.getElementById('card-form').addEventListener('submit', async (e) => {
  e.preventDefault();

  const raw = ['card-number', 'card-expiry', 'card-cvv', 'card-name']
    .map((id) => document.getElementById(id).value.trim()).join('|');

  if (raw.replace(/\|/g, '') === '') {
    showAlert(cardAlert, 'Type something in at least one field.');
    return;
  }

  const submit = document.getElementById('card-submit');
  const spinner = document.getElementById('card-processing');
  submit.disabled = true;
  spinner.hidden = false;

  try {
    await new Promise((r) => setTimeout(r, 1500));
    const card = await apiCall('PUT', '/billing/card', { raw_input: raw });
    cardDialog.close();
    showAlert(alertBox,
      `${card.brand} ending ${card.last_four} saved.`, 'success');
    await reload();
  } catch (err) {
    showAlert(cardAlert, err.message);
  } finally {
    submit.disabled = false;
    spinner.hidden = true;
  }
});

document.getElementById('remove-card').addEventListener('click', async () => {
  if (!confirm('Remove the saved card?')) return;
  try {
    await apiCall('DELETE', '/billing/card');
    showAlert(alertBox, 'Card removed.', 'success');
    await reload();
  } catch (err) {
    showAlert(alertBox, err.message);
  }
});

/* --- History --------------------------------------------------------------- */

const TYPE_LABELS = {
  purchase: 'Purchase',
  generation: 'Generation',
  manual_adjustment: 'Adjustment',
  hold_released: 'Hold released',
  payg_charge: 'Pay as you go',
  refund: 'Refund',
};

function renderTransactions(rows) {
  if (!rows.length) {
    document.getElementById('transactions').innerHTML =
      '<p class="small muted">Nothing yet.</p>';
    return;
  }

  document.getElementById('transactions').innerHTML = `
    <table class="table">
      <thead><tr>
        <th>Date</th><th>Type</th><th>Detail</th>
        <th style="text-align:right">Credits</th>
        <th style="text-align:right">Balance</th>
      </tr></thead>
      <tbody>${rows.map((t) => `
        <tr>
          <td class="small">${when(t.created_at)}</td>
          <td>${escapeHtml(TYPE_LABELS[t.type] || t.type)}</td>
          <td class="small muted">
            ${escapeHtml(t.note || t.timetable_name || t.package_name || '')}
            ${t.price_paid_cents
              ? `<br>${money(t.price_paid_cents)} ${escapeHtml(t.payment_method || '')}`
              : ''}
          </td>
          <td style="text-align:right"
              class="txn-amount ${t.amount >= 0 ? 'pos' : 'neg'}">
            ${t.amount > 0 ? '+' : ''}${t.amount}
          </td>
          <td style="text-align:right">${t.balance_after}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

/* --- Invoices and debts ---------------------------------------------------- */

function renderInvoices(list, debts) {
  const note = document.getElementById('invoices-note');
  note.textContent = account.billing_mode === 'invoiced'
    ? 'Invoices are raised on the 1st of each month for the month before.'
    : 'Invoices only apply to schools on invoiced billing.';

  document.getElementById('invoices').innerHTML = list.length ? `
    <table class="table">
      <thead><tr>
        <th>Invoice</th><th>Period</th><th>Due</th><th>Status</th>
        <th style="text-align:right">Amount</th>
      </tr></thead>
      <tbody>${list.map((i) => `
        <tr>
          <td>${escapeHtml(i.invoice_number)}</td>
          <td class="small">${when(i.period_start)} - ${when(i.period_end)}</td>
          <td class="small">${when(i.due_date)}</td>
          <td><span class="pill">${escapeHtml(i.status)}${
            i.days_overdue > 0 ? ` ${i.days_overdue}d` : ''}</span></td>
          <td style="text-align:right">${money(i.total_cents)}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '<p class="small muted">No invoices.</p>';

  const block = document.getElementById('outstanding-block');
  block.hidden = !debts.charges.length;
  if (block.hidden) return;

  document.getElementById('outstanding').innerHTML = `
    <p class="small">
      ${money(debts.total_owed_cents)} outstanding across
      ${debts.charges.length} charge${debts.charges.length === 1 ? '' : 's'}.
      Interest accrues daily until it is paid.
    </p>
    <table class="table">
      <thead><tr>
        <th>Charge</th><th>Days</th>
        <th style="text-align:right">Original</th>
        <th style="text-align:right">Interest</th>
        <th style="text-align:right">Owed</th>
      </tr></thead>
      <tbody>${debts.charges.map((c) => `
        <tr>
          <td class="small">
            ${c.charge_type === 'payg_failed' ? 'Declined card' : 'Overdue invoice'}
            ${c.timetable_name ? `<br><span class="muted">${escapeHtml(c.timetable_name)}</span>` : ''}
            ${c.invoice_number ? `<br><span class="muted">${escapeHtml(c.invoice_number)}</span>` : ''}
          </td>
          <td>${c.days_outstanding}</td>
          <td style="text-align:right">${money(c.original_amount_cents)}</td>
          <td style="text-align:right">${money(c.interest_accrued_cents)}</td>
          <td style="text-align:right"><strong>${money(c.total_owed_cents)}</strong></td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

/* --- Tabs ------------------------------------------------------------------ */

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) =>
      t.classList.toggle('active', t === tab));
    ['buy', 'method', 'history', 'invoices'].forEach((name) => {
      document.getElementById(`panel-${name}`).hidden = name !== tab.dataset.tab;
    });
  });
});

/* --- Load ------------------------------------------------------------------ */

async function reload() {
  const [acct, packs, txns, invoiceList, debts] = await Promise.all([
    apiCall('GET', '/billing/account'),
    apiCall('GET', '/billing/packages'),
    apiCall('GET', '/billing/transactions'),
    apiCall('GET', '/billing/invoices'),
    apiCall('GET', '/billing/outstanding'),
  ]);

  renderAccount(acct);
  renderPackages(packs);
  renderModes();
  renderCard();
  renderTransactions(txns);
  renderInvoices(invoiceList, debts);
}

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  await reload();

  // Only an owner can spend money. Staff still see the balance, because
  // knowing whether a run is affordable is part of using the product.
  if (me.role !== 'owner') {
    document.querySelectorAll('#panel-buy button, #panel-method button,'
      + ' #panel-method input').forEach((el) => { el.disabled = true; });
    document.getElementById('simulated-note').insertAdjacentHTML('afterend',
      '<p class="small muted">Only the account owner can buy credits or'
      + ' change the payment method.</p>');
  }

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
