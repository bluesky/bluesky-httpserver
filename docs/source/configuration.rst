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

(Include reference to the supported environment variables)

Configuration Files
-------------------

The preferable method for customizing HTTP server is using configuration YML files. The server
not attemting to load config files unless the path is passed to the server using environment
variable ``QSERVER_HTTP_SERVER_CONFIG`` as described in :ref:`passing_config_to_server`.
The path may point to a single config file or a directory containing multiple config files.
The settings in config file override any settings defined using environment variables.

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

Single-User Mode
++++++++++++++++

Single-user access mode is activated by passing single-user API key to the server using
:ref:`environment variable<passing_single_user_API_key_as_ev>` or server config file
:ref:`configuration file<passing_single_user_API_key_in_config>`. The single-user
API key can be used for making API requests. The scopes for single-user access are
defined by API access policy (see :ref:`basic_api_access_policy` for details).

.. note::

    Single-user mode is disabled if any providers are listed in the server config file.

Public Access
+++++++++++++

Dictionary Authenticator
++++++++++++++++++++++++

LDAP Authenticator
++++++++++++++++++

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
            scipes_remove:
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
            mail: bob@gmail.com
          jdoe:
            roles: advanced
            dislayed_name: Doe, John
            mail: jdoe@gmail.com

See the documentation on ``DictionaryAPIAccessControl`` for more details.

.. autosummary::
   :nosignatures:
   :toctree: generated

    authorization.DictionaryAPIAccessControl

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
