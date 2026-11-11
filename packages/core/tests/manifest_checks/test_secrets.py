import pytest
from dta_core.manifest.checks.secrets import credential_name, literal_value, url_problems


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "DB_PASSWD",
        "api_key",
        "X-API-Key",
        "Authorization",
        "access_token",
        "sig",
        "key",
    ],
)
def test_credential_like_names(name):
    assert credential_name(name)


@pytest.mark.parametrize("name", ["author", "format", "page", "Content-Type", "monkey"])
def test_ordinary_names(name):
    assert not credential_name(name)


@pytest.mark.parametrize("value", ["${TOKEN}", "Bearer ${TOKEN}", " Basic ${B64} ", "${A}${B}", ""])
def test_placeholder_values_are_not_literal(value):
    assert not literal_value(value)


@pytest.mark.parametrize("value", ["abc123", "Bearer abc123", "abc${TOKEN}", "${TOKEN} extra"])
def test_written_out_values_are_literal(value):
    assert literal_value(value)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://${USER}:${PASSWORD}@${HOST}:5432/db",
        "postgresql://reader@db.example.org/db",
        "https://api.example.org/v1?api_key=${KEY}&page=2",
        "https://api.example.org/v1?author=alice",
        "data/*.csv",
        "${SOURCE_URL}",
    ],
)
def test_clean_addresses(url):
    assert url_problems(url) == []


def test_problems_are_named_without_their_values():
    problems = url_problems("https://u:hunter2@api.example.org/v1?token=s3cr3t&sig=abc&page=2")
    assert problems == [
        "a literal password in the URL userinfo",
        "a literal value for query parameter 'token'",
        "a literal value for query parameter 'sig'",
    ]
    assert not any(secret in " ".join(problems) for secret in ("hunter2", "s3cr3t", "abc"))


def test_unparseable_url_is_still_checked_for_a_password():
    assert url_problems("http://u:hunter2@[::1/x") == ["a literal password in the URL userinfo"]
    assert url_problems("http://u:${PW}@[::1/x") == []
