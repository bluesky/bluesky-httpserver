==================
bluesky-httpserver
==================

.. image:: https://img.shields.io/pypi/v/bluesky-httpserver.svg
        :target: https://pypi.python.org/pypi/bluesky-httpserver

.. image:: https://img.shields.io/conda/vn/conda-forge/bluesky-httpserver
        :target: https://anaconda.org/conda-forge/bluesky-httpserver

..
  .. image:: https://img.shields.io/codecov/c/github/bluesky/bluesky-httpserver
          :target: https://codecov.io/gh/bluesky/bluesky-httpserver

.. image:: https://img.shields.io/github/commits-since/bluesky/bluesky-httpserver/latest
        :target: https://github.com/bluesky/bluesky-httpserver

.. image:: https://img.shields.io/pypi/dm/bluesky-httpserver?label=PyPI%20downloads
        :target: https://pypi.python.org/pypi/bluesky-httpserver

.. image:: https://img.shields.io/conda/dn/conda-forge/bluesky-httpserver?label=Conda-Forge%20downloads
        :target: https://anaconda.org/conda-forge/bluesky-httpserver


HTTP Server that provides REST API for accessing Queue Server.

* Free software: 3-clause BSD license
* `Installation instructions <https://bluesky.github.io/bluesky-httpserver/installation.html>`_.
* `Brief description of the project <https://bluesky.github.io/bluesky-httpserver/introduction.html>`_.
* `Full documentation <https://bluesky.github.io/bluesky-httpserver>`_.
* `'bluesky-queueserver': HTTP Server providing REST API for Queue Server <https://bluesky.github.io/bluesky-queueserver>`_.
* `'bluesky-queueserver-api': Python API for Queue Server <https://bluesky.github.io/bluesky-queueserver-api>`_.


THE FOLLOWING IS A COPY OF THE ORIGINAL README.RST FROM BLUESKY-QUEUESERVER PROJECT. PROPER DOCUMENTATION
FOR BLUESKY-HTTPSERVER IS COMING SOON.

Features
--------

This is demo version of the QueueServer. The project is in the process of active development, so
APIs may change at any time without notice. QueueServer may not be considered stable, so install
and use it only for evaluation purposes.

QueueServer is supporting the following functions:


- Opening, closing and destroying of RE (Run Engine) Worker environment.

- Loading and publishing the lists of allowed plans and devices.

- Loading beamlines' startup files from the corresponding ``profile_collection`` repositories.

- Adding and removing plans from the queue; rearranging plans in the queue.

- Starting/stopping execution of the queue.

- Control of the running plans: pausing (immediate and deferred), resuming and stopping
  (stop, abort and halt) the running plan.

- Saving data to Databroker.

- Streaming documents via Kafka.


In some cases the program may crash and leave some sockets open. This may prevent the Manager from
restarting. To close the sockets (we are interested in sockets on ports 60615 and 60610), find
PIDs of the processes::

  $ netstat -ltnp

and then kill the processes::

  $ kill -9 <pid>


Installation
------------

The latest released version of HTTP Server may be installed from PyPI::

  pip install bluesky-httpserver

This will also install the released version of `bluesky-queueserver`. The Queue Server is
using Redis to store and manage the queue. See the Queue Server documentation for the instructions
on Redis installation (https://blueskyproject.io/bluesky-queueserver/installation.html).

Alternatively `bluesky-queueserver` and `bluesky-httpserver` may be installed from source
from the respective GitHub repositories.

Starting QueueServer
--------------------

The RE Manager and Web Server are running as two separate applications. To run the demo you will need to open
three shells: the first for RE Manager, the second for Web Server and the third to send HTTP requests to
the server.

In the first shell start RE Manager::

  start-re-manager

RE Manager supports a number of command line options. Use 'start-re-manager -h' to view
the available options.

The Web Server should be started from the second shell as follows::

  uvicorn bluesky_httpserver.server:app --host localhost --port 60610

The Web Server connects to RE Manager using Zero MQ. The default ZMQ address is 'tcp://localhost:60615'.
A different ZMQ address may be passed to the Web Server by setting the *QSERVER_ZMQ_ADDRESS_CONTROL*
environment variable before starting the server::

  export QSERVER_ZMQ_ADDRESS_CONTROL='tcp://localhost:60615'

The Web Server supports using external modules for processing some requests. Those modules
are optional and may contain custom instrument-specific processing code. The name of the external
modules may be passed to HTTP server by setting **QSERVER_CUSTOM_MODULES** environment
variable::

  QSERVER_CUSTOM_MODULES=<name-of-external-module> uvicorn bluesky_queueserver.server:app --host localhost --port 60610

The value of the environment variable is a string containing a comma or column-separated list of
module names. The first module that contains the required functions is selected and used by the server.
If the module name contains '-' (dash) characters, they will be automatically converted to '_'
(underscore) characters. If the server fails to load custom external module, the server
will support only default functionality and may reject the requests that require custom processing.

The third shell will be used to send HTTP requests. RE Manager can also be controlled using 'qserver' CLI
tool. If only CLI tool will be used, then there is no need to start the Web Server. The following manual
demostrates how to control RE Manager using CLI commands and HTTP requests. The CLI tool commands will be
shown alongside with HTTP requests.

To view interactive API docs, visit::

  http://localhost:60610/docs

The 'qserver' CLI should be used in a separate shell. To display available options use::

  qserver -h
