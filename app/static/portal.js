(function () {
  var me = document.currentScript;
  var pk = me.dataset.pk, domain = me.dataset.domain;
  var $ = function (id) { return document.getElementById(id); };
  function msg(text, bad) { var m = $("msg"); m.textContent = text; m.className = bad ? "bad" : "ok"; }

  async function call(path) {
    var token = await window.Clerk.session.getToken();
    var r = await fetch(path, { method: "POST", headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" }, body: "{}" });
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(j.message || "Something went wrong (" + r.status + ")");
    return j;
  }
  function showKey(j) {
    $("key").textContent = j.api_key;
    $("keybox").hidden = false;
    msg(j.warning || "Copy your key now.", false);
  }
  async function act(path) {
    try { showKey(await call(path)); } catch (e) { msg(e.message, true); }
  }

  var s = document.createElement("script");
  s.src = "https://" + domain + "/npm/@clerk/clerk-js@5/dist/clerk.browser.js";
  s.async = true; s.crossOrigin = "anonymous";
  s.setAttribute("data-clerk-publishable-key", pk);
  s.onload = async function () {
    await window.Clerk.load();
    function render() {
      var signed = !!window.Clerk.user;
      $("signin").hidden = signed; $("actions").hidden = !signed;
      if (signed) { $("who").textContent = window.Clerk.user.primaryEmailAddress ? window.Clerk.user.primaryEmailAddress.emailAddress : ""; }
    }
    window.Clerk.addListener(render); render();
    $("signin").onclick = function () { window.Clerk.openSignIn({}); };
    $("create").onclick = function () { act("/v1/keys"); };
    $("rotate").onclick = function () { if (confirm("Your old key stops working immediately. Continue?")) act("/v1/keys/rotate"); };
    $("signout").onclick = function () { window.Clerk.signOut(); };
    $("copy").onclick = function () { navigator.clipboard.writeText($("key").textContent); msg("Copied.", false); };
  };
  s.onerror = function () { msg("Could not load the login service.", true); };
  document.head.appendChild(s);
})();
