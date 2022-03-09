===============
Release History
===============

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
