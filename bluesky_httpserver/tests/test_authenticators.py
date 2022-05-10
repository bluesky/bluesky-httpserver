import pytest

from ..authenticators import LDAPAuthenticator


@pytest.mark.parametrize("use_tls,use_ssl", [(False, False)])
def test_LDAPAuthenticator_01(use_tls, use_ssl):
    """
    Basic test for ``LDAPAuthenticator``.

    TODO: The test could be extended with enabled TLS or SSL, but it requires configuration
    of the LDAP server.
    """
    authenticator = LDAPAuthenticator(
        "localhost",
        1389,
        bind_dn_template="cn={username},ou=users,dc=example,dc=org",
        use_tls=use_tls,
        use_ssl=use_ssl,
    )
    assert authenticator.authenticate("user01", "password1") == "user01"
    assert authenticator.authenticate("user02", "password2") == "user02"
    assert authenticator.authenticate("user02a", "password2") is None
    assert authenticator.authenticate("user02", "password2a") is None
