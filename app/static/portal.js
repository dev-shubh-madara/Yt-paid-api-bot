(function () {
  var me = document.currentScript;
  var pk = me.dataset.pk, domain = me.dataset.domain;
  var $ = function (id) { return document.getElementById(id); };
  var state = { plans: null, account: null, order: null, timer: null };

  function msg(text, bad) { var m = $("msg"); m.textContent = text || ""; m.className = bad ? "bad" : "ok"; }
  function inr(n) { return "₹" + Number(n).toLocaleString("en-IN"); }
  function when(iso) { try { return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }); } catch (e) { return iso; } }

  async function api(method, path, body) {
    var headers = { "Content-Type": "application/json" };
    if (window.Clerk && window.Clerk.session) headers.Authorization = "Bearer " + (await window.Clerk.session.getToken());
    var r = await fetch(path, { method: method, headers: headers, body: body === undefined ? undefined : JSON.stringify(body) });
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(j.message || "Something went wrong (" + r.status + ")");
    return j;
  }
  function copy(text, what) {
    navigator.clipboard.writeText(text).then(function () { msg(what + " copied.", false); }, function () { msg("Could not copy. Select it and copy by hand.", true); });
  }

  // ── account + plans ───────────────────────────────────────────────────
  async function refresh() {
    var acc = await api("GET", "/v1/portal/me");
    state.account = acc;
    $("who").textContent = acc.email;
    $("nokey").hidden = acc.has_key;
    $("haskey").hidden = !acc.has_key;
    $("plansec").hidden = false;
    if (acc.has_key) {
      var u = acc.usage || {}, k = acc.key || {};
      $("planchip").textContent = (u.plan_name || u.plan || "free");
      var pct = u.daily_limit ? Math.min(100, Math.round(100 * u.used_today / u.daily_limit)) : 0;
      $("meterfill").style.width = pct + "%";
      $("meterfill").style.background = pct > 90 ? "var(--bad)" : "var(--ok)";
      $("usage").textContent = u.used_today + " of " + u.daily_limit + " songs used today";
      $("expiry").textContent = k.plan_expires_at ? "Plan valid until " + when(k.plan_expires_at) : "Free plan";
    }
    renderPlans();
  }

  function renderPlans() {
    var box = $("plans"); box.textContent = "";
    var p = state.plans, acc = state.account;
    if (!p) return;
    var cur = acc && acc.key ? acc.key.plan : "free";
    p.paid.forEach(function (pl) {
      var d = document.createElement("div"); d.className = "plan" + (pl.id === cur ? " cur" : "");
      var name = document.createElement("span"); name.textContent = pl.name;
      var price = document.createElement("b"); price.textContent = inr(pl.price_inr);
      var lim = document.createElement("span"); lim.className = "lim"; lim.textContent = pl.daily_limit.toLocaleString("en-IN") + " songs/day · " + p.plan_duration_days + " days";
      var b = document.createElement("button");
      b.textContent = pl.id === cur ? "Renew" : "Buy";
      b.disabled = !acc || !acc.has_key || !acc.payments_enabled;
      b.onclick = function () { buy(pl, b); };
      d.append(name, price, lim, b); box.appendChild(d);
    });
    var note = "";
    if (acc && !acc.has_key) note = "Get your key first, then you can buy a plan.";
    else if (acc && !acc.payments_enabled) note = "Buying is not switched on yet. Please contact the owner.";
    else note = "Pay with any UPI app. Time is added on top if you renew the same plan.";
    $("paynote").textContent = note;
  }

  // ── checkout ──────────────────────────────────────────────────────────
  function stopPolling() { if (state.timer) { clearTimeout(state.timer); state.timer = null; } }
  function closeCheckout() { stopPolling(); state.order = null; $("checkout").hidden = true; }

  function showOrder(o) {
    state.order = o;
    var box = $("checkout"); box.hidden = false;
    var plan = state.plans.paid.filter(function (x) { return x.id === o.plan; })[0];
    $("cotitle").textContent = "Pay for " + (plan ? plan.name : o.plan);
    var st = $("costatus");
    if (o.status === "paid") {
      stopPolling();
      st.textContent = "✅ Payment received. Your plan is active!";
      box.querySelectorAll("img,a,dl,p.small,details").forEach(function (e) { e.hidden = true; });
      refresh().catch(function () {});
      return;
    }
    box.querySelectorAll("img,a,dl,p.small,details").forEach(function (e) { e.hidden = false; });
    if (o.status === "expired") { stopPolling(); st.textContent = "This order expired. Close this and buy again."; return; }
    var pay = o.pay || {};
    $("qr").src = pay.qr || "";
    $("qr").hidden = !pay.qr;
    $("upilink").href = pay.upi_uri || "#";
    $("upiid").textContent = pay.upi_id || "";
    $("amount").textContent = inr(pay.amount_inr);
    $("code").textContent = pay.note || o.code;
    st.textContent = o.message || "Waiting for your payment…";
    poll();
  }

  function poll() {
    stopPolling();
    var id = state.order && state.order.order_id;
    if (!id) return;
    state.timer = setTimeout(async function () {
      try {
        var o = await api("GET", "/v1/portal/orders/" + encodeURIComponent(id));
        if (!state.order || state.order.order_id !== id) return;
        if (o.status === "pending") { state.order = o; poll(); } else { showOrder(o); }
      } catch (e) { if (state.order && state.order.order_id === id) poll(); }
    }, 5000);
  }

  async function buy(pl, btn) {
    btn.disabled = true; msg("", false);
    try {
      showOrder(await api("POST", "/v1/portal/orders", { plan: pl.id }));
      $("checkout").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) { msg(e.message, true); }
    btn.disabled = false;
  }

  // ── key actions ───────────────────────────────────────────────────────
  function showKey(j) {
    $("key").textContent = j.api_key; $("keybox").hidden = false;
    msg(j.warning || "Copy your key now.", false);
  }
  async function keyAction(path) {
    try { showKey(await api("POST", path, {})); await refresh(); } catch (e) { msg(e.message, true); }
  }

  function wire() {
    $("create").onclick = function () { keyAction("/v1/keys"); };
    $("rotate").onclick = function () { if (confirm("Your old key stops working immediately. Continue?")) keyAction("/v1/keys/rotate"); };
    $("signout").onclick = function () { closeCheckout(); window.Clerk.signOut(); };
    $("signin").onclick = function () { window.Clerk.openSignIn({}); };
    $("copy").onclick = function () { copy($("key").textContent, "Key"); };
    $("copyupi").onclick = function () { copy($("upiid").textContent, "UPI ID"); };
    $("copycode").onclick = function () { copy($("code").textContent, "Note"); };
    $("coclose").onclick = closeCheckout;
    $("sendutr").onclick = async function () {
      var utr = $("utr").value.trim();
      if (!state.order) return;
      try { showOrder(await api("POST", "/v1/portal/orders/" + encodeURIComponent(state.order.order_id) + "/claim", { utr: utr })); msg("", false); }
      catch (e) { msg(e.message, true); }
    };
  }

  async function onAuth() {
    var signed = !!window.Clerk.user;
    $("signedout").hidden = signed; $("app").hidden = !signed;
    var uid = signed ? window.Clerk.user.id : null;
    if (uid === state.uid && (state.account || !signed)) return;  // Clerk fires often; only reload on a real change
    state.uid = uid;
    if (!signed) { state.account = null; closeCheckout(); return; }
    try { await refresh(); } catch (e) { msg(e.message, true); }
  }

  fetch("/v1/plans").then(function (r) { return r.json(); }).then(function (p) {
    state.plans = p; $("freelimit").textContent = p.free.daily_limit; renderPlans();
  }).catch(function () {});

  var s = document.createElement("script");
  s.src = "https://" + domain + "/npm/@clerk/clerk-js@5/dist/clerk.browser.js";
  s.async = true; s.crossOrigin = "anonymous";
  s.setAttribute("data-clerk-publishable-key", pk);
  s.onload = async function () {
    await window.Clerk.load();
    wire();
    window.Clerk.addListener(function () { onAuth(); });
    onAuth();
  };
  s.onerror = function () { msg("Could not load the login service.", true); };
  document.head.appendChild(s);
})();
