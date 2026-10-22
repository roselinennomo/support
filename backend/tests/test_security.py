from app.security import create_access_token, hash_password, verify_password


def test_password_hash_round_trip_and_wrong_password():
    encoded = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("incorrect password", encoded)
    assert not verify_password("correct horse battery staple", "malformed")


def test_access_token_is_created():
    token = create_access_token("user-123")

    assert isinstance(token, str)
    assert token
