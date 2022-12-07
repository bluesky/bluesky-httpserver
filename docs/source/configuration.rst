====================
Server Configuration
====================

.. currentmodule:: bluesky_httpserver

Configuration Using Environment Variables
-----------------------------------------

Basic configuration of the HTTP Server may be set using environment variables. For example,
single-user API key could be :ref:`set using the environment variable<passing_single_user_API_key_as_ev>`
``QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY``. While many environment variables are supported by the server
and allow high level of customization of functionality, using configuration YML files is more simple,
allows greater fexibility and is considered a preferable way of configuring the server in production
deployments.

Environment variable for passing the path to server configuration file(s):

- ``QSERVER_HTTP_SERVER_CONFIG`` - path to a single YML file or a directory with multiple YML files.

Environment variables for controlling 0MQ communication with Run Engine Manager:

- ``QSERVER_ZMQ_CONTROL_ADDRESS`` - 0MQ socket (REQ-REP) for control channel of RE Manager.

- ``QSERVER_ZMQ_INFO_ADDRESS`` -  0MQ socket (PUB-SUB) for console output of RE Manager.

- ``QSERVER_ZMQ_PUBLIC_KEY`` - public key for encryption of control API requests.

Environment variables for configuring authentication:

- ``QSERVER_HTTP_SERVER_SERVER_SECRET_KEYS`` - the value may be a single key or a ``;``-separated list of keys to
  support key rotation. The first key will be used for encryption. Each key will be tried in turn for decryption.

- ``QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY`` - Single-user API key. Enable single-user mode.

- ``QSERVER_HTTP_SERVER_ALLOW_ANONYMOUS_ACCESS`` - Enables public anonymous access if the expression evaluates ``True``.

- ``QSERVER_HTTP_SERVER_ALLOW_ORIGINS`` - the list of domains enables web apps served from other domains to make
  requests to the server.

Environment variables for controlling token and session lifetimes:

- ``QSERVER_HTTP_SERVER_ACCESS_TOKEN_MAX_AGE`` - maximum age of an access token

- ``QSERVER_HTTP_SERVER_REFRESH_TOKEN_MAX_AGE`` - maximum age of a refresh token

- ``QSERVER_HTTP_SERVER_SESSION_MAX_AGE`` - maximum age of a session

Environment variables for database configuration:

- ``QSERVER_HTTP_SERVER_DATABASE_URI`` - database URI. The default URI is *'sqlite:///bluesky_httpserver.sqlite'*.

- ``QSERVER_HTTP_SERVER_DATABASE_POOL_SIZE`` - connection pool size. Default is 5.

- ``QSERVER_HTTP_SERVER_DATABASE_POOL_PRE_PING`` - if true (default), use pessimistic connection testings.
  This is recommended.

Environment variables for customization of the server:

- ``QSERVER_HTTP_CUSTOM_ROUTERS`` - one or multiple custom routers (module names) separated with ``:`` or ``,``.

- ``QSERVER_CUSTOM_MODULES`` - THE FUNCTIONALITY WILL BE DEPRECATED IN FAVOR OF CUSTOM ROUTERS.


Configuration Files
-------------------

The preferable method for customizing HTTP server is using configuration YML files. The server
not attemting to load config files unless the path is passed to the server using environment
variable ``QSERVER_HTTP_SERVER_CONFIG`` as described in :ref:`passing_config_to_server`.
The path may point to a single config file or a directory containing multiple config files.
The settings in config file override any settings defined using environment variables.

Communication With Run Engine Manager
*************************************

HTTP Server is communicating with Run Engine (RE) Manager over 0MQ. The default server
configuration assumes that RE Manager is running on ``localhost`` and port 60615 is used
for control channel (REQ-REP), the console output is published using port 60625 (PUB-SUB),
and encryption for control channel is disabled. The default settings allow Queue Server to
run 'out of the box' if all system components are running on the same machine, which
is the case in testing and simple demos. In practical deployments the settings need to be
customized.

The configuration for 0MQ communication with RE Manager can be customized using environment
variables or configuration files.

The following environment variables are used to configure 0MQ communication settings:

- ``QSERVER_ZMQ_CONTROL_ADDRESS`` - the address of REQ-REP 0MQ socket of RE Manager used
  for control channel. The default address: ``tcp://localhost:60615``.

- ``QSERVER_ZMQ_INFO_ADDRESS`` - the address of PUB-SUB 0MQ socket used by RE Manager
  to publish console output. The default address: ``tcp://localhost:60625``.

- ``QSERVER_ZMQ_PUBLIC_KEY`` - the public key used for encryption of control messages
  sent to RE Manager over 0MQ. The encryption is disabled by default. To enable the encryption,
  generate the public/private key pair using
  `'qserver-zmq-keys' CLI tool <https://blueskyproject.io/bluesky-queueserver/cli_tools.html#qserver-zmq-keys>`_,
  pass the private key to RE Manager. Pass the public key to HTTP Server using
  ``QSERVER_ZMQ_PUBLIC_KEY`` environment variable.

The same parameters can be specified by including ``qserver_zmq_configuration`` into
the config file::

  qserver_zmq_configuration:
    control_address: tcp://localhost:60615
    info_address: tcp://localhost:60625
    public_key: ${PUBLIC_KEY}

All parameters in the config file are optional and override the values passed using
environment variables and the default values. The public key is typically passed using environment
variable ``QSERVER_ZMQ_PUBLIC_KEY``, but different environment variable name could be specified
in the config file as in the example above. Explicitly including public key in the config file
is considered unsafe practice in production systems.

Custom Routers
**************

The HTTP Server can be extended to support application-specific functionality by developing
Python modules with custom routers. The module names are passed to the server as ``:``-separated
list using the environment variable ``QSERVER_HTTP_CUSTOM_ROUTERS``::

  export QSERVER_HTTP_CUSTOM_ROUTERS modu.le1:mod.ule2

Alternatively, the list of modules can be specified in the configuration file::

  server_configuration:
    custom_routers:
      - modu.le1
      - mod.ule2

Authentication
**************

The server may be configured to run in single-user mode or multi-user mode. In nulti-user
mode the server is using one or more authentication providers to validate user login
data and allows users to obtain access tokens or API keys for authorization of requests.
Single-user and multi-user modes are mutually exclusive: activation of one or more
authentication providers automatically disables single-user mode.

In addition, the server supports autonomous public mode, which could be enabled in
the server configuration. The public mode can be activated for the server running in
single-user or multi-user mode.

Setting Secret Keys
+++++++++++++++++++

The server is using secret keys for authentication and authorization algorithms.
If the secret keys are not set in the configuration, the server generates random
secret keys upon startup and the existing tokens stop working after the restart
of the server. This is not acceptable in production deployments, therefore
the server configuration must contain secret keys that do not change between restarts.

The server configuration may contain multiple secret keys (a list of keys).
The first key in the list is used to encode tokens. The server attempts to decode
the received tokens by trying all secret keys.

One method to pass the keys is to set the environment variable ``QSERVER_HTTP_SERVER_SERVER_SECRET_KEYS``
with the string value of the key or multiple keys separated by ';'. Alternatively
the secret key can be set as part of *authentication* parameters in the server config file::

  authentication:
    secret_key:
      - ${SECRET_KEY_1}
      - ${SECRET_KEY_2}

It is considered unsafe to keep secret keys in text files, but instead use environment variables
to list the secret keys. In this example, the environment variables ``SECRET_KEY_1`` and
``SECRET_KEY_2`` must be set before starting the server.

Secure secret keys may be generated using ``openssl`` or by running a Python script from Linux
command line. Generating a secret key using ``openssl``::

  openssl rand -hex 32

Generating a secret key Python script::

  python -c "import secrets; print(secrets.token_hex(32))"

Single-User Mode
++++++++++++++++

Single-user access mode is activated by passing single-user API key to the server using
:ref:`environment variable<passing_single_user_API_key_as_ev>` or server config file
:ref:`configuration file<passing_single_user_API_key_in_config>`. The single-user
API key can be used for making API requests. The scopes for single-user access are
defined by API access policy (see :ref:`basic_api_access_policy` for details).

.. note::

    Single-user mode is disabled if any providers are listed in the server config file.

Anonymous Public Access
+++++++++++++++++++++++

Public access may enabled by setting ``authentication/allow_anonymous_access`` parameter
in the server config file (see :ref:`enabling_anonymous_public_access`). In most practical
deployment anonymous access is expected to be disabled or provide minimal monitoring
privileges.

Dictionary Authenticator
++++++++++++++++++++++++

Dictionary authenticator is recommended for server testing and demos. The authenticator
is receiving username-to-password mapping during initialization. The mapping is defined
as ``users_to_password`` argument define in server config file.

The following example shows server configured to use with ``DictionaryAuthenticator``
for user authentication (named as *'toy'* provider), ``DictionaryAPIAccessControl``
authorization policy and enabled public access::

    authentication:
      allow_anonymous_access: True
      providers:
        - provider: toy
          authenticator: bluesky_httpserver.authenticators:DictionaryAuthenticator
          args:
            users_to_passwords:
              bob: ${BOB_PASSWORD}
              alice: ${ALICE_PASSWORD}
              cara: ${CARA_PASSWORD}
    api_access:
      policy: bluesky_httpserver.authorization:DictionaryAPIAccessControl
      args:
        users:
          bob:
            roles:
              - admin
              - expert
          alice:
            roles: advanced
          cara:
            roles: user

The passwords are defined in the configuration as environment variable names,
which should be set before starting the server::

    export BOB_PASSWORD=bob_password
    export ALICE_PASSWORD=alice_password
    export CARA_PASSWORD=cara_password

The users *'bob'*, *'alice'* and *'cara'* should now be able to log into the server
and generate tokens and apikeys.

See the documentation on ``DictionaryAuthenticator`` for more details.

.. autosummary::
   :nosignatures:
   :toctree: generated

    authenticators.DictionaryAuthenticator

LDAP Authenticator
++++++++++++++++++

LDAP authenticator is designed for production deployments. The authenticator validates
user login information (username/password) by communicating with LDAP server (e.g. active
Directory server). The following example illustrates how to configure the server to
use demo OpenLDAP server running in docker container (run ``./start_LDAP.sh`` in the root
of the repository to start the server). The server is configured to authenticate
two users: *'user01'* and *'user02'* with passwords *'password1'* and *'password2'*
respectively. The configuration does not enable public access. ::

    authentication:
      providers:
        - provider: ldap
          authenticator: bluesky_httpserver.authenticators:LDAPAuthenticator
          args:
            server_address: localhost
            server_port: 1389
            use_tls: false
            use_ssl: false
    api_access:
      policy: bluesky_httpserver.authorization:DictionaryAPIAccessControl
      args:
        users:
          user01:
            roles:
              - admin
              - expert
          user02:
            roles: user

See the documentation on ``LDAPAuthenticator`` for more details.

.. autosummary::
   :nosignatures:
   :toctree: generated

    authenticators.LDAPAuthenticator


Expiration Time for Tokens and Sessions
+++++++++++++++++++++++++++++++++++++++

The server is using reasonable default values for lifetimes of the access token (15 minutes)
refresh token (7 days) and sessions (365 days). The default values may be changed in
configuration by setting authentication parameters ``access_token_max_age``,
``refresh_token_max_age`` and ``session_max_age``, which define maximum age of the respecitvely
items in seconds. For example, the following configuration sets maximum age of the tokens
and the session to 10, 3600 and 7200 seconds respectively::

    authentication:
      providers:
        <list one or more providers here ...>
      access_token_max_age: 10
      refresh_token_max_age: 3600
      session_max_age: 7200

Authorization: API Access
*************************

The HTTP Server is using API access policy to determine whether an authorized user
is allowed to call requested API. The API access policy associates user names with
one or several roles and the roles with allowed access scopes. If no policy is
selected in the configuration, the server is using ``BasicAPIAccessControl``, which
supports API access control for single-user and anonymous public access.
``DictionaryAPIAccessControl`` policy is a subclass of the basic policy that
accepts the fixed dictionary that maps user names to assigned roles as an argument
during initialization (arguments are defined in the config file) and serves as
a convenient tool for testing, demos and small local deployments.
More sophysticated policies based on ``BasicAPIAccessControl`` should be implemented
for production deployments, where user roles are stored on a secure server.

.. _basic_api_access_policy:

Basic API Access Policy
+++++++++++++++++++++++

``BasicAPIAccessControl`` is used by default if no API access policy is specified in
the config file. The policy supports access in single-user mode and anonymous public mode.
The policy defines two user names: ``UNAUTHENTICATED_SINGLE_USER`` and ``UNAUTHENTICATED_PUBLIC``
associated with ``unauthenticated_single_user`` and ``unauthenticated_public`` respecitvely.
The first user name is used to manage access for clients using single-user API key and
the second user name is used for access without API key or token (calls with an invalid
API key or a token always fail).

.. note::

    Basic policy does not allow any users to log into the server or use tokens and
    API keys except the single-user API key.

If the default scopes are not adequate, the roles can be customized in the config file.
For example, the following configuration adds ``read:console`` scope to
``unauthenticated_public`` role and removes ``write:scripts`` and ``user:apikeys``
scopes from ``unauthenticated_single_user`` role::

    api_access:
      policy: bluesky_httpserver.authorization:BasicAPIAccessControl
      args:
        roles:
          unauthenticated_public:
            scopes_add: read:console
          unauthenticated_single_user:
            scopes_remove:
              - write:scripts
              - user:apikeys

See the documentation on ``BasicAPIAccessControl`` for more details.

.. autosummary::
   :nosignatures:
   :toctree: generated

    authorization.BasicAPIAccessControl

Dictionary API Access Policy
++++++++++++++++++++++++++++

``DictionaryAPIAccessControl`` is a subclass of ``BasicAPIAccessControl`` and
provides five additional default roles ``admin``, ``expert``, ``advanced``,
``user`` and ``observer``. The roles are assigned reasonable default scopes,
but could be customized using ``roles`` argument, which is handled by the
base class. The instructions and examples of customization of roles may
be found in the documentation on ``BasicAPIAccessControl`` policy.

The mapping of user names to user information, including the assigned roles,
displayed name (optional) and email (optional) is passed to ``DictionaryAPIAccessControl``
using the argument ``users``. The optional information is used to generated
formatted displayed name (such as ``jdoe "Doe, John <jdoe@gmail.com>")``
required for some Run Engine Manager API calls. If no users are listed,
the policy behaves identically to ``BasicAPIAccessControl`` policy.

The following example illustrates how to configure ``DictionaryAPIAccessControl``
for two users (``bob`` and ``jdoe`` are login usernames)::

    api_access:
      policy: bluesky_httpserver.authorization:DictionaryAPIAccessControl
      args:
        users:
          bob:
            roles:
              - admin
              - expert
            email: bob@gmail.com
          jdoe:
            roles: advanced
            dislayed_name: Doe, John
            email: jdoe@gmail.com

See the documentation on ``DictionaryAPIAccessControl`` for more details.

.. autosummary::
   :nosignatures:
   :toctree: generated

    authorization.DictionaryAPIAccessControl

Access Policy Based on External Access Control Server
+++++++++++++++++++++++++++++++++++++++++++++++++++++

The policy is periodically loading user access data from the external
Access Control server. The server holds user access information obtained from other
services, such as Active Directory, and serves the purpose of reducing the load
on those services.

The following example shows configuration of ``ServerBasedAPIAccessControl``
to use hypothetical ``accesscontrol.server.com:60001`` as Access Control server.
The requests are sent with the average period of 300 seconds (+/- 20%). If the server
can not be contacted for 3600 seconds (1 hour), the access control data expires and
users lose access to the Queue Server. ::

    api_access:
      policy: bluesky_httpserver.authorization:ServerBasedAPIAccessControl
      args:
        instrument: srx
        server: accesscontrol.server.com
        port: 60001
        update_period: 300
        expiration_period: 3600
        roles:
          expert:
            scopes_remove:
              - write:scripts
              - write:permissions

See the documentation on ``ServerBasedAPIAccessControl`` for more details.

.. autosummary::
   :nosignatures:
   :toctree: generated

    authorization.ServerBasedAPIAccessControl

Authorization: Resource Access
******************************

Resource access policy is used to manage user access to resources, such as
plans and devices. The policy associates user name with the group name, which
is passed to Run Engine Manager in some API calls.

Default Resource Access Policy
++++++++++++++++++++++++++++++

Only the default policy ``DefaultResourceAccessControl`` is currently implemented.
This is a simple policy, which associates one fixed group name with all users.
The group name used by default is ``'primary'``. ``DefaultResourceAccessControl``
with default settings is activated by default if no other policy is selected
in the config file. While the default policy may look simplistic, it may suite
the needs of many production deployments, where user group permissions are
used not for access control, but for filtering out unwanted plans and device to
avoid clutter in the lists of allowed plans and devices (and GUI combo boxes
generated based on those lists).

The default group name can be changed in the policy configuration. For example,
the following policy configuration sets the returned group name to ``test_user``::

    resource_access:
      policy: bluesky_httpserver.authorization:DefaultResourceAccessControl
      args:
        default_group: test_user

See the documentation on ``DefaultResourceAccessControl`` for more details.

.. autosummary::
   :nosignatures:
   :toctree: generated

    authorization.DefaultResourceAccessControl
