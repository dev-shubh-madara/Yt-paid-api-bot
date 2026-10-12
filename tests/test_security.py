import random
import string

from app.security import hash_key, new_api_key, sign_token, verify_token


def test_token_roundtrip_and_tamper_detection():
    t = sign_token("sec", {"v": "abc", "e": 5})
    assert verify_token("sec", t) == {"v": "abc", "e": 5}
    assert verify_token("other", t) is None
    body, sig = t.split(".")
    assert verify_token("sec", body + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B")) is None
    assert verify_token("sec", "") is None and verify_token("sec", "x") is None


def test_tokens_never_contain_what_the_bot_greps_for():
    rnd = random.Random(7)
    alphabet = string.ascii_letters + string.digits + "_-"
    for _ in range(20000):
        vid = "".join(rnd.choice(alphabet) for _ in range(11))
        tok = sign_token("secret", {"v": vid, "k": "audio", "e": rnd.randrange(2**31), "i": rnd.randrange(999)})
        assert "_" not in tok
        assert verify_token("secret", tok)["v"] == vid
        assert not any(w in tok for w in ("vid_", "live_", "index_"))


def test_api_keys_are_unique_and_hash_is_stable():
    keys = {new_api_key() for _ in range(1000)}
    assert len(keys) == 1000
    k = next(iter(keys))
    assert hash_key(k) == hash_key(k) and hash_key(k) != hash_key(k + "x")
