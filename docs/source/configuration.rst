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

Authorization: API Access
*************************

BasicAPIAccessControl
+++++++++++++++++++++

If no API access policy is selected in the configuration, the server is using
``BasicAPIAccessControl`` policy with default settings. The policy supports access in
single-user mode and anonymous public mode. If the default permissions for those modes
are not adequate, the respective roles can be fully customized. Basic policy does not
allow any users to log into the server or use tokens and API keys except single-user
API key. ``BasicAPIAccessControl`` also manages scopes for multiple roles intended
for use in subclasses of the policy, such as ``DictionaryAPIAccessControl``.
See the documentation on ``BasicAPIAccessControl`` for more details.

.. autosummary::
   :nosignatures:
   :toctree: generated

    authorization.BasicAPIAccessControl


Authorization: Resource Access
******************************