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

THE FOLLOWING IS A COPY OF THE ORIGINAL README.RST FROM BLUESKY-QUEUESERVER PROJECT. PROPER DOCUMENTATION
FOR BLUESKY-HTTPSERVER IS COMING SOON.

Server for queueing plans

* Free software: 3-clause BSD license
* Documentation: https://bluesky.github.io/bluesky-httpserver.

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

Interacting with RE Manager using 'qserver' CLI tool and HTTP requests
----------------------------------------------------------------------

The most basic request is 'ping' intended to fetch some response from RE Manager::

  qserver ping
  http GET http://localhost:60610/api
  http GET http://localhost:60610/api/ping


Current default address of RE Manager is set to tcp://localhost:60615, but different
address may be passed as a parameter to CLI tool::

  qserver ping -a "tcp://localhost:60615"

The 'qserver' CLI tool may run in the monitoring mode (send 'ping' request to RE Manager every second)::

  qserver monitor

Currently 'ping' request returns the status of RE Manager, but the returned data may change. The recommended
way to fetch status of RE Manager is to use 'status' request::

  qserver status
  http GET http://localhost:60610/api/status

Before plans could be executed, the RE Worker environment must be opened. Opening RE Worker environment
involves loading beamline profile collection and instantiation of Run Engine and may take a few minutes.
The package comes with simulated profile collection that includes simulated Ophyd devices and built-in
Bluesky plans and loads almost instantly. An open RE Worker environment may be closed or destroyed.
Orderly closing of the environment is a safe operation, which is possible only when RE Worker
(and RE Manager) is in idle state, i.e. no plans are currently running or paused. Destroying
the environment is potentially dangerous, since it involves killing of RE Process that could potentially
be running plans, and supposed to be used for destroying unresponsive environment in case of RE failure.
Note that any operations on the queue (such as adding or removing plans) can be performed before
the environment is opened.

Open the new RE environment::

  qserver environment open
  http POST http://localhost:60610/api/environment/open

Close RE environment::

  qserver environment close
  http POST http://localhost:60610/api/environment/close

Destroy RE environment::

  qserver environment destroy
  http POST http://localhost:60610/api/environment/destroy

Get the lists (JSON) of allowed plans and devices::

  qserver allowed plans
  qserver allowed devices

  http POST http://localhost:60610/api/plans/allowed
  http POST http://localhost:60610/api/devices/allowed

The list of allowed plans and devices is generated based on the list of existing plans and devices
('existing_plans_and_devices.yaml' by default) and user group permissions ('user_group_permissions.yaml'
by default). The files with permission data are loaded at RE Manager startup. If any of the files
are changed while RE Manager is running (e.g. a new plan was added to the profile collection and
the new 'existing_plans_and_devices.yaml' file was generated) and restarting RE Manager is not
desirable, the data can be reloaded by sending 'permissions_reload' request::

  qserver permissions reload

  http GET http://localhost:60610/api/permissions/reload

Before plans could be executed they should be placed in the **plan queue**. The plan queue contains
**items**. The items are **plans** that could be executed by Run Engine or **instructions** that
can modify the state of the queue or RE Manager. Currently only one instruction ('queue_stop' - stops
execution of the queue) is supported.

Push a new plan to the back of the queue::

  qserver queue add plan '{"name":"count", "args":[["det1", "det2"]]}'
  qserver queue add plan '{"name":"scan", "args":[["det1", "det2"], "motor", -1, 1, 10]}'
  qserver queue add plan '{"name":"count", "args":[["det1", "det2"]], "kwargs":{"num":10, "delay":1}}'

  http POST http://localhost:60610/api/queue/item/add item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'
  http POST http://localhost:60610/api/queue/item/add item:='{"name":"scan", "args":[["det1", "det2"], "motor", -1, 1, 10], "item_type": "plan"}'
  http POST http://localhost:60610/api/queue/item/add item:='{"name":"count", "args":[["det1", "det2"]], "kwargs":{"num":10, "delay":1}, "item_type": "plan"}'

It takes 10 second to execute the third plan in the group above, so it is may be the most convenient for testing
pausing/resuming/stopping of experimental plans.

API for queue operations is designed to work identically with items of all types. For example, a 'queue_stop`
instruction can be added to the queue `queue_item_add` API::

  qserver queue add instruction queue-stop
  http POST http://localhost:60610/api/queue/item/add item:='{"name":"queue_stop", "item_type": "instruction"}'

An item can be added at any position of the queue. Push a plan to the front or the back of the queue::

  qserver queue add plan front '{"name":"count", "args":[["det1", "det2"]]}'
  qserver queue add plan back '{"name":"count", "args":[["det1", "det2"]]}'
  qserver queue add plan 2 '{"name":"count", "args":[["det1", "det2"]]}'  # Inserted at pos #2 (0-based)

  http POST http://localhost:60610/api/queue/item/add pos:='"front"' item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'
  http POST http://localhost:60610/api/queue/item/add pos:='"back"' item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'
  http POST http://localhost:60610/api/queue/item/add pos:=2 item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'

The following command will insert an item in place of the last item in the queue; the last item remains
the last item in the queue::

  qserver queue add plan -1 '{"name":"count", "args":[["det1", "det2"]]}'
  http POST http://localhost:60610/api/queue/item/add pos:=-1 item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'

An item can be inserted before or after an existing item with given Item UID.
Insert the plan before an existing item with <uid>::

  qserver queue add plan before_uid '<uid>' '{"name":"count", "args":[["det1", "det2"]]}'
  http POST http://localhost:60610/api/queue/item/add before_uid:='<uid>' item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'

Insert the plan after an existing item with <uid>::

  qserver queue add plan after_uid '<uid>' '{"name":"count", "args":[["det1", "det2"]]}'
  http POST http://localhost:60610/api/queue/item/add after_uid:='<uid>' item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'

If the queue has 5 items (0..4), then the following command pushes the new plan to the back of the queue::

  qserver queue add plan 5 '{"name":"count", "args":[["det1", "det2"]]}'
  http POST http://localhost:60610/api/queue/item/add pos:=5 item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'

The 'queue_item_add' request will accept any index value. If the index is out of range, then the item will
be pushed to the front or the back of the queue. If the queue is currently running, then it is recommended
to access elements using negative indices (counted from the back of the queue).

The names of the plans and devices are strings. The strings are converted to references to Bluesky plans and
Ophyd devices in the worker process. The simulated beamline profile collection includes all simulated
Ophyd devices and built-in Bluesky plans.

A batch of plans may be submitted to the queue by sending a single request. Every plan in the batch
is validated and the plans are added to the queue only if all plans pass validation. Otherwise the
batch is rejected. The following request adds two plans to the queue::

  http POST http://localhost:60610/api/queue/item/add/batch items:='[{"name":"count", "args":[["det1"]], "item_type": "plan"}, {"name":"count", "args":[["det2"]], "item_type": "plan"}]'

Alternatively the queue may be populated by uploading the list of plans with parameters in the form of
a spreadsheet to HTTP server. Note that this is an experimental feature, which could be modified at any
time until API is settled. The format of the spreadsheet will be specific to each beamline
using the server. Beamline-specific code will be distributed in a separate package from the core HTTP
server code. Currently, to upload spreadsheet located at `../sample_excel.xlsx` (could be arbitrary path)
run the following command::

  http --form POST http://localhost:60610/api/queue/upload/spreadsheet spreadsheet@../sample_excel.xlsx

Queue Server API allow to execute a single item (plan or instruction) submitted with the API call. Execution
of an item starts immediately if possible (RE Manager is idle and RE Worker environment exists), otherwise
API call fails and the item is not added to the queue. The following commands start execution of a single plan::

  qserver queue execute plan '{"name":"count", "args":[["det1", "det2"]], "kwargs":{"num":10, "delay":1}}'
  http POST http://localhost:60610/api/queue/item/execute item:='{"name":"count", "args":[["det1", "det2"]], "kwargs":{"num":10, "delay":1}, "item_type": "plan"}'

Queue can be edited at any time. Changes to the running queue become effective the moment they are
performed. As the currently running plan is finished, the new plan is popped from the top of the queue.

The contents of the queue may be fetched at any time::

  qserver queue get
  http GET http://localhost:60610/api/queue/get

The last item can be removed (popped) from the back of the queue::

  qserver queue item remove
  qserver queue item remove back

  echo '{}' | http POST http://localhost:60610/api/queue/item/remove
  http POST http://localhost:60610/api/queue/item/remove pos:='"back"'

The position of the removed item may be specified similarly to `queue_item_add` request with the difference
that the position index must point to the existing element, otherwise the request fails (returns 'success==False').
The following examples remove the plan from the front of the queue and the element previous to last::

  qserver queue item remove front
  qserver queue item remove -p -2

  http POST http://localhost:60610/api/queue/item/remove pos:='"front"'
  http POST http://localhost:60610/api/queue/item/remove pos:=-2

The items can also be addressed by UID. Remove the item with <uid>::

  qserver queue item remove '<uid>'
  http POST http://localhost:60610/api/queue/item/remove uid:='<uid>'

Items can be read from the queue without changing it. `queue_item_get` requests are formatted identically to
`queue_item_remove` requests::

  qserver queue item get
  qserver queue item get back
  qserver queue item get front
  qserver queue item get -2
  qserver queue item get '<uid>'

  echo '{}' | http GET http://localhost:60610/api/queue/item/get
  http GET http://localhost:60610/api/queue/item/get pos:='"back"'
  http GET http://localhost:60610/api/queue/item/get pos:='"front"'
  http GET http://localhost:60610/api/queue/item/get pos:=-2
  http GET http://localhost:60610/api/queue/item/get uid:='<uid>'

Items can be moved within the queue. Items can be addressed by position or UID. If positional addressing
is used then items are moved from 'source' position to 'destination' position.
If items are addressed by UID, then the item with <uid_source> is inserted before or after
the item with <uid_dest>::

  qserver queue item move 3 5
  qserver queue item move <uid_source> before <uid_dest>
  qserver queue item move <uid_source> after <uid_dest>

  http POST http://localhost:60610/api/queue/item/move pos:=3 pos_dest:=5
  http POST http://localhost:60610/api/queue/item/move uid:='<uid_source>' before_uid:='<uid_dest>'
  http POST http://localhost:60610/api/queue/item/move uid:='<uid_source>' after_uid:='<uid_dest>'

Addressing by position and UID can be mixed. The following instruction will move queue item #3
to the position following an item with <uid_dest>::

  qserver queue item move 3 after <uid_dest>
  http POST http://localhost:60610/api/queue/item/move pos:=3 after_uid:='<uid_dest>'

The following instruction moves item with <uid_source> to the front of the queue::

  qserver queue item move <uid_source> "front"
  http POST http://localhost:60610/api/queue/item/move uid:='<uid_source>' pos_dest:='"front"'

The parameters of queue items may be updated or replaced. When the item is replaced, it is assigned a new
item UID, while if the item is updated, item UID remains the same. The commands implementing those
operations do not distinguish plans and instructions, i.e. an instruction may be updated/replaced
by a plan or a plan by an instruction. The operations may be performed using CLI tool by calling
*'queue update'* and *'queue replace'* with parameter *<existing-uid>* being item UID of the item in the
queue which is being replaced followed by the JSON representation of the dictionary of parameters
of the new item::

  qserver queue update plan <existing-uid> {"name":"count", "args":[["det1", "det2"]]}'
  qserver queue update instruction <existing-uid> {"action":"queue_stop"}
  qserver queue replace plan <existing-uid> {"name":"count", "args":[["det1", "det2"]]}'
  qserver queue replace instruction <existing-uid> {"action":"queue_stop"}

REST API */queue/item/update* is used to implement both operations. Item parameter *'item_uid'* must
be set to the UID of the item to be updated. Additional API parameter 'replace' determines if the item
is updated or replaced. If the parameter is skipped or set *false*, the item is updated. If the
parameter is set *true*, the item is replaced (i.e. new item UID is generated)::

  http POST http://localhost:60610/api/queue/item/update item:='{"item_uid":"<existing-uid>", "name":"count", "args":[["det1", "det2"]], "item_type":"plan"}'
  http POST http://localhost:60610/api/queue/item/update item:='{"item_uid":"<existing-uid>", "name":"queue_stop", "item_type":"instruction"}'
  http POST http://localhost:60610/api/queue/item/update replace:=true item:='{"item_uid":"<existing-uid>", "name":"count", "args":[["det1", "det2"]], "item_type":"plan"}'
  http POST http://localhost:60610/api/queue/item/update replace:=true item:='{"item_uid":"<existing-uid>", "name":"queue_stop", "item_type":"instruction"}'

Remove all entries from the plan queue::

  qserver queue clear
  http POST http://localhost:60610/api/queue/clear

The plan queue can operate in LOOP mode, which is disabled by default. To enable or disable the LOOP mode
the following commands::

  qserver queue mode set loop True
  qserver queue mode set loop False

  http POST http://localhost:60610/api/queue/mode/set mode:='{"loop": true}'
  http POST http://localhost:60610/api/queue/mode/set mode:='{"loop": false}'

Start execution of the plan queue. The environment MUST be opened before queue could be started::

  qserver queue start
  http POST http://localhost:60610/api/queue/start

Request to execute an empty queue is a valid operation that does nothing.

As the queue is running, the list of active runs (runs generated by the running plan may be obtained
at any time). The set of active runs consists of two subsets: open runs and closed runs. For
simple single-run plans the list will contain only one item. The list can be loaded using CLI
commands and HTTP API::

  qserver re runs            # Get the list of active runs (runs generated by the currently running plans)
  qserver re runs active     # Get the list of active runs
  qserver re runs open       # Get the list of open runs (subset of active runs)
  qserver re runs closed     # Get the list of closed runs (subset of active runs)

  http GET http://localhost:60610/api/re/runs/active  # Get the list of active runs
  http GET http://localhost:60610/api/re/runs/open    # Get the list of open runs
  http GET http://localhost:60610/api/re/runs/closed  # Get the list of closed runs

The queue can be stopped at any time. Stopping the queue is a safe operation. When the stopping
sequence is initiated, the currently running plan is finished and the next plan is not be started.
The stopping sequence can be cancelled if it was activated by mistake or decision was changed::

  qserver queue stop
  qserver queue stop cancel

  http POST http://localhost:60610/api/queue/stop
  http POST http://localhost:60610/api/queue/stop/cancel

While a plan in a queue is executed, operation Run Engine can be paused. In the unlikely event
if the request to pause is received while RunEngine is transitioning between two plans, the request
may be rejected by the RE Worker. In this case it needs to be repeated. If Run Engine is in the paused
state, plan execution can be resumed, aborted, stopped or halted. If the plan is aborted, stopped
or halted, it is not removed from the plan queue (it remains the first in the queue) and execution
of the queue is stopped. Execution of the queue may be started again if needed.

Running plan can be paused immediately (returns to the last checkpoint in the plan) or at the next
checkpoint (deferred pause)::

  qserver re pause
  qserver re pause deferred
  qserver re pause immediate

  http POST http://localhost:60610/api/re/pause option="deferred"
  http POST http://localhost:60610/api/re/pause option="immediate"

Resuming, aborting, stopping or halting of currently executed plan::

  qserver re resume
  qserver re stop
  qserver re abort
  qserver re halt

  http POST http://localhost:60610/api/re/resume
  http POST http://localhost:60610/api/re/stop
  http POST http://localhost:60610/api/re/abort
  http POST http://localhost:60610/api/re_halt

There is minimal user protection features implemented that will prevent execution of
the commands that are not supported in current state of the server. Error messages are printed
in the terminal that is running the server along with output of Run Engine.

Data on executed plans, including stopped plans, is recorded in the history. History can
be downloaded at any time::

  qserver history get
  http GET http://localhost:60610/api/history/get

History is not intended for long-term storage. It can be cleared at any time::

  qserver history clear
  http POST http://localhost:60610/api/history/clear

Stop RE Manager (exit RE Manager application). There are two options: safe request that is rejected
when the queue is running or a plan is paused::

  qserver manager stop
  qserver manager stop safe_on

  echo '{}' | http POST http://localhost:60610/api/manager/stop
  http POST http://localhost:60610/api/manager/stop option="safe_on"

Manager can be also stopped at any time using unsafe stop, which causes current RE Worker to be
destroyed even if a plan is running::

  qserver manager stop safe_off
  http POST http://localhost:60610/api/manager/stop option="safe_off"

The 'test_manager_kill' request is designed specifically for testing ability of RE Watchdog
to restart malfunctioning RE Manager process. This command stops event loop of RE Manager process
and causes RE Watchdog to restart the process (currently after 5 seconds). RE Manager
process is expected to fully recover its state, so that the restart does not affect
running or paused plans or the state of the queue. Another potential use of the request
is to test handling of communication timeouts, since RE Manager does not respond to the request::

  qserver manager kill test
  http POST http://localhost:60610/api/test/manager/kill


Additional API
--------------
API that are implemented, but not listed in this document:

- ``/api/re/runs`` - access to ``re_runs``, combines ``/api/re/runs/active``, ``/api/re/runs/open``, ``/api/re/runs/closed``
- ``/api/plans/existing`` - access to ``plans_existing`` API
- ``/api/devices/existing`` - access to ``devices_existing`` API
- ``/api/permissions/get`` - access to ``permissions_get`` API
- ``/api/permissions/set`` - access to ``permissions_set`` API
- ``/api/script/upload`` - access to ``script_upload`` API
- ``/api/function/execute`` - access to ``function_execute`` API
- ``/api/task/status`` - access to ``task_status`` API
- ``/api/task/result`` - access to ``task_result`` API
- ``/api/lock`` - lock RE Manager
- ``/api/lock/info`` - load RE Manager lock status, optionally verify a lock key
- ``/api/unlock`` - unlock RE Manager

Streaming Console Output of RE Manager
--------------------------------------

HTTP server provides streaming API ``stream_console_output`` that allows web applications to receive,
process and display captured console output of RE manager. To test operation of the streaming API,
enable publishing of console output by RE Manager::

  start-re-manager --zmq-publish-console ON

start HTTP Server, start Web Browser and type the following address::

  http://localhost:60610/stream_console_output

Then open a separate terminal and send a few requests to RE Manager using ``qserver`` tool, e.g. ::

  qserver environment open
  qserver environment close

JSON representation of console output message (timestamp and text message) will be displayed
in the browser, e.g. ::

{"time": 1629816304.5475085, "msg": "INFO:bluesky_queueserver.manager.manager:Opening the new RE environment ...\n"}

Client application is responsible for processing JSON messages and displaying formatted output to users.

HTTP Server is not performing caching of the console output, so streamed data contain only messages
received after the web client connects to the server.

If RE Manager is configured to publish console address to 0MQ socket with port number different from
default or HTTP server is running on a separate workstation/server, the address of 0MQ socket
can be specified by setting the environment variable ``QSERVER_ZMQ_INFO_ADDRESS``, e.g. ::

  export QSERVER_ZMQ_INFO_ADDRESS='tcp://localhost:60625'


Console Output of RE Manager
----------------------------
In some cases, using streaming console output is inconvenient or difficult. The server
provides endpoint ``/console_output`` returns the last ``nlines`` of the console output
represented as a text string. The parameter ``nlines`` is optional with the default value of 200.
The maximum number of returned lines is limited (currently to 2000 lines). ::

  http GET http://localhost:60610/api/console_output
  http GET http://localhost:60610/api/console_output lines=500
