===============
Release History
===============

v0.0.5 (2022-06-24)
===================

Added
-----

- Support for custom routers. The list of routers is provided by environment variable ``QSERVER_HTTP_CUSTOM_ROUTERS``.
  A router is specified as ``<module_name>.<router_name>``. Multiple routers are separated by colon or comma, e.g.
  ``QSERVER_HTTP_CUSTOM_ROUTERS=module.one.router:module.two.router:module.three.router``. The server fails to start
  if loading of any of the listed routers fails.

- Initial implementation of basic authentication. The authentication API may change in future releases.

Changed
-------

- Implemented support for standard environment variable names. Old names are deprecated and will be removed in the future.
  The following environment variables are supported:

  - ``QSERVER_ZMQ_CONTROL_ADDRESS`` - address of the control socket of RE Manager;
  - ``QSERVER_ZMQ_INFO_ADDRESS`` - address of the socket used for publishing console output;
  - ``QSERVER_ZMQ_PUBLIC_KEY`` - public key for encrypted communication with RE Manager.

- The server is now started a ``uvicorn bluesky_httpserver.server:app --host localhost --port 60610``.

- Prefix ``/api`` is added to all REST API, e.g. ``/status`` is now ``/api/status``.

- Changed ``/queue/item/get``, ``/permissions/get``, ``/task/status`` and ``/task/result`` API from ``POST`` to ``GET``.


v0.0.4 (2022-04-05)
===================

Added
-----

- ``console_output_update`` endpoint for monitoring of console output in polling mode.


v0.0.3 (2022-03-08)
===================

Added
-----

* New API ``/re/runs``, which combines already existing ``/re/runs/active``, ``/re/runs/open``
  and ``/re/runs/closed``.

* New API: The following API were added: ``plans_existing``, ``devices_existing``,
  ``permissions_get``, ``permissions_set``, ``script_upload``, ``function_execute``,
  ``task_status``, ``task_result``.

* New API: ``/console_output`` and ``/console_output/uid`` for polling console output of
  RE Manager.

Changed
-------

* ``/plans/allowed`` API now accepts an optional parameter ``reduced``.
  If ``"reduced": True``, then the simplified version of plan descriptions
  that could be more convenient for web applications are returned, otherwise
  full descriptions are returned. Previously the API always returned
  simplified descriptions.

* HTTP Server is now using ``bluesky-queueserver-api`` for most of communication with
  RE Manager.


v0.0.2 (2021-10-06)
===================

Added
-----

* New ``stream_console_output`` API for streaming console output captured by RE Manager.
