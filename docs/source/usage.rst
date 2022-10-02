=============================
Starting and Using the Server
=============================

The examples illustrate how to access API of the Bluesky HTTP Server using
``httpie`` command-line tool. Though it is unlikely that ``httpie`` is ever used
to control the server in practical deployments, the instructions could be useful for application developers
for testing the server and understanding how the API work.

Installation instructions for ``httpie``: `<https://httpie.io/docs/cli/installation>`_.

In the examples it is assumed that the server address is ``localhost`` and the server
port is ``60610``. The address and the port are used by default by Bluesky Queue Server
components and should be substituted by custom address and/or port if necessary.

.. _starting_http_server:

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

.. _passing_single_user_API_key_as_ev:

Passing single-user API key by setting an environment variable
**************************************************************

An API key is an arbitrary non-empty string that consists of alphanumeric characters.
Substitute `<generated-api-key>` for the generated API key::

  QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY=<generated-api-key> uvicorn --host localhost --port 60610 bluesky_httpserver.server:app

.. _passing_single_user_API_key_in_config:

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

.. _passing_config_to_server:

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

.. _enabling_anonymous_public_access:

Enabling Anonymous Public Access
--------------------------------

Anonymous public access is disabled by default. It can be enabled by setting ``authentication/allow_anonymous_access``
in the server config file::

    authentication:
      allow_anonymous_access: True

Anonymous public access rules are applied when no API key or token is passed with API requests.
API calls with invalid token or API key are rejected even if public access is enabled.

Authentication API for Users
============================

Logging into the Server (Requesting Token)
------------------------------------------

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
-------------------

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


Verifying Scopes of Access Tokens and API Keys
----------------------------------------------

User can verify currently permissions for a token or API key at any time by sending ``/auth/scopes`` request.
The API returns the list of assigned roles and the list of scopes applied to the token or the API key::

  # Get scopes for the access token
  http GET http://localhost:60610/api/auth/scopes 'Authorization: Bearer <token>'
  # Get scopes for the API key
  http GET http://localhost:60610/api/auth/scopes 'Authorization: ApiKey <api-key>'


Getting Information on API Key
------------------------------

Information on an existing API key may be obtained calling ``/auth/apikey`` (GET) API and using
the API key for authentication::

  http GET http://localhost:60610/api/auth/apikey 'Authorization: ApiKey <apikey>'


Deleting API Key
----------------

API key may be deleted by an authenticated user by calling ``/auth/apikey`` (DELETE). The API key
used for authorization of the API request can also be deleted::

  # Authorization using token
  http DELETE http://localhost:60610/api/auth/apikey first_eight==<first-eight-chars-of-key> 'Authorization: Bearer <token>'
  # Authorization using API key
  http DELETE http://localhost:60610/api/auth/apikey first_eight==<first-eight-chars-of-key> 'Authorization: ApiKey <api-key>'


Refreshing Sessions
-------------------

Refresh token returned by ``/auth/apikey`` can be used to obtain replacement access tokens by calling
``/auth/session/refresh`` API::

  http POST http://localhost:60610/api/auth/session/refresh refresh_token=<refresh-token>


whoami
------

An access token or an API key may be used to obtain full information about the user, including
principal identities and open sessions by calling ``/auth/whoami`` API::

  # 'whoami' using the access token
  http GET http://localhost:60610/api/auth/scopes 'Authorization: Bearer <token>'
  # 'whoami' using the API key
  http GET http://localhost:60610/api/auth/scopes 'Authorization: ApiKey <api-key>'


Revoking Sessions
-----------------

Authenticated user may revoke any of the open sessions using session UUID. The list of sessions
is returned by ``/auth/whoami`` API. Revoking the session invalidates the respective refresh token.
Access tokens and API keys will continue working until expiration. ::

  # Revoke session using access token
  http DELETE http://localhost:60610/api/auth/session/revoke/<full-session-uid>  'Authorization: Bearer <token>'
  # Revoke session using API key
  http DELETE http://localhost:60610/api/auth/session/revoke/<full-session-uid>  'Authorization: ApiKey <api-key>'

.. _passing_tokens_and_api_keys_with_api_requests:

Passing Tokens and API Keys in API Requests
-------------------------------------------

Generated access tokens or API keys can be used for authorization in API requests.
``/status`` API returns the status of RE Manager::

  # Get scopes for the access token
  http GET http://localhost:60610/api/status 'Authorization: Bearer <token>'
  # Get scopes for the API key
  http GET http://localhost:60610/api/status 'Authorization: ApiKey <api-key>'


Logging Out of the Server
-------------------------

The API ``/auth/logout`` is not changing the state of the server and returns ``{}`` (empty
dictionary). The purpose of the API is to delete any tokens or API keys stored locally by
the browser. The API request does not require authentication::

  http POST http://localhost:60610/api/logout


Administrative API
==================

Some API are available only to clients with administrative permissons
(scope ``admin:read:principals`` and/or ``admin:apikeys``).


Getting Information on All Principals
-------------------------------------

Clients with ``admin:read:principals`` may get information on all active principals using
``/auth/principal`` API. The API is similar to ``/auth/whoami``, but instead of returning
a single item with information on authorized principal it returns the list of items
for all principal::

  # Get information on all principals using token with admin privileges
  http GET http://localhost:60610/api/auth/principal 'Authorization: Bearer <token>'
  # Get information on all principals using API key with admin privileges
  http GET http://localhost:60610/api/auth/principal 'Authorization: ApiKey <api-key>'


Getting Information on a Selected Principal
-------------------------------------------

Clients with ``admin:read:principals`` may get information on any principals
using ``/auth/principal/<principal-UUID>`` API.
The principals are identified by UUID. The returned data is structured
identically as the data returned by ``/auth/whoami``, but may represent any
user of the server, not only the authorized user::

  # Get information on a selected principal using token with admin privileges
  http GET http://localhost:60610/api/auth/principal/<principal-UUID> 'Authorization: Bearer <token>'
  # Get information on all principals using API key with admin privileges
  http GET http://localhost:60610/api/auth/principal/<principal-UUID> 'Authorization: ApiKey <api-key>'


Generating an API Key for a Principal
-------------------------------------

Clients with ``admin:apikeys`` scope may generate API keys for any principal in the system
using ``/auth/principal/<principal-UUID>/apikey`` API.
The scopes for the generated API key are limited by permissions assigned to the principal
(not the client sending the request). The API works similarly to ``/auth/apikey``
and accepts identical set of parameters: ``expires_in`` is a required parameter
representing API key expiration time in seconds, ``scopes`` and ``note`` are optional parameters.
The API call must be authorized using a token or an API key of the client with administrative
privileges. ::

  # Generate API key for a given selected principal using token with admin privileges
  http POST http://localhost:60610/api/auth/principal/<principal-UUID>/apikey expires_in:=900 'Authorization: Bearer <token>'
  # Generate API key for a given selected principal using API key with admin privileges
  http POST http://localhost:60610/api/auth/principal/<principal-UUID>/apikey expires_in:=900 'Authorization: ApiKey <api-key>'
