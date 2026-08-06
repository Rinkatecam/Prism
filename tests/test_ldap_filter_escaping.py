"""LDAP filter injection guard for auth._check_user_allowed.

Found 2026-08-05 during a credential/injection audit. The function built four
LDAP filters by f-string interpolation with no escaping:

    (sAMAccountName={bare})                          <- from the login username
    (&(objectClass=group)(cn={allowed_bare}))        <- from allowed_users config
    (&(distinguishedName={user_dn})(memberOf:...))   <- from directory data

RFC 4515 metacharacters — ( ) * \\ NUL — change a filter's STRUCTURE rather than
being matched literally, so `*` alone turns an equality test into a wildcard.

Why it matters even though reachability is narrow: this function runs only after
a successful bind, so it is not an authentication bypass. It is the
AUTHORIZATION check — it decides whether an authenticated user satisfies
`allowed_users`. A filter that always matches promotes "anyone in the directory"
to "allowed into Prism".

These tests assert on the filter STRING handed to conn.search(), which is the
only place the defect was ever visible.
"""

from __future__ import annotations

import pytest

import auth


class _FakeEntry:
    def __init__(self, dn="CN=u,DC=ad,DC=example,DC=com"):
        self.distinguishedName = dn
        self.memberOf = None


class _FakeConn:
    """Records every filter passed to search(); returns no entries by default so
    _check_user_allowed walks the whole path without short-circuiting."""

    def __init__(self, entries_for=None):
        self.filters: list[str] = []
        self._entries_for = entries_for or (lambda f: [])
        self.entries: list = []

    def search(self, base_dn, search_filter, **kwargs):
        self.filters.append(search_filter)
        self.entries = self._entries_for(search_filter)
        return bool(self.entries)


BASE_DN = "DC=ad,DC=example,DC=com"


def _run(username, allowed, conn):
    return auth._check_user_allowed(conn, username, username, allowed, BASE_DN)


@pytest.mark.parametrize("payload", [
    "*",                          # bare wildcard -> matches every account
    "a*",                         # prefix wildcard
    "x)(objectClass=*",           # break out and OR in a match-all
    "x)(|(cn=*",                  # break out with an OR
    "x\\",                        # trailing escape char
    "(x)",                        # stray parens
])
def test_username_metacharacters_cannot_reshape_the_filter(payload):
    conn = _FakeConn()
    _run(payload, ["some-group"], conn)

    assert conn.filters, "the user search must have run"
    user_filter = conn.filters[0]

    # The raw metacharacters must not survive into the filter.
    inner = user_filter[len("(sAMAccountName="):-1]
    for ch in "()*":
        assert ch not in inner, (
            f"unescaped {ch!r} reached the filter: {user_filter!r}"
        )
    # Structure intact: exactly one attribute assertion.
    assert user_filter.startswith("(sAMAccountName=")
    assert user_filter.endswith(")")
    assert user_filter.count("(") == 1 and user_filter.count(")") == 1


def test_bare_wildcard_does_not_become_a_match_all():
    """The sharpest form: '*' must be matched literally, not as a wildcard."""
    conn = _FakeConn()
    _run("*", ["some-group"], conn)
    assert conn.filters[0] == "(sAMAccountName=\\2a)", conn.filters[0]


def test_ordinary_username_is_unchanged():
    """Escaping must not break the normal case."""
    conn = _FakeConn()
    _run("a.admin", ["some-group"], conn)
    assert conn.filters[0] == "(sAMAccountName=a.admin)"


def test_upn_and_domain_forms_still_reduce_to_the_bare_name():
    for form, expected in (("svc@ad.example.com", "svc"),
                           ("EXAMPLE\\svc", "svc")):
        conn = _FakeConn()
        _run(form, ["some-group"], conn)
        assert conn.filters[0] == f"(sAMAccountName={expected})", form


def test_allowed_group_name_is_escaped_in_the_group_lookup():
    """allowed_users is admin-supplied, but a CN with a paren must still not
    reshape the query."""
    conn = _FakeConn(entries_for=lambda f: [_FakeEntry()]
                     if f.startswith("(sAMAccountName=") else [])
    _run("someone", ["grp)(cn=*"], conn)

    group_filters = [f for f in conn.filters if "objectClass=group" in f]
    assert group_filters, "group lookup must have run"
    assert "*" not in group_filters[0], group_filters[0]
    assert group_filters[0].count("(") == 3, group_filters[0]  # (&(..)(..))


def test_direct_match_still_short_circuits_without_any_search():
    """A username already in allowed_users needs no directory query at all."""
    conn = _FakeConn()
    assert _run("a.admin", ["a.admin"], conn) is True
    assert conn.filters == [], "no LDAP search should be issued"
