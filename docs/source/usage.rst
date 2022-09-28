=============================
Starting and Using the Server
=============================

Starting the Server
===================

Single-User Mode
----------------

The server can configured to operate in single-user mode. The mode is useful
for demos and testing, but it could be used in small local deployments where no
true authorization is required. The single-user API key is be passed to
the server by setting an environment variable ``QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY``
or listed in server config files (``authentication/single_user_api_key``).
API key listed in config files overrides the key in environment variable.

.. note::

  Single-user mode is disabled if any providers are specified in the server
  config files even if single-user API key is passed to the server. Make supported
  that ``authentication/providers`` section is not included in the config files
  if single-user access is the desired mode of authorization.

Passing single-user API key by setting an environment variable
**************************************************************

An API key is an arbitrary non-empty string that consists of alphanumeric characters.
Substitute `<generated-api-key>` for the generated API key::

  QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY=<generated-api-key> uvicorn --host localhost --port 60610 bluesky_httpserver.server:app

Specifying single-user API key in configuration file
****************************************************

Alternatively, the single-user API key may be specified in the server config file.
It is considered unsafe practice to explicitly list API keys in config files, so
the purpose of this feature is mainly to customize the name of environment variable
used to pass the API key if the default name is inconvenient.

For example, the following config file (e.g. ``config.yml``) causes the server
to load single-user API key from the environment variable ``SU_API_KEY``::

    authentication:
      single_user_api_key: ${SU_API_KEY}

The environment variable must be set to the generated API key::

    export SU_API_KEY=<generated-api-key>

and the path to the config file passed to the server::

    QSERVER_HTTP_SERVER_CONFIG=config.yml uvicorn --host localhost --port 60610 bluesky_httpserver.server:app

Passing Configuration to the Server
-----------------------------------

In practical deployments, server configuration is represented as one or more YML files.
Path to the location of startup files is passed to the server using the environment variable
``QSERVER_HTTP_SERVER_CONFIG``. The path may point to a single file or a directory with multiple
files. If the variable is not set, then no configuration is loaded. The settings from in config files
override settings passed as environment variables. ::

    # 'config.yml' in the current working directory
    QSERVER_HTTP_SERVER_CONFIG=config.yml uvicorn --host localhost --port 60610 bluesky_httpserver.server:app

    # 'config.yml' in the directory '~/.config/qserver/http'
    QSERVER_HTTP_SERVER_CONFIG=~/.qserver/http/config.yml uvicorn --host localhost --port 60610 bluesky_httpserver.server:app

    # Multiple config files in the directory '~/.config/qserver/http'
    QSERVER_HTTP_SERVER_CONFIG=~/.config/qserver/http uvicorn --host localhost --port 60610 bluesky_httpserver.server:app

Enabling Anonymous Public Access
--------------------------------

Anonymous public access is disabled by default. It can be enabled by setting ``authentication/allow_anonymous_access``
in the server config file::

    authentication:
      allow_anonymous_access: True

Anonymous public access rules are applied when no API key or token is passed with API requests.
API calls with invalid token or API key are rejected even if public access is enabled.

Logging into the Server (Requesting Token)
==========================================

Users log into the server by calling ``/auth/provider/<provider-name>/token``, where ``<provider-name>``
should be substituted by the name of authentication provider. A user submits *username* and *password*
with the API request and gets access token and refresh token. The access token is used for authorization
of other API requests and the refresh token is used to request new access token when current token expires.

The server must be configured to have at least one active authentication provider. The server is shipped
with simple ``DictionaryAPIAccessControl`` authentication policy, which performs authentication based
on dictionary that maps usernames and passwords and intended for use in demos and testing. The following
is an example of a config file sets up ``DictionaryAPIAccessControl`` as a provider named ``toy``::

  authentication:
    providers:
      - provider: toy
        authenticator: bluesky_httpserver.authenticators:DictionaryAuthenticator
        args:
          users_to_passwords:
            bob: ${BOB_PASSWORD}
            alice: ${ALICE_PASSWORD}
            tom: ${TOM_PASSWORD}
  api_access:
    policy: bluesky_httpserver.authorization:DictionaryAPIAccessControl
    args:
      users:
        bob:
          roles:
            - admin
            - expert
        alice: 
          roles: user
        tom: 
          roles: observer

Generally it is not a good idea to explicitly list passwords in configuration files. Using environment
variables is more secure. The environment variable should be set before starting the server::

    export BOB_PASSWORD=bob_password
    export ALICE_PASSWORD=alice_password
    export TOM_PASSWORD=tom_password

Then users ``bob``, ``alice`` and ``tom`` can log into the server as ::

  http --form POST http://localhost:60610/api/auth/provider/toy/token username=bob password=bob_password

If authentication is successful, then the server returns access and refresh tokens.

Generating API Keys
===================

Users that are assigned the scope ``user:apikeys`` can generate API keys used for authorization
without logging into the server. API keys are often used for long-running applications or
autonomous agents. API keys carry information that allows the server to identify the user
who generated the key and the scopes that define access permissions. The scopes of an API key
may be a full set or a subset of user's scopes.

The API ``/auth/apikey`` accepts three parameters:

  - ``expires_in`` (int) - time until expiration of the API key in seconds;
  - ``scopes`` (option, list of strings) - list of scopes;
  - ``note`` (optional, string) - text message;

API keys may be generated using a valid token or an API key with the scope ``user:apikeys``.
If no ``scopes`` are specified in the request, then API *inherits* scopes of the user
(if authorized by token) or created using a copy of scopes of the original API key
(if authorized by API key). The *inherited* scopes change as user privileges change and
may be expanded if the user is given additional permissions. If the parameter ``scopes``
is used to pass a list of scopes, then the API key has a *fixed* set of scopes. API request
may never access API outside the listed scopes even if user privileges are extended.
If user privileges are reduced, some scopes may not be accessed even if they are listed.

The user generating API key must be permitted to use each scope listed in the request.
If the new key is generated based on the existing API key, each scope must also be
allowed for the existing API key. The request fails if any of the listed scopes is
not permitted.

Request API key that inherits the scopes of the user (principal) using an access token
(replace ``<token>`` with the token)::

    http POST http://localhost:60610/api/auth/apikey expires_in:=900 'Authorization: Bearer <token>'

Request API key with fixed set of scopes (scopes are a subset of the scopes of the principal)
using an access token::

    http POST http://localhost:60610/api/auth/apikey expires_in:=900 scopes:='["read:status", "user:apikeys"]' 'Authorization: Bearer <token>'

Request API key using an existing API key. The scopes for the new key are a copy of the scopes of
the existing key::

    http POST http://localhost:60610/api/auth/apikey expires_in:=900 'Authorization: ApiKey <apikey>'

Request API key with fixed set of scopes using an existing API key::

    http POST http://localhost:60610/api/auth/apikey expires_in:=900 scopes:='["read:status"]' 'Authorization: ApiKey <apikey>'


Using Tokens and API Keys in API Requests
=========================================

Administrative API
==================