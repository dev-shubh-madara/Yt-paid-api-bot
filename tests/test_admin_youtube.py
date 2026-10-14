"""Cookies and proxy set at runtime through /admin/youtube, no env vars."""

COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"


async def test_requires_admin_token(api):
    r = await api.http.get("/admin/youtube")
    assert r.status_code in (401, 403)


async def test_set_status_and_clear(api):
    h = api.admin
    r = await api.http.post("/admin/youtube/cookies", json={"cookies": COOKIES}, headers=h)
    assert r.status_code == 200 and r.json()["cookies"] == 1
    r = await api.http.post("/admin/youtube/cookies", json={"cookies": "x" * 40}, headers=h)
    assert r.status_code == 422
    old = api.st.http
    r = await api.http.post("/admin/youtube/proxy", json={"proxy": "http://u:secret@9.9.9.9:80"}, headers=h)
    assert r.status_code == 200 and "secret" not in r.text and api.st.http is not old
    s = (await api.http.get("/admin/youtube", headers=h)).json()
    assert s["cookies_loaded"] and s["proxy"] == "http://***@9.9.9.9:80"
    await api.http.post("/admin/youtube/proxy", json={"proxy": ""}, headers=h)
    await api.http.delete("/admin/youtube/cookies", headers=h)
    s = (await api.http.get("/admin/youtube", headers=h)).json()
    assert not s["cookies_loaded"] and s["proxy"] is None
