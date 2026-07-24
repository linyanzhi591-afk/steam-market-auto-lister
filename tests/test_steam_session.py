from app.services.steam_session import extract_steam_id


def test_extract_steam_id_from_login_cookie() -> None:
    cookies = [{"name": "steamLoginSecure", "value": "76561198000000000%7C%7Csecret"}]
    assert extract_steam_id(cookies) == "76561198000000000"


def test_extract_steam_id_rejects_unrelated_cookie() -> None:
    assert extract_steam_id([{"name": "sessionid", "value": "secret"}]) is None


def test_extract_steam_id_rejects_malformed_value() -> None:
    cookies = [{"name": "steamLoginSecure", "value": "not-a-steam-id%7C%7Csecret"}]
    assert extract_steam_id(cookies) is None
