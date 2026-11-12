from app.services.auth import generate_invite_code, hash_invite_code


def test_invite_codes_are_high_entropy_and_unique():
    codes = {generate_invite_code() for _ in range(100)}
    assert len(codes) == 100
    assert all(len(code) >= 40 for code in codes)


def test_invite_hash_is_deterministic_and_does_not_equal_code():
    code = generate_invite_code()
    hashed = hash_invite_code(code)
    assert hashed == hash_invite_code(code)
    assert hashed != code
    assert len(hashed) == 64
    assert hash_invite_code(code + "x") != hashed
